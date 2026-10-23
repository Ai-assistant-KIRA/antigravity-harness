"""
Configuration and paths for the Harness plugin system.
"""

from __future__ import annotations
import os
import sys
from pathlib import Path

# Base user gemini config directory
USER_HOME = Path.home()
GEMINI_CONFIG_DIR = USER_HOME / ".gemini" / "config"
GLOBAL_PLUGIN_DIR = GEMINI_CONFIG_DIR / "plugins" / "harness"
GLOBAL_SKILLS_DIR = GLOBAL_PLUGIN_DIR / "skills"
GLOBAL_MEMORY_DIR = GLOBAL_PLUGIN_DIR / "memory"
GLOBAL_RULES_DIR = GLOBAL_PLUGIN_DIR / "rules"

# Antigravity runtime environment variables
APP_DATA_DIR = Path(os.environ.get("ANTIGRAVITY_APP_DATA_DIR", USER_HOME / ".gemini" / "antigravity"))
CURRENT_CONVERSATION_ID = os.environ.get("ANTIGRAVITY_CONVERSATION_ID", "")
CURRENT_PROJECT_ID = os.environ.get("ANTIGRAVITY_PROJECT_ID", "")


def get_plugin_dir() -> Path:
    """Returns the plugin install directory (global by default, or overridden by env)."""
    env_dir = os.environ.get("HARNESS_PLUGIN_DIR")
    if env_dir:
        return Path(env_dir)
    return GLOBAL_PLUGIN_DIR


def get_memory_db_path() -> Path:
    """Returns the SQLite database path for persistent memory."""
    mem_dir = get_plugin_dir() / "memory"
    mem_dir.mkdir(parents=True, exist_ok=True)
    return mem_dir / "harness_memory.db"


def get_experience_md_path() -> Path:
    """Returns the path to the human-readable EXPERIENCE.md memory log."""
    mem_dir = get_plugin_dir() / "memory"
    mem_dir.mkdir(parents=True, exist_ok=True)
    return mem_dir / "EXPERIENCE.md"


def get_workspace_rules_path(workspace_root: Path | None = None) -> Path:
    """Returns the workspace AGENTS.md rule file path."""
    if workspace_root:
        return workspace_root / "AGENTS.md"
    return Path.cwd() / "AGENTS.md"


def get_plugin_rules_path() -> Path:
    """Returns the plugin's global AGENTS.md rule file path."""
    rules_dir = get_plugin_dir() / "rules"
    rules_dir.mkdir(parents=True, exist_ok=True)
    return rules_dir / "AGENTS.md"


def get_skills_dirs(workspace_root: Path | None = None) -> list[Path]:
    """Returns all directories where skills can be placed for progressive disclosure."""
    dirs = []
    # 1. Plugin skills directory
    plugin_skills = get_plugin_dir() / "skills"
    plugin_skills.mkdir(parents=True, exist_ok=True)
    dirs.append(plugin_skills)

    # 2. Workspace skills directory (.agents/skills) if in a workspace
    if workspace_root:
        ws_skills = workspace_root / ".agents" / "skills"
        dirs.append(ws_skills)
    else:
        cwd_skills = Path.cwd() / ".agents" / "skills"
        if cwd_skills.exists():
            dirs.append(cwd_skills)

    # 3. Global skills directory (~/.gemini/config/skills)
    global_user_skills = GEMINI_CONFIG_DIR / "skills"
    if global_user_skills.exists():
        dirs.append(global_user_skills)

    return dirs


def resolve_transcript_path(conversation_id: str = "", provided_path: str = "") -> Path | None:
    """Finds the transcript JSONL file for a conversation."""
    if provided_path and Path(provided_path).exists():
        return Path(provided_path)

    cid = conversation_id or CURRENT_CONVERSATION_ID
    if not cid:
        return None

    # Check common Antigravity transcript locations:
    candidates = [
        APP_DATA_DIR / "brain" / cid / ".system_generated" / "logs" / "transcript.jsonl",
        APP_DATA_DIR / "brain" / cid / ".system_generated" / "logs" / "transcript_full.jsonl",
        APP_DATA_DIR / "brain" / cid / "transcript.jsonl",
        APP_DATA_DIR / cid / "transcript.jsonl",
    ]
    for p in candidates:
        if p.exists():
            return p
    return None
