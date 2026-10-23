"""
Command-line interface for the Harness² Antigravity Plugin.
"""

from __future__ import annotations
import argparse
import json
import shutil
import sys
from pathlib import Path

from . import __version__
from .core.config import (
    get_plugin_dir,
    get_memory_db_path,
    get_experience_md_path,
    get_workspace_rules_path,
    get_plugin_rules_path,
    resolve_transcript_path,
)
from .core.engine import HarnessEngine
from .core.memory_store import MemoryStore
from .core.skill_builder import SkillBuilder
from .core.agents_md_manager import AgentsMdManager


# Configure stdout encoding safely for Windows consoles
if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass


def cmd_status(args: argparse.Namespace) -> None:
    """Shows status of the Harness system, rules, skills, and memory."""
    print("=" * 60)
    print(f"[HARNESS] Harness2: Recursive Agent Harnessing (v{__version__})")
    print("=" * 60)
    plugin_dir = get_plugin_dir()
    print(f"Plugin Directory : {plugin_dir}")
    print(f"Memory Database  : {get_memory_db_path()}")
    print(f"Experience Log   : {get_experience_md_path()}")

    # Skills count
    builder = SkillBuilder()
    skills = builder.list_installed_skills()
    print(f"\n[+] Discovered Skills ({len(skills)}):")
    for s in skills:
        print(f"  - {s['name']}: {s['description'][:80]}...")

    # Memory Store stats
    store = MemoryStore()
    lessons = store.get_recent_lessons(5)
    print(f"\n[+] Recent Contrastive Lessons ({len(lessons)}):")
    for l in lessons:
        print(f"  - [{l['category']}] `{l['trigger_tool']}`: {l['resolution'][:80]}...")

    # Rules status
    ws_rules = get_workspace_rules_path()
    pl_rules = get_plugin_rules_path()
    print(f"\n[+] AGENTS.md Rules:")
    print(f"  - Workspace Rules: {'Exists' if ws_rules.exists() else 'Not yet created'} ({ws_rules})")
    print(f"  - Plugin Rules   : {'Exists' if pl_rules.exists() else 'Not yet created'} ({pl_rules})")
    print("=" * 60)


def cmd_reflect(args: argparse.Namespace) -> None:
    """Manually triggers the Propose-Probe-Compose cycle on a session."""
    print("🔄 Running Harness² Propose-Probe-Compose cycle...")
    engine = HarnessEngine(Path.cwd() if args.workspace else None)
    res = engine.run_cycle(
        conversation_id=args.session or "",
        transcript_path=args.transcript or None,
    )
    print(f"✅ {res.summary}")
    if res.skills_synthesized:
        print(f"   Built/Updated skills: {', '.join(res.skills_synthesized)}")


def cmd_skills(args: argparse.Namespace) -> None:
    """Lists or inspects synthesized skills."""
    builder = SkillBuilder()
    skills = builder.list_installed_skills()

    if args.name:
        for s in skills:
            if s["name"] == args.name:
                print(f"--- Skill: {s['name']} ---")
                p = Path(s["path"])
                if p.exists():
                    print(p.read_text(encoding="utf-8"))
                return
        print(f"Error: Skill '{args.name}' not found.")
        sys.exit(1)

    print(f"Indexed Harness Skills ({len(skills)} total):")
    for s in skills:
        print(f"\n- {s['name']}")
        print(f"  Description: {s['description']}")
        print(f"  Path: {s['path']}")


def cmd_sync(args: argparse.Namespace) -> None:
    """Syncs the experience library and regenerates EXPERIENCE.md."""
    store = MemoryStore()
    store.sync_experience_md()
    print(f"[+] Synchronized EXPERIENCE.md at: {get_experience_md_path()}")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Harness²: Recursive Agent Harnessing CLI for Antigravity"
    )
    subparsers = parser.add_subparsers(dest="command")

    # status
    p_status = subparsers.add_parser("status", help="Show system status and learning metrics")
    p_status.set_defaults(func=cmd_status)

    # reflect
    p_reflect = subparsers.add_parser("reflect", help="Run self-improvement reflection on a session")
    p_reflect.add_argument("--session", "-s", help="Conversation ID")
    p_reflect.add_argument("--transcript", "-t", help="Path to transcript.jsonl")
    p_reflect.add_argument("--workspace", "-w", action="store_true", help="Target active workspace")
    p_reflect.set_defaults(func=cmd_reflect)

    # skills
    p_skills = subparsers.add_parser("skills", help="List or inspect skills")
    p_skills.add_argument("--name", "-n", help="Specific skill name to inspect")
    p_skills.set_defaults(func=cmd_skills)

    # sync
    p_sync = subparsers.add_parser("sync", help="Sync EXPERIENCE.md memory log")
    p_sync.set_defaults(func=cmd_sync)

    args = parser.parse_args()
    if hasattr(args, "func"):
        args.func(args)
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
