"""Prompt-aware recall for the ``PreInvocation`` hook.

The SessionStart briefing is workspace-shaped, not request-shaped. This module
extracts the newest user request (and, on later invocations, the newest tool
error) from the transcript tail, retrieves a very small number of verified
lessons or matching skills, and returns one compact ``ephemeralMessage``.

Design constraints:

* one injection per prompt/error signature (deduped on disk, per conversation);
* the first invocation of a turn is the normal injection point, with a
  prompt-hash fallback for hosts that keep ``invocationNum`` global;
* a later invocation only injects after a new tool error that matches verified
  experience;
* never raise into the hook; the caller always returns ``{"injectSteps": []}``
  on any failure.
"""

from __future__ import annotations

import json
import re
import time
from pathlib import Path
from typing import Any, Iterable

from .config import get_state_dir, safe_conversation_id, tokenize
from .memory_store import MemoryStore
from .redaction import redact
from .skill_builder import SkillBuilder
from .util import atomic_write_text, file_lock, one_line, sha256_text, truncate

MAX_TAIL_BYTES = 256_000
MAX_TAIL_STEPS = 1_500
MAX_MESSAGE_CHARS = 1_500
MAX_LESSONS = 3
MAX_SKILLS = 1
DEDUPE_TTL_SECONDS = 6 * 60 * 60
DEDUPE_LIMIT = 200

_LOW_SIGNAL_PROMPTS = {
    "continue",
    "go",
    "ok",
    "okay",
    "yes",
    "no",
    "next",
    "proceed",
    "keep going",
    "do it",
    "sounds good",
    "thanks",
    "thank you",
}


# ------------------------------------------------------------------ transcript
def _tail_steps(transcript_path: Path | str | None) -> list[dict[str, Any]]:
    if not transcript_path:
        return []
    path = Path(transcript_path).expanduser()
    try:
        size = path.stat().st_size
        with path.open("rb") as handle:
            if size > MAX_TAIL_BYTES:
                handle.seek(size - MAX_TAIL_BYTES)
                handle.readline()  # drop the partial first line
            data = handle.read()
    except (OSError, ValueError):
        return []

    steps: list[dict[str, Any]] = []
    for line in data.decode("utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            steps.append(value)
    return steps[-MAX_TAIL_STEPS:]


def _content_of(step: dict[str, Any]) -> str:
    for key in ("content", "output", "text", "result", "message"):
        value = step.get(key)
        if isinstance(value, str) and value.strip():
            return value
    return ""


def clean_prompt(text: str) -> str:
    """Strip Antigravity wrappers and make a prompt safe to echo back."""
    if not isinstance(text, str):
        return ""
    text = re.sub(r"<USER_REQUEST>|</USER_REQUEST>", "", text, flags=re.IGNORECASE)
    text = re.sub(
        r"<ADDITIONAL_METADATA>.*?</ADDITIONAL_METADATA>",
        "",
        text,
        flags=re.IGNORECASE | re.DOTALL,
    )
    text = re.sub(
        r"<USER_SETTINGS_CHANGE>.*?</USER_SETTINGS_CHANGE>",
        "",
        text,
        flags=re.IGNORECASE | re.DOTALL,
    )
    text = re.sub(r"The current local time is:.*", "", text, flags=re.IGNORECASE)
    cleaned = one_line(redact(text), 600)
    if cleaned.strip(" .!?:;,").lower() in _LOW_SIGNAL_PROMPTS:
        return ""
    if len(tokenize(cleaned)) < 1:
        return ""
    return cleaned


def latest_user_prompt(
    transcript_path: Path | str | None,
) -> tuple[str, int]:
    """Return ``(cleaned_prompt, step_index)`` for the newest user request."""
    steps = _tail_steps(transcript_path)
    for index in range(len(steps) - 1, -1, -1):
        step = steps[index]
        step_type = str(step.get("type", "") or "")
        source = str(step.get("source", "") or "")
        if step_type != "USER_INPUT" and source != "USER_EXPLICIT":
            continue
        prompt = clean_prompt(_content_of(step))
        if prompt:
            return prompt, index
    return "", -1


def _error_of(step: dict[str, Any]) -> str:
    for key in ("error", "errorMessage", "error_message"):
        value = step.get(key)
        if isinstance(value, str) and value.strip():
            return value
    for call in step.get("tool_calls") or step.get("toolCalls") or ():
        if isinstance(call, dict):
            value = call.get("error") or call.get("errorMessage")
            if isinstance(value, str) and value.strip():
                return value
    status = str(step.get("status", "") or "").upper()
    if status in ("ERROR", "FAILED"):
        content = _content_of(step)
        if content.strip():
            return content
    return ""


def recent_tool_error(
    transcript_path: Path | str | None,
    after_step: int = -1,
) -> str:
    """Newest tool error after the current prompt, if the transcript has one."""
    steps = _tail_steps(transcript_path)
    start = max(after_step + 1, 0)
    for step in reversed(steps[start:]):
        error = _error_of(step)
        if error:
            return one_line(redact(error), 500)
    return ""


# --------------------------------------------------------------------- search
def _tokens(text: str) -> set[str]:
    return set(tokenize(text))


def _utility(lesson: dict[str, Any]) -> float:
    try:
        return float(MemoryStore.lesson_utility(lesson))
    except Exception:
        return 0.0


def _trusted(text: str) -> bool:
    try:
        from .trust import scan_text

        return not any(finding.severity == "high" for finding in scan_text(text))
    except Exception:
        # Trust scanning is an additional gate, not a hard dependency.
        return True


def find_recall(
    query: str,
    workspace: Path | str | None,
    limit: int = MAX_LESSONS,
    include_skills: bool = True,
) -> dict[str, list[dict[str, Any]]]:
    """Return verified lessons and matching skills for one query."""
    query = (query or "").strip()
    if not query:
        return {"lessons": [], "skills": []}
    query_tokens = _tokens(query)
    if not query_tokens:
        return {"lessons": [], "skills": []}

    store = MemoryStore(workspace=workspace)
    lessons: list[dict[str, Any]] = []
    try:
        candidates = store.search_lessons(
            query,
            limit=max(limit * 3, 6),
            workspace=workspace,
            verified_only=True,
        )
    except Exception:
        candidates = []
    for lesson in candidates:
        text = " ".join(
            str(lesson.get(key) or "")
            for key in ("trigger_tool", "error_pattern", "resolution")
        )
        overlap = len(query_tokens & _tokens(text))
        if overlap <= 0:
            continue
        if not _trusted(text):
            continue
        enriched = dict(lesson)
        enriched["_overlap"] = overlap
        lessons.append(enriched)
    lessons.sort(
        key=lambda item: (item.get("_overlap", 0), _utility(item), item.get("last_seen") or ""),
        reverse=True,
    )
    lessons = lessons[: max(1, limit)]

    skills: list[dict[str, Any]] = []
    if include_skills:
        try:
            installed = SkillBuilder(workspace).list_installed_skills(workspace)
        except Exception:
            installed = []
        for skill in installed:
            if not skill.get("valid", True):
                continue
            text = f"{skill.get('name', '')} {skill.get('description', '')}"
            overlap = len(query_tokens & _tokens(text))
            if overlap <= 0:
                continue
            enriched = dict(skill)
            enriched["_overlap"] = overlap
            skills.append(enriched)
        skills.sort(
            key=lambda item: (
                item.get("_overlap", 0),
                float(item.get("quality") or 0),
                item.get("name", ""),
            ),
            reverse=True,
        )
        skills = skills[: max(1, MAX_SKILLS)]

    return {"lessons": lessons, "skills": skills}


# -------------------------------------------------------------------- message
def format_recall_message(
    lessons: Iterable[dict[str, Any]],
    skills: Iterable[dict[str, Any]],
) -> str:
    lesson_rows = list(lessons)[:MAX_LESSONS]
    skill_rows = list(skills)[:MAX_SKILLS]
    if not lesson_rows and not skill_rows:
        return ""
    lines = ["⚡ Harness² recall for the current request:"]
    for lesson in lesson_rows:
        tool = one_line(lesson.get("trigger_tool") or "tool", 60)
        failure = one_line(
            lesson.get("error_pattern") or lesson.get("error_message") or "", 150
        )
        resolution = one_line(
            lesson.get("resolution") or lesson.get("resolution_summary") or "", 240
        )
        if failure and resolution:
            lines.append(f"- `{tool}`: {failure} → {resolution}")
        elif resolution:
            lines.append(f"- `{tool}`: {resolution}")
    for skill in skill_rows:
        name = one_line(skill.get("name") or "", 80)
        description = one_line(skill.get("description") or "", 180)
        if name:
            lines.append(f"- Skill `{name}`: {description}")
    lines.append("Use only what matches the current task; verify before acting.")
    return truncate("\n".join(lines), MAX_MESSAGE_CHARS)


# --------------------------------------------------------------------- dedupe
def _retrieval_state_path(
    conversation_id: str, transcript_path: Path | str | None
) -> Path:
    key = safe_conversation_id(conversation_id or "")
    if not key:
        key = "t" + sha256_text(str(transcript_path or ""))[:16]
    return get_state_dir() / "retrieval" / f"{key}.json"


def _read_state(path: Path) -> dict[str, Any]:
    empty: dict[str, Any] = {
        "version": 1,
        "last_prompt_hash": "",
        "last_error_hash": "",
        "signatures": {},
    }
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return empty
    if not isinstance(value, dict):
        return empty
    signatures = value.get("signatures")
    empty["signatures"] = signatures if isinstance(signatures, dict) else {}
    prompt_hash = value.get("last_prompt_hash")
    empty["last_prompt_hash"] = prompt_hash if isinstance(prompt_hash, str) else ""
    error_hash = value.get("last_error_hash")
    empty["last_error_hash"] = error_hash if isinstance(error_hash, str) else ""
    return empty


def _pruned(signatures: dict[str, Any], now: float) -> dict[str, float]:
    kept: dict[str, float] = {}
    for signature, stamp in signatures.items():
        try:
            value = float(stamp)
        except (TypeError, ValueError):
            continue
        if now - value <= DEDUPE_TTL_SECONDS:
            kept[str(signature)] = value
    if len(kept) > DEDUPE_LIMIT:
        newest = sorted(kept.items(), key=lambda item: item[1], reverse=True)
        kept = dict(newest[:DEDUPE_LIMIT])
    return kept


def _commit_state(
    path: Path,
    prompt_hash: str,
    signature: str,
    error_hash: str = "",
) -> bool:
    """Claim ``signature`` and return True when this call should inject."""
    lock = path.with_name(path.name + ".lock")
    try:
        with file_lock(lock, timeout=0.75) as acquired:
            if not acquired:
                # Prefer skipping a recall over blocking the model call.
                return False
            state = _read_state(path)
            now = time.time()
            signatures = _pruned(state.get("signatures", {}), now)
            duplicate = bool(signature) and signature in signatures
            if signature and not duplicate:
                signatures[signature] = now
            state = {
                "version": 1,
                "last_prompt_hash": prompt_hash or state.get("last_prompt_hash", ""),
                "last_error_hash": error_hash or state.get("last_error_hash", ""),
                "signatures": signatures,
            }
            atomic_write_text(
                path, json.dumps(state, ensure_ascii=False, sort_keys=True) + "\n"
            )
            return not duplicate
    except Exception:
        return False


# ------------------------------------------------------------------ entrypoint
def build_pre_invocation_payload(
    conversation_id: str = "",
    workspace: Path | str | None = None,
    transcript_path: Path | str | None = None,
    invocation_num: int = 0,
    initial_num_steps: int = 0,
) -> dict[str, Any]:
    """Build a PreInvocation response with at most one ephemeral recall step."""
    try:
        invocation = max(0, int(invocation_num))
    except (TypeError, ValueError):
        invocation = 0

    prompt, prompt_index = latest_user_prompt(transcript_path)
    error = recent_tool_error(transcript_path, after_step=prompt_index)
    prompt_hash = sha256_text(prompt)[:24] if prompt else ""
    error_hash = sha256_text(error)[:24] if error else ""

    state_path = _retrieval_state_path(conversation_id, transcript_path)
    prior_state = _read_state(state_path)
    new_turn = bool(prompt) and prompt_hash != prior_state.get("last_prompt_hash", "")
    new_error = bool(error) and error_hash != prior_state.get("last_error_hash", "")
    allow_prompt = bool(prompt) and (invocation == 0 or new_turn)

    queries: list[tuple[str, str]] = []
    if allow_prompt:
        queries.append(("prompt", prompt))
    if new_error and (invocation > 0 or not allow_prompt):
        queries.append(("error", error))

    if not queries:
        if prompt and (invocation == 0 or new_turn):
            _commit_state(state_path, prompt_hash, "", error_hash)
        return {"injectSteps": []}

    lessons: list[dict[str, Any]] = []
    skills: list[dict[str, Any]] = []
    seen_lessons: set[Any] = set()
    signature_parts: list[str] = []
    for kind, query in queries:
        found = find_recall(query, workspace)
        signature_parts.append(f"{kind}:{sha256_text(query)[:16]}")
        for lesson in found["lessons"]:
            key = lesson.get("id")
            if key in seen_lessons:
                continue
            seen_lessons.add(key)
            lessons.append(lesson)
        for skill in found["skills"]:
            if not any(
                item.get("name") == skill.get("name")
                and item.get("scope") == skill.get("scope")
                for item in skills
            ):
                skills.append(skill)

    lessons.sort(
        key=lambda item: (item.get("_overlap", 0), _utility(item)), reverse=True
    )
    lessons = lessons[:MAX_LESSONS]
    skills = skills[:MAX_SKILLS]
    message = format_recall_message(lessons, skills)
    if not message:
        _commit_state(
            state_path,
            prompt_hash if allow_prompt else "",
            "",
            error_hash,
        )
        return {"injectSteps": []}

    ids = ",".join(str(item.get("id")) for item in lessons if item.get("id") is not None)
    signature = "|".join([*signature_parts, f"lessons:{ids}"])
    if not _commit_state(
        state_path, prompt_hash if allow_prompt else "", signature, error_hash
    ):
        return {"injectSteps": []}

    store = MemoryStore(workspace=workspace)
    try:
        store.record_lessons_surfaced(
            [int(item["id"]) for item in lessons if item.get("id") is not None]
        )
    except Exception:
        pass
    try:
        store.record_skills_surfaced(
            [
                {"name": item.get("name"), "scope": item.get("scope")}
                for item in skills
                if item.get("name")
            ]
        )
    except Exception:
        pass

    return {"injectSteps": [{"ephemeralMessage": message}]}
