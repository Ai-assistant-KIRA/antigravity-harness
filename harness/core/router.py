"""Task-aware skill and tool routing for project-local ``AGENTS.md`` files.

The router answers one question: *given this request, which project-local skills
and tools should the agent load?* It is deliberately deterministic and offline:

* only skills that physically live under ``<workspace>/.agents/skills`` are
  eligible, so another project's or the global library's skills cannot leak in;
* tools come from a small built-in catalog, optionally restricted or extended by
  ``<workspace>/.agents/tools.json``;
* the selection is written into the managed ``Task Routing`` section of the
  project's ``AGENTS.md``, which is the surface Antigravity already reads;
* task text is redacted and trust-scanned before it is persisted.

No model call is made. Ranking is lexical overlap plus project signals, quality,
and evidence of reuse.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .agents_md_manager import AgentsMdManager, MAX_ROUTING_ITEMS
from .config import (
    detect_project_signals,
    get_workspace_skills_dir,
    get_workspace_tools_path,
    tokenize,
)
from .memory_store import MemoryStore, enrich_skills_with_usage, is_retired_skill
from .redaction import redact, redact_strict
from .settings import get_setting, load_settings
from .skill_builder import SkillBuilder
from .trust import scan_text
from .util import one_line, strip_paths

# Words that appear in most requests and match most skill descriptions. They
# must not make an unrelated skill look relevant.
_ROUTING_STOPWORDS = frozenset(
    {
        "the", "and", "for", "with", "this", "that", "from", "into", "our", "your",
        "use", "using", "when", "task", "involves", "make", "made", "want", "need",
        "please", "can", "you", "all", "any", "new", "get", "set", "run", "add",
        "now", "also", "just", "like", "both", "them", "they", "its", "has",
        "have", "will", "should", "would", "could", "about", "then", "than",
        "repeatable", "procedure", "verified", "step", "steps", "project",
        "file", "files", "work", "editor", "user", "skill", "skills",
    }
)
MIN_SKILL_OVERLAP = 2

_TOOL_NAME_RE = re.compile(r"^[A-Za-z0-9_.:-]{2,80}$")

# Built-in tools exposed by Antigravity-style agents. The catalog is only a
# ranking vocabulary; it never makes a tool available by itself.
TOOL_CATALOG: tuple[dict[str, Any], ...] = (
    {
        "name": "run_command",
        "description": "Run shell, build, test, package-manager, or git commands.",
        "signals": (
            "run command terminal shell powershell bash cmd",
            "pytest unittest test tests build compile lint typecheck",
            "install dependency dependencies package npm pnpm yarn pip uv poetry",
            "git commit branch merge rebase deploy docker make",
        ),
    },
    {
        "name": "read_file",
        "description": "Read a file, inspect source, or gather exact line context.",
        "signals": (
            "read open inspect view file source code line error traceback",
            "log logs config configuration",
        ),
    },
    {
        "name": "write_file",
        "description": "Create or edit files and apply the implementation.",
        "signals": (
            "write create edit modify update implement refactor add fix change",
            "file code patch",
        ),
    },
    {
        "name": "list_dir",
        "description": "List a directory to discover project structure.",
        "signals": ("list directory folder tree structure workspace project files",),
    },
    {
        "name": "find_by_name",
        "description": "Find files by name or glob pattern.",
        "signals": ("find locate search filename glob path file",),
    },
    {
        "name": "grep_search",
        "description": "Search file contents for a symbol, string, or pattern.",
        "signals": ("grep search symbol reference usage pattern text contents",),
    },
    {
        "name": "search_web",
        "description": "Search the web for current documentation or unknown APIs.",
        "signals": (
            "search web research docs documentation latest version unknown error",
            "look up internet",
        ),
    },
    {
        "name": "read_url_content",
        "description": "Fetch a URL and read its content.",
        "signals": ("url fetch http https website page link download",),
    },
    {
        "name": "browser",
        "description": "Drive a browser to inspect or verify a web UI.",
        "signals": (
            "browser page click screenshot web ui frontend localhost render",
            "playwright e2e end-to-end",
        ),
    },
    {
        "name": "run_extension",
        "description": "Run a configured editor or Antigravity extension.",
        # Only an explicit mention of an extension. "plugin", "editor" and
        # "run" matched nearly every request in a plugin or video-editor repo.
        "signals": ("extension", "vs code extension", "ide extension"),
    },
    {
        "name": "generate_image",
        "description": "Generate or edit an image asset.",
        "signals": ("image picture asset logo illustration icon generate image",),
    },
)

_DEFAULT_TOOLS = ("read_file", "run_command", "write_file")
_MAX_DESCRIPTION = 140


def _safe_task_text(text: str, limit: int = 160) -> str:
    """Make a user request safe to persist in a project rule file.

    Absolute paths and credentials are removed rather than masked: the task
    line is a short summary, and a pasted API key or a user's file path does
    not belong in a committed AGENTS.md.
    """
    cleaned = redact_strict(str(text or ""))
    cleaned = re.sub(r"\S*\[REDACTED\]\S*", " ", cleaned)
    cleaned = strip_paths(cleaned)
    # Tails of paths whose folder names contain spaces ("video editor\x.mp4")
    # and quotes orphaned by the removal.
    cleaned = re.sub(r"\S*[\\/]\S*", " ", cleaned)
    cleaned = re.sub(r"(?<!\w)[\"'](?!\w)", " ", cleaned)
    raw = one_line(cleaned, limit).strip(" ,;:-\"'")
    if not raw:
        return ""
    if "<!-- HARNESS:" in raw or "HARNESS:AUTONOMOUS_RULES" in raw:
        return ""
    try:
        if any(finding.severity == "high" for finding in scan_text(raw)):
            # Do not persist instructions that look like prompt injection.
            return ""
    except Exception:
        pass
    return raw


def _safe_description(text: Any, limit: int = _MAX_DESCRIPTION) -> str:
    value = one_line(redact(str(text or "")), limit)
    if not value:
        return ""
    try:
        if any(finding.severity == "high" for finding in scan_text(value)):
            return ""
    except Exception:
        pass
    return value.replace("`", "'")


def _as_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


@dataclass
class RouteSelection:
    task: str = ""
    skills: list[dict[str, Any]] = field(default_factory=list)
    tools: list[dict[str, Any]] = field(default_factory=list)
    inventory: list[str] = field(default_factory=list)
    declared_tools: bool = False

    def items(self) -> list[str]:
        lines: list[str] = []
        for skill in self.skills:
            description = _safe_description(skill.get("description"))
            suffix = f" — {description}" if description else ""
            lines.append(f"Skill: `{skill.get('name')}`{suffix}")
        for tool in self.tools:
            description = _safe_description(tool.get("description"), 110)
            suffix = f" — {description}" if description else ""
            lines.append(f"Tool: `{tool.get('name')}`{suffix}")
        if self.inventory:
            names = ", ".join(f"`{name}`" for name in self.inventory)
            lines.append(
                f"Project skill inventory: {names} "
                "(project-local only; load one when its trigger matches)"
            )
        else:
            lines.append(
                "Project skill inventory: none yet "
                "(a verified multi-step session will generate one)"
            )
        return lines[: max(1, MAX_ROUTING_ITEMS - 1)]

    def summary(self) -> str:
        parts: list[str] = []
        if self.skills:
            parts.append("skills " + ", ".join(f"`{item['name']}`" for item in self.skills))
        if self.tools:
            parts.append("tools " + ", ".join(f"`{item['name']}`" for item in self.tools))
        if not parts:
            return ""
        return "Routed project context: " + "; ".join(parts) + " (written to AGENTS.md)"


@dataclass
class RouteResult:
    selection: RouteSelection
    write: dict[str, Any]

    def as_dict(self) -> dict[str, Any]:
        return {
            "task": self.selection.task,
            "skills": [item.get("name") for item in self.selection.skills],
            "tools": [item.get("name") for item in self.selection.tools],
            "inventory": self.selection.inventory,
            "path": self.write.get("path"),
            "updated": bool(self.write.get("updated")),
            "items": int(self.write.get("items") or 0),
            "warnings": list(self.write.get("warnings") or []),
        }


class TaskRouter:
    """Select project-local skills and tools for a task and persist the choice."""

    def __init__(self, workspace_path: Path | str | None = None):
        self.workspace_path = (
            Path(workspace_path).expanduser() if workspace_path else None
        )
        self.settings, self.settings_warnings = load_settings()
        self.enabled = bool(get_setting(self.settings, "routing.enabled", True))
        self.auto_apply = bool(get_setting(self.settings, "routing.auto_apply", True))
        self.max_skills = max(
            0, int(get_setting(self.settings, "routing.max_skills", 5))
        )
        self.max_tools = max(
            0, int(get_setting(self.settings, "routing.max_tools", 6))
        )
        self.manager = AgentsMdManager(self.workspace_path)
        self.skill_builder = SkillBuilder(self.workspace_path)

    # ------------------------------------------------------------------ inputs
    def project_skills(self) -> list[dict[str, Any]]:
        """Only skills that physically belong to this workspace."""
        if not self.workspace_path:
            return []
        root = get_workspace_skills_dir(self.workspace_path)
        try:
            installed = self.skill_builder.list_installed_skills(
                self.workspace_path, include_global=False
            )
        except Exception:
            installed = []
        try:
            root_resolved = root.resolve()
        except OSError:
            root_resolved = root
        skills: list[dict[str, Any]] = []
        for skill in installed:
            if skill.get("scope") != "workspace" or not skill.get("valid", True):
                continue
            try:
                Path(str(skill.get("path") or "")).resolve().relative_to(root_resolved)
            except (OSError, ValueError):
                continue
            skills.append(skill)
        try:
            store = MemoryStore(workspace=self.workspace_path)
            skills = enrich_skills_with_usage(
                skills, store.get_all_skills(self.workspace_path), store.project
            )
        except Exception:
            pass
        return [skill for skill in skills if not is_retired_skill(skill)]

    def declared_tools(self) -> list[dict[str, Any]]:
        """Optional project tool declarations from ``.agents/tools.json``."""
        if not self.workspace_path:
            return []
        path = get_workspace_tools_path(self.workspace_path)
        if not path.exists():
            return []
        try:
            document = json.loads(path.read_text(encoding="utf-8-sig"))
        except (OSError, ValueError):
            return []
        if isinstance(document, dict):
            entries = document.get("tools") or document.get("allowed_tools") or []
        elif isinstance(document, list):
            entries = document
        else:
            return []
        tools: list[dict[str, Any]] = []
        for entry in entries if isinstance(entries, list) else []:
            if isinstance(entry, str):
                name, description, signals = entry, "", []
            elif isinstance(entry, dict):
                name = str(entry.get("name") or entry.get("id") or "")
                description = str(entry.get("description") or "")
                raw_signals = entry.get("signals") or entry.get("keywords") or []
                signals = [str(item) for item in raw_signals] if isinstance(raw_signals, list) else []
            else:
                continue
            name = name.strip()
            if not _TOOL_NAME_RE.match(name):
                continue
            tools.append(
                {
                    "name": name,
                    "description": _safe_description(description, 160)
                    or "Declared by this project.",
                    "signals": tuple(signals),
                    "declared": True,
                }
            )
        return tools

    # ----------------------------------------------------------------- ranking
    def select(self, task: str = "") -> RouteSelection:
        clean_task = _safe_task_text(task)
        signals = detect_project_signals(self.workspace_path)
        skill_records = self.project_skills()
        selection = RouteSelection(
            task=clean_task,
            inventory=[str(item.get("name")) for item in skill_records][
                : max(0, MAX_ROUTING_ITEMS - 2)
            ],
            declared_tools=bool(self.declared_tools()),
        )
        if not clean_task:
            # A refresh keeps the inventory current without inventing a task.
            return selection

        # Relevance comes from the request alone. Project signals ("video",
        # "python") and quality used to be enough to route every skill to
        # every task; they now only rank skills that already match.
        tokens = {
            token for token in tokenize(clean_task)
            if len(token) >= 3 and token not in _ROUTING_STOPWORDS
        }
        lowered = clean_task.casefold()

        scored_skills: list[tuple[float, dict[str, Any]]] = []
        for skill in skill_records:
            name = str(skill.get("name") or "")
            text = f"{name} {skill.get('description', '')}"
            text_tokens = set(tokenize(text))
            overlap = len(tokens & text_tokens)
            name_hit = bool(tokens & set(tokenize(name.replace("-", " "))))
            if overlap < MIN_SKILL_OVERLAP and not (name_hit and overlap >= 1):
                continue
            score = float(overlap * 3) + (2.0 if name_hit else 0.0)
            for signal in signals:
                if signal and signal.casefold() in text.casefold():
                    score += 1.5
            score += _as_float(skill.get("quality")) * 2.0
            score += min(5, int(skill.get("reused_count") or 0)) * 0.2
            if skill.get("managed"):
                score += 0.5
            scored_skills.append((score, skill))
        scored_skills.sort(key=lambda item: (-item[0], item[1].get("name", "")))
        selection.skills = [
            skill for _score, skill in scored_skills[: self.max_skills]
        ]

        catalog = self._tool_catalog()
        scored_tools: list[tuple[float, dict[str, Any]]] = []
        for tool in catalog:
            name = str(tool.get("name") or "")
            # Built-in tools match on their curated signals, not their prose
            # description ("Antigravity extension" matched every Antigravity
            # task). Declared tools may only have a description.
            parts = [name] + [str(item) for item in tool.get("signals") or ()]
            if tool.get("declared") or not tool.get("signals"):
                parts.append(str(tool.get("description") or ""))
            text = " ".join(parts)
            text_tokens = set(tokenize(text))
            score = float(len(tokens & text_tokens) * 3)
            if name.casefold() in lowered:
                score += 6
            for phrase in tool.get("signals") or ():
                phrase_text = str(phrase).casefold()
                if " " in phrase_text and phrase_text in lowered:
                    score += 4
            if tool.get("declared"):
                score += 4
            if tool.get("project"):
                score += 2
            if score > 0:
                scored_tools.append((score, tool))
        scored_tools.sort(key=lambda item: (-item[0], item[1].get("name", "")))
        selected = [tool for _score, tool in scored_tools[: self.max_tools]]
        if not selected:
            defaults = [
                tool for tool in catalog
                if str(tool.get("name")) in _DEFAULT_TOOLS
            ]
            selected = (defaults or catalog)[: self.max_tools]
        selection.tools = selected
        return selection

    def _tool_catalog(self) -> list[dict[str, Any]]:
        declared = self.declared_tools()
        catalog = [dict(tool) for tool in TOOL_CATALOG]
        if declared:
            # A project that declares tools owns the vocabulary. Built-ins are
            # kept only when the declaration names them again.
            return declared
        return catalog

    # ------------------------------------------------------------------ output
    def apply(self, task: str = "", dry_run: bool = False) -> RouteResult:
        selection = self.select(task)
        write = self.manager.set_routing(
            selection.task,
            selection.items(),
            target="workspace",
            dry_run=dry_run,
        )
        return RouteResult(selection=selection, write=write)

    def should_auto_apply(self) -> bool:
        return bool(self.enabled and self.auto_apply)
