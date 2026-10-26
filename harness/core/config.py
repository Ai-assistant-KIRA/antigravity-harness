"""
Configuration, path discovery, and workspace helpers for Harness².

All paths can be overridden with environment variables so the plugin can be
tested in isolation and deployed to non-default Antigravity installations.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from pathlib import Path
from typing import Any, Iterable

USER_HOME = Path.home()
# Initial values are kept for backward compatibility with v1 imports. Functions
# below read the environment dynamically so tests and alternate installations
# can be isolated without reloading the module.
GEMINI_CONFIG_DIR = Path(os.environ.get("HARNESS_GEMINI_CONFIG_DIR", USER_HOME / ".gemini" / "config")).expanduser()
GLOBAL_PLUGIN_DIR = Path(os.environ.get("HARNESS_PLUGIN_DIR", GEMINI_CONFIG_DIR / "plugins" / "harness")).expanduser()
GLOBAL_SKILLS_DIR = GLOBAL_PLUGIN_DIR / "skills"
GLOBAL_MEMORY_DIR = GLOBAL_PLUGIN_DIR / "memory"
GLOBAL_RULES_DIR = GLOBAL_PLUGIN_DIR / "rules"
GLOBAL_STATE_DIR = GLOBAL_PLUGIN_DIR / "state"

CURRENT_CONVERSATION_ID = os.environ.get("ANTIGRAVITY_CONVERSATION_ID", "")
CURRENT_PROJECT_ID = os.environ.get("ANTIGRAVITY_PROJECT_ID", "")


def get_gemini_config_dir() -> Path:
    return _env_path("HARNESS_GEMINI_CONFIG_DIR", USER_HOME / ".gemini" / "config")


def _app_data_candidates(include_missing: bool = False) -> list[Path]:
    candidates = [
        Path(os.environ["ANTIGRAVITY_APP_DATA_DIR"]).expanduser()
        if os.environ.get("ANTIGRAVITY_APP_DATA_DIR")
        else None,
        USER_HOME / ".gemini" / "antigravity",
        USER_HOME / ".gemini" / "antigravity-ide",
        USER_HOME / ".gemini" / "antigravity-cli",
    ]
    unique: list[Path] = []
    for candidate in candidates:
        if candidate and candidate not in unique and (include_missing or candidate.exists()):
            unique.append(candidate)
    return unique


APP_DATA_DIR = next(
    iter(_app_data_candidates()),
    USER_HOME / ".gemini" / "antigravity",
)


def _env_path(name: str, default: Path) -> Path:
    raw = os.environ.get(name)
    return Path(raw).expanduser() if raw else default


def get_plugin_dir() -> Path:
    """Return the plugin install directory (global by default)."""
    return _env_path("HARNESS_PLUGIN_DIR", get_gemini_config_dir() / "plugins" / "harness")


def get_memory_dir(create: bool = True) -> Path:
    path = get_plugin_dir() / "memory"
    if create:
        path.mkdir(parents=True, exist_ok=True)
    return path


def get_state_dir(create: bool = True) -> Path:
    path = get_plugin_dir() / "state"
    if create:
        path.mkdir(parents=True, exist_ok=True)
    return path


def get_memory_db_path() -> Path:
    return get_memory_dir(create=False) / "harness_memory.db"


def get_project_memory_dir(
    workspace_root: Path | str | None, create: bool = False
) -> Path:
    """Per-project memory directory, isolated by workspace fingerprint.

    Project data lives under the plugin's memory directory rather than inside
    the user's repository. That keeps ``git status`` clean while still giving
    every project a physically separate database, experience log, and future
    exports.
    """
    fingerprint = project_fingerprint(workspace_root)
    path = get_memory_dir(create=False) / "projects" / fingerprint
    if create:
        path.mkdir(parents=True, exist_ok=True)
    return path


def get_project_memory_db_path(workspace_root: Path | str | None) -> Path:
    return get_project_memory_dir(workspace_root) / "harness_memory.db"


def get_project_memory_db_paths() -> list[Path]:
    """Every existing per-project database (used by all-project queries)."""
    root = get_memory_dir(create=False) / "projects"
    if not root.is_dir():
        return []
    try:
        return sorted(root.glob("*/harness_memory.db"))
    except OSError:
        return []


def get_project_experience_md_path(workspace_root: Path | str | None) -> Path:
    return get_project_memory_dir(workspace_root) / "EXPERIENCE.md"


def get_experience_md_path() -> Path:
    return get_memory_dir(create=False) / "EXPERIENCE.md"


def get_lesson_id_counter_path() -> Path:
    """Shared monotonic lesson-id counter.

    Project databases are physically isolated, so SQLite's per-database
    AUTOINCREMENT would hand out the same ids in every project. Lesson ids are
    user-facing (``harness memory show <id>``), so they must stay unique across
    the global store and every project store.
    """
    return get_state_dir() / "lesson_ids.seq"


def get_workspace_tools_path(workspace_root: Path | str | None = None) -> Path:
    """Project-declared tool routing file (optional)."""
    root = Path(workspace_root).expanduser() if workspace_root else Path.cwd()
    return root / ".agents" / "tools.json"


def _setting(section: str, key: str, default: Any) -> Any:
    """Read one setting without making config import settings at module load."""
    try:
        from .settings import get_setting, load_settings

        settings, _warnings = load_settings()
        return get_setting(settings, f"{section}.{key}", default)
    except Exception:
        return default


def _env_flag(name: str) -> bool | None:
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return None
    return raw.strip().lower() not in ("0", "false", "no", "off", "disabled")


def project_isolation_enabled() -> bool:
    """Whether project memory/experience should be stored per project."""
    override = _env_flag("HARNESS_PROJECT_ISOLATION")
    if override is not None:
        return override
    return bool(_setting("project", "isolate_memory", True))


def project_skills_only() -> bool:
    """Whether skill discovery is limited to the current workspace."""
    override = _env_flag("HARNESS_PROJECT_SKILLS_ONLY")
    if override is not None:
        return override
    return bool(_setting("project", "project_skills_only", True))


def get_effective_memory_db_path(workspace_root: Path | str | None) -> Path:
    if workspace_root and project_isolation_enabled():
        return get_project_memory_db_path(workspace_root)
    return get_memory_db_path()


def get_effective_experience_md_path(workspace_root: Path | str | None) -> Path:
    if workspace_root and project_isolation_enabled():
        return get_project_experience_md_path(workspace_root)
    return get_experience_md_path()


def get_hook_error_log_path() -> Path:
    return get_state_dir() / "hook_errors.log"


def get_event_journal_path() -> Path:
    return get_state_dir() / "events.jsonl"


def get_workspace_rules_path(workspace_root: Path | None = None) -> Path:
    root = Path(workspace_root).expanduser() if workspace_root else Path.cwd()
    return root / "AGENTS.md"


def get_plugin_rules_path(create: bool = True) -> Path:
    path = get_plugin_dir() / "rules"
    if create:
        path.mkdir(parents=True, exist_ok=True)
    return path / "AGENTS.md"


def get_workspace_skills_dir(workspace_root: Path | None = None) -> Path:
    root = Path(workspace_root).expanduser() if workspace_root else Path.cwd()
    return root / ".agents" / "skills"


def get_global_user_skills_dir() -> Path:
    return get_gemini_config_dir() / "skills"


def get_skills_dirs(workspace_root: Path | None = None) -> list[Path]:
    """All directories Antigravity may discover skills from.

    Workspace skills intentionally come first so project-specific learning wins
    over a stale global copy with the same name.
    """
    dirs: list[Path] = []
    if workspace_root:
        dirs.append(get_workspace_skills_dir(workspace_root))
    else:
        cwd_skills = get_workspace_skills_dir(Path.cwd())
        if cwd_skills.exists():
            dirs.append(cwd_skills)
    dirs.append(get_plugin_dir() / "skills")
    global_user = get_global_user_skills_dir()
    if global_user.exists():
        dirs.append(global_user)
    return dirs


def get_primary_skills_dir(workspace_root: Path | None = None) -> Path:
    """Where newly synthesized skills are written by default.

    Project learnings should stay with the project. Only sessions without a
    workspace fall back to the global plugin skill library.
    """
    if workspace_root:
        return get_workspace_skills_dir(workspace_root)
    return get_plugin_dir() / "skills"


def get_app_data_dirs() -> list[Path]:
    return _app_data_candidates(include_missing=False)


def safe_conversation_id(value: str | None) -> str:
    """Return a filesystem-safe conversation identifier.

    Antigravity normally supplies a UUID. Anything containing separators,
    control characters, or path traversal is hashed so it can never escape the
    transcript root. Idempotence is preserved because the mapping is stable.
    """
    text = str(value or "").strip()
    if not text:
        return ""
    if (
        len(text) <= 128
        and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", text)
        and ".." not in text
    ):
        return text
    return "conversation-" + hashlib.sha1(
        text.encode("utf-8", errors="replace")
    ).hexdigest()[:24]


def resolve_transcript_path(
    conversation_id: str = "",
    provided_path: str | Path = "",
    app_data_dir: Path | None = None,
) -> Path | None:
    """Find the newest transcript for a conversation across Antigravity surfaces."""
    if provided_path:
        candidate = Path(provided_path).expanduser()
        if candidate.exists() and candidate.is_file():
            return candidate

    cid = safe_conversation_id(
        conversation_id or os.environ.get("ANTIGRAVITY_CONVERSATION_ID", "")
    )
    if not cid:
        return None

    roots = [Path(app_data_dir).expanduser()] if app_data_dir else get_app_data_dirs()
    names = ("transcript_full.jsonl", "transcript.jsonl")
    candidates: list[Path] = []
    for root in roots:
        for name in names:
            candidates.append(root / "brain" / cid / ".system_generated" / "logs" / name)
            candidates.append(root / "brain" / cid / name)
            candidates.append(root / cid / name)
            candidates.append(root / "conversations" / cid / name)

    existing = [p for p in candidates if p.exists() and p.is_file()]
    if existing:
        return max(existing, key=lambda p: p.stat().st_mtime)

    # Fallback for unusual layouts: a shallow glob on the conversation id.
    for root in roots:
        brain = root / "brain" / cid
        if brain.exists():
            matches = sorted(
                (p for p in brain.rglob("transcript*.jsonl") if p.is_file()),
                key=lambda p: p.stat().st_mtime,
                reverse=True,
            )
            if matches:
                return matches[0]
    return None


def find_workspace_root(start: Path | str | None = None) -> Path:
    """Walk upward to the nearest repository/workspace boundary."""
    if not start:
        return Path.cwd()
    path = Path(start).expanduser()
    try:
        path = path.resolve()
    except OSError:
        path = path.absolute()
    if path.is_file():
        path = path.parent
    markers = (".git", "pyproject.toml", "package.json", "Cargo.toml", "go.mod", "AGENTS.md")
    current = path
    for _ in range(20):
        if any((current / marker).exists() for marker in markers):
            return current
        if current.parent == current:
            break
        current = current.parent
    return path


def project_fingerprint(workspace_root: Path | str | None) -> str:
    """Stable, privacy-preserving identifier for a workspace."""
    if not workspace_root:
        return "global"
    try:
        normalized = str(Path(workspace_root).expanduser().resolve()).lower()
    except OSError:
        normalized = str(Path(workspace_root).expanduser().absolute()).lower()
    return hashlib.sha1(normalized.encode("utf-8", errors="replace")).hexdigest()[:16]


def detect_project_signals(workspace_root: Path | str | None) -> list[str]:
    """Cheap, offline detection of the stack for skill/memory relevance ranking."""
    if not workspace_root:
        return []
    root = Path(workspace_root)
    signals: set[str] = set()

    def add(value: str) -> None:
        value = value.strip().lower()
        if value and len(value) <= 40:
            signals.add(value)

    package_json = root / "package.json"
    if package_json.exists():
        add("javascript")
        add("node")
        try:
            data = json.loads(package_json.read_text(encoding="utf-8", errors="replace"))
            deps = {}
            for key in ("dependencies", "devDependencies", "peerDependencies"):
                value = data.get(key)
                if isinstance(value, dict):
                    deps.update(value)
            for name in deps:
                add(name)
                base = name.split("/")[-1]
                if base.startswith("@"):
                    continue
                add(base)
            for framework in ("next", "react", "vue", "svelte", "vite", "angular", "astro"):
                if any(framework in dep.lower() for dep in deps):
                    add(framework)
        except Exception:
            pass

    pyproject = root / "pyproject.toml"
    requirements = root / "requirements.txt"
    if pyproject.exists() or requirements.exists():
        add("python")
        text = ""
        try:
            if pyproject.exists():
                text += pyproject.read_text(encoding="utf-8", errors="replace")
            if requirements.exists():
                text += "\n" + requirements.read_text(encoding="utf-8", errors="replace")
        except Exception:
            pass
        lowered = text.lower()
        for framework in (
            "fastapi", "django", "flask", "pytest", "pydantic", "sqlalchemy",
            "alembic", "uv", "poetry", "ruff", "mypy", "celery", "pandas",
        ):
            if framework in lowered:
                add(framework)

    if (root / "go.mod").exists():
        add("go")
    if (root / "Cargo.toml").exists():
        add("rust")
        try:
            text = (root / "Cargo.toml").read_text(encoding="utf-8", errors="replace").lower()
            for crate in ("tokio", "axum", "actix", "serde", "clap"):
                if crate in text:
                    add(crate)
        except Exception:
            pass
    if (root / "Dockerfile").exists() or (root / "docker-compose.yml").exists() or (root / "compose.yaml").exists():
        add("docker")
    if (root / "playwright.config.ts").exists() or (root / "playwright.config.js").exists():
        add("playwright")
    if (root / ".github" / "workflows").exists():
        add("github-actions")

    return sorted(signals)


def tokenize(text: str) -> list[str]:
    """Split text into lowercase search tokens, dropping syntax noise."""
    tokens: list[str] = []
    for raw in re.findall(r"[A-Za-z0-9][A-Za-z0-9_+.#-]*", (text or "").lower()):
        token = raw.strip(".-#+")
        if len(token) < 3 or token.isdigit():
            continue
        if token in _STOPWORDS or token in tokens:
            continue
        tokens.append(token)
    return tokens


_STOPWORDS = {
    "the", "and", "for", "with", "that", "this", "from", "into", "your", "you",
    "please", "can", "could", "would", "should", "make", "sure", "use", "using",
    "when", "then", "than", "also", "have", "has", "are", "was", "were", "will",
    "not", "but", "all", "any", "our", "its", "their", "them", "they", "task",
    "work", "working", "code", "project", "file", "files", "run", "running",
    "set", "setup", "suite", "create", "creating", "please", "help", "want",
    "need", "doing", "done", "new", "via", "per", "each", "some", "still",
}


def slugify(text: str, max_length: int = 48) -> str:
    cleaned = re.sub(r"[^a-zA-Z0-9]+", "-", (text or "").lower()).strip("-")
    cleaned = re.sub(r"-{2,}", "-", cleaned)
    return (cleaned[:max_length].rstrip("-") or "harness-skill")


def unique_preserve(items: Iterable[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for item in items:
        key = item.strip().lower()
        if key and key not in seen:
            seen.add(key)
            result.append(item.strip())
    return result
