"""
Command-line interface for the Harness² Antigravity plugin.

The CLI is safe to run from any working directory. When installed as a plugin,
use ``bin/harness.cmd`` (Windows) or ``bin/harness`` (POSIX); from a checkout,
``python scripts/harness_cli.py`` also works.
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path
from typing import Any

from . import __version__
from .core.agents_md_manager import AgentsMdManager
from .core.config import (
    get_experience_md_path,
    get_memory_db_path,
    get_plugin_dir,
    get_plugin_rules_path,
    get_workspace_rules_path,
)
from .core.engine import HarnessEngine
from .core.memory_store import MemoryStore
from .core.retrieval import find_recall, format_recall_message
from .core.router import TaskRouter
from .core.settings import get_setting, load_settings
from .core.skill_builder import SkillBuilder, SkillError
from .core.skill_diagnostics import diagnose_skill_generation
from .core.trust import audit_privacy
from .core.util import jaccard, normalize_for_compare, one_line

# Keep in sync with PLUGIN_MANIFEST_FIELDS in install_plugin.py.
PLUGIN_MANIFEST_FIELDS = {
    "$schema",
    "name",
    "displayName",
    "display_name",
    "description",
    "version",
    "disabled",
    "logo",
    "suggestedPrompts",
    "suggested_prompts",
    "skills",
    "author",
    "homepage",
    "repository",
    "license",
    "keywords",
    "extensions",
    "source",
    "sourcePath",
    "source_path",
}

# Configure the console safely on Windows.
if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
if hasattr(sys.stderr, "reconfigure"):
    try:
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass


# ----------------------------------------------------------------------- helpers
def _workspace(args: argparse.Namespace) -> Path | None:
    if getattr(args, "global_scope", False):
        return None
    value = getattr(args, "workspace", None)
    if value is None:
        return None
    if value is True:
        return Path.cwd().resolve()
    return Path(str(value)).expanduser().resolve()


def _echo(data: Any, as_json: bool = False) -> None:
    if as_json:
        print(json.dumps(data, indent=2, ensure_ascii=False, default=str))
    else:
        print(data)


def _add_workspace(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--workspace",
        "-w",
        nargs="?",
        const=".",
        default=None,
        metavar="PATH",
        help="Workspace root to inspect (defaults to the current directory for reflect).",
    )


def _skill_to_dict(skill: dict[str, Any]) -> dict[str, Any]:
    return {
        key: value
        for key, value in skill.items()
        if key in (
            "name", "description", "path", "folder", "scope", "managed",
            "valid", "errors", "warnings", "quality", "source", "duplicates",
        )
    }


# ------------------------------------------------------------------------ status
def cmd_status(args: argparse.Namespace) -> None:
    workspace = _workspace(args)
    plugin = get_plugin_dir()
    store = MemoryStore(workspace=workspace)
    builder = SkillBuilder(workspace)
    stats = store.get_stats(workspace, all_projects=workspace is None)
    skills = builder.list_installed_skills(workspace)
    lessons = store.get_recent_lessons(
        limit=8, workspace=workspace, all_projects=workspace is None
    )
    rules = AgentsMdManager(workspace)

    effectiveness = None
    if getattr(args, "effectiveness", False):
        ranked_lessons = store.list_lessons(
            limit=200,
            workspace=workspace,
            include_candidates=True,
            all_projects=workspace is None,
        )
        ranked_lessons.sort(
            key=lambda lesson: (
                lesson.get("utility", MemoryStore.lesson_utility(lesson)),
                str(lesson.get("last_seen") or ""),
            ),
            reverse=True,
        )
        ranked_skills = store.get_all_skills(workspace)
        for skill in ranked_skills:
            skill["utility"] = MemoryStore.skill_utility(skill)
        ranked_skills.sort(
            key=lambda skill: (
                skill.get("utility", 0.0),
                int(skill.get("reused_count") or 0),
                str(skill.get("updated_at") or ""),
            ),
            reverse=True,
        )
        effectiveness = {
            "lessons": ranked_lessons[:5],
            "skills": ranked_skills[:5],
        }

    if args.json:
        payload = {
            "version": __version__,
            "plugin_dir": str(plugin),
            "workspace": str(workspace) if workspace else "all projects",
            "stats": stats,
            "skills": [_skill_to_dict(skill) for skill in skills],
            "lessons": lessons,
            "rules": {
                "workspace": {
                    "path": str(rules.workspace_agents_md),
                    "exists": rules.workspace_agents_md.exists(),
                    "bytes": rules.workspace_agents_md.stat().st_size
                    if rules.workspace_agents_md.exists() else 0,
                },
                "plugin": {
                    "path": str(rules.plugin_agents_md),
                    "exists": rules.plugin_agents_md.exists(),
                    "bytes": rules.plugin_agents_md.stat().st_size
                    if rules.plugin_agents_md.exists() else 0,
                },
            },
        }
        if effectiveness is not None:
            payload["effectiveness"] = effectiveness
        _echo(payload, True)
        return

    print("=" * 68)
    print(f"[HARNESS] Harness²: Recursive Agent Harnessing (v{__version__})")
    print("=" * 68)
    print(f"Plugin directory : {plugin}")
    print(f"Scope            : {workspace if workspace else 'all projects'}")
    print(f"Memory database  : {stats['db_path']}")
    print(f"Experience log   : {stats['experience_path']}")
    print(f"FTS search       : {'enabled' if stats['fts'] else 'LIKE fallback'}")
    print(
        f"Memory           : {stats['sessions']} sessions, "
        f"{stats['verified_lessons']} verified lessons, "
        f"{stats['candidate_lessons']} candidates"
    )
    print(f"Rule edits       : {stats['rule_edits']}")
    print()

    print(f"[+] Skills ({len(skills)}):")
    if not skills:
        print("  - none yet")
    for skill in skills[:12]:
        flag = "" if skill.get("valid") else " [INVALID]"
        print(f"  - {skill['name']} ({skill['scope']}){flag}: {one_line(skill['description'], 100)}")
    if len(skills) > 12:
        print(f"  - ...and {len(skills) - 12} more")
    print()

    print(f"[+] Recent verified lessons ({len(lessons)}):")
    if not lessons:
        print("  - none yet")
    for lesson in lessons:
        print(
            f"  - [{lesson['category']}] {lesson['trigger_tool']} "
            f"({lesson['occurrences']}x): {one_line(lesson['resolution'], 110)}"
        )
    print()

    if effectiveness is not None:
        top_lessons = effectiveness["lessons"]
        print(f"[+] Lesson effectiveness (top {len(top_lessons)}):")
        if not top_lessons:
            print("  - none yet")
        for lesson in top_lessons:
            marker = "verified" if lesson.get("verified") else "candidate"
            print(
                f"  - [{marker}] {lesson['trigger_tool']} "
                f"confidence {float(lesson.get('confidence') or 0):.2f}, "
                f"occurrences {lesson['occurrences']}, "
                f"surfaced {lesson.get('surfaced_count', 0)}, "
                f"recurred {lesson.get('post_surface_occurrences', 0)}, "
                f"utility {float(lesson.get('utility') or 0):.2f}: "
                f"{one_line(lesson.get('resolution', ''), 90)}"
            )
        print()

        top_skills = effectiveness["skills"]
        print(f"[+] Skill effectiveness (top {len(top_skills)}):")
        if not top_skills:
            print("  - none yet")
        for skill in top_skills:
            print(
                f"  - {skill['name']} ({skill.get('scope', 'global')}) "
                f"reuse {skill.get('reused_count', 0)}, "
                f"surfaced {skill.get('surfaced_count', 0)}, "
                f"utility {float(skill.get('utility') or 0):.2f}"
            )
        print()

    for label, path in (
        ("Workspace rules", rules.workspace_agents_md),
        ("Global rules", rules.plugin_agents_md),
    ):
        if path.exists():
            print(f"[+] {label}: {path} ({path.stat().st_size} bytes)")
        else:
            print(f"[+] {label}: not created ({path})")
    print("=" * 68)


# ----------------------------------------------------------------------- reflect
def cmd_reflect(args: argparse.Namespace) -> None:
    workspace = _workspace(args)
    if workspace is None and not args.global_scope:
        workspace = Path.cwd().resolve()
    engine = HarnessEngine(workspace)
    result = engine.run_cycle(
        conversation_id=args.session or "",
        transcript_path=args.transcript or None,
        workspace_path=workspace,
        force=args.force,
        lock_timeout=15.0,
    )
    if args.json:
        _echo(result.__dict__, True)
        return
    print(f"[{result.status.upper()}] {result.summary}")
    if result.conversation_id:
        print(f"  conversation : {result.conversation_id}")
    if result.transcript_hash:
        print(f"  transcript   : {result.transcript_hash[:16]}")
    if result.skills_synthesized:
        print(f"  new skills   : {', '.join(result.skills_synthesized)}")
    if result.skills_updated:
        print(f"  updated      : {', '.join(result.skills_updated)}")
    if result.skills_skipped:
        for item in result.skills_skipped:
            print(f"  skipped      : {item['name']} ({item['reason']})")
    if result.skill_reuse:
        print(f"  skills reused: {', '.join(result.skill_reuse)}")
    if result.routing:
        routed_skills = result.routing.get("skills") or []
        routed_tools = result.routing.get("tools") or []
        print(
            "  routing      : "
            f"{len(routed_skills)} skill(s), {len(routed_tools)} tool(s) "
            f"-> {result.routing.get('path')}"
        )
    for warning in result.warnings:
        print(f"  warning      : {warning}")
    if result.status == "no_transcript":
        sys.exit(2)


# ------------------------------------------------------------------------ skills
def cmd_skills(args: argparse.Namespace) -> None:
    action = args.action or "list"
    workspace = _workspace(args)
    builder = SkillBuilder(workspace)
    store = MemoryStore(workspace=workspace)

    if action in ("why", "diagnose"):
        report = diagnose_skill_generation(
            workspace=workspace,
            limit=max(1, int(getattr(args, "limit", None) or 10)),
            memory=store,
        )
        if args.json:
            _echo(report, True)
            return
        print("=" * 68)
        print("[HARNESS] Why skills were (or were not) generated")
        print("=" * 68)
        print(f"Scope        : {report['workspace'] or 'all projects'}")
        print(f"Min quality  : {report['min_quality']:.2f}")
        print(f"Skills index : {report['skills_indexed']} harness-managed skill(s)")
        print(f"Eligible now : {report['eligible']} workflow(s) ready to write")
        if not report["sessions"]:
            print("\nNo recorded sessions yet. Run a task in Antigravity first.")
            return
        for entry in report["sessions"]:
            scope = Path(entry["workspace"]).name if entry["workspace"] else "global"
            print()
            print(
                f"- {entry['conversation_id'][:8]} | {entry['updated_at'][:16]} | "
                f"{scope} | outcome={entry['outcome'] or 'unknown'} | "
                f"tasks={entry['tasks']}"
            )
            if entry["task_summary"]:
                print(f"    prompt: {entry['task_summary']}")
            if entry.get("error"):
                print(f"    ! {entry['error']}")
                continue
            workflows = entry["workflows"]
            if not workflows:
                print("    no task segment reached the workflow extractor")
            for workflow in workflows:
                label = workflow.get("name") or "unnamed"
                detail = f"quality {float(workflow.get('quality') or 0):.2f}"
                if workflow.get("steps"):
                    detail += f", {workflow['steps']} step(s)"
                print(f"    * {label} ({detail})")
                print(f"      task  : {one_line(str(workflow.get('task') or ''), 120)}")
                print(f"      verdict: {workflow.get('verdict')}")
        print("=" * 68)
        print("Note: skills you installed manually are not tracked here.")
        return

    if action in ("list", "ls"):
        skills = builder.list_installed_skills(workspace)
        if args.name and not args.target:
            action = "show"
        elif args.target and not args.name:
            args.name = args.target
            action = "show"
        else:
            if args.json:
                _echo([_skill_to_dict(skill) for skill in skills], True)
                return
            print(f"Skills ({len(skills)}):")
            if not skills:
                print("  - none yet")
                return
            for skill in skills:
                flag = "" if skill.get("valid") else f" [INVALID: {'; '.join(skill.get('errors', []))}]"
                print(f"\n- {skill['name']} ({skill['scope']}){flag}")
                print(f"  description: {one_line(skill['description'], 220)}")
                print(f"  path: {skill['path']}")
                if skill.get("duplicates"):
                    print(f"  shadowed copies: {len(skill['duplicates'])}")
            return

    if action in ("show", "inspect"):
        name = args.name or args.target
        if not name:
            print("Error: --name is required", file=sys.stderr)
            sys.exit(1)
        for skill in builder.list_installed_skills(workspace):
            if skill["name"] == name:
                if args.json:
                    _echo(skill, True)
                else:
                    print(Path(skill["path"]).read_text(encoding="utf-8", errors="replace"))
                return
        print(f"Error: skill '{name}' was not found.", file=sys.stderr)
        sys.exit(1)

    if action == "install":
        source = args.source or args.target
        if not source:
            print("Error: provide a local path, GitHub repo, or URL.", file=sys.stderr)
            sys.exit(1)
        scope = args.scope or ("workspace" if workspace else "global")
        if scope in ("workspace", "project") and not workspace:
            print("Error: workspace scope needs --workspace PATH.", file=sys.stderr)
            sys.exit(1)
        try:
            results = builder.install_skill(
                source,
                name=args.name,
                scope=scope,
                workspace_path=workspace,
                force=args.force,
                all_skills=args.all,
                timeout=args.timeout,
            )
        except SkillError as exc:
            print(f"Error: {exc}", file=sys.stderr)
            sys.exit(1)
        payloads: list[dict[str, Any]] = []
        for result in results:
            if result.status != "unchanged":
                store.record_skill_saved(
                    name=result.name,
                    description=builder.read_skill(result.path).get("description", ""),
                    file_path=str(result.path),
                    workspace=workspace if scope == "workspace" else None,
                    scope="workspace" if scope == "workspace" else ("user" if scope == "user" else "plugin"),
                    source="external",
                )
            payloads.append(
                {
                    "name": result.name,
                    "path": str(result.path),
                    "status": result.status,
                    "source": result.source,
                    "warnings": result.warnings,
                }
            )
            if not args.json:
                print(f"[{result.status.upper()}] {result.name} -> {result.path}")
                for warning in result.warnings:
                    print(f"  warning: {warning}")
        if args.json:
            _echo(payloads, True)
        return

    if action == "remove":
        name = args.name or args.target
        if not name:
            print("Error: skill name is required.", file=sys.stderr)
            sys.exit(1)
        if not args.yes:
            if args.json:
                print("Error: --yes is required to remove a skill in JSON mode.", file=sys.stderr)
                sys.exit(1)
            try:
                answer = input(f"Remove skill '{name}'? [y/N] ").strip().lower()
            except EOFError:
                answer = "n"
            if answer not in ("y", "yes"):
                print("Cancelled.")
                return
        removed = builder.remove_skill(name, scope=args.scope, workspace_path=workspace)
        for path in removed:
            deleted = store.delete_skill_record(
                name, file_path=str(Path(path) / "SKILL.md")
            )
            if not deleted:
                store.delete_skill_record(name, file_path=str(Path(path)))
        if args.json:
            _echo({"removed": removed}, True)
        elif removed:
            for path in removed:
                print(f"[REMOVED] {path}")
        else:
            print(f"No matching skill '{name}' found.", file=sys.stderr)
            sys.exit(1)
        return

    if action in ("validate", "check"):
        skills = builder.list_installed_skills(workspace)
        invalid = [skill for skill in skills if not skill.get("valid")]
        if args.json:
            _echo(
                {
                    "checked": len(skills),
                    "invalid": [_skill_to_dict(skill) for skill in invalid],
                    "skills": [_skill_to_dict(skill) for skill in skills],
                },
                True,
            )
        else:
            print(f"Validated {len(skills)} skill(s); {len(invalid)} invalid.")
            for skill in invalid:
                print(f"  - {skill['name']}: {'; '.join(skill.get('errors', []))}")
        if invalid:
            sys.exit(1)
        return

    print(f"Unknown skills action: {action}", file=sys.stderr)
    sys.exit(1)


# ------------------------------------------------------------------------ memory
def _lesson_id(args: argparse.Namespace) -> int:
    if not args.terms:
        print("Error: a lesson id is required.", file=sys.stderr)
        sys.exit(1)
    try:
        return int(str(args.terms[0]).strip())
    except (TypeError, ValueError):
        print(
            f"Error: lesson id must be an integer (got '{args.terms[0]}').",
            file=sys.stderr,
        )
        sys.exit(1)


def cmd_memory(args: argparse.Namespace) -> None:
    action = args.action or "stats"
    workspace = _workspace(args)
    store = MemoryStore(workspace=workspace)

    if action in ("stats", "status"):
        stats = store.get_stats(workspace, all_projects=args.all_projects)
        if args.json:
            _echo(stats, True)
        else:
            for key, value in stats.items():
                print(f"{key}: {value}")
        return

    if action in ("search", "find"):
        if not args.terms:
            print("Error: a search query is required.", file=sys.stderr)
            sys.exit(1)
        results = store.search_lessons(
            " ".join(args.terms),
            limit=args.limit if args.limit is not None else 10,
            workspace=workspace,
            verified_only=not args.include_candidates,
            all_projects=args.all_projects,
        )
        if args.json:
            _echo(results, True)
            return
        print(f"Matches ({len(results)}):")
        for lesson in results:
            marker = "verified" if lesson["verified"] else "candidate"
            print(
                f"- [{marker}] {lesson['category']} `{lesson['trigger_tool']}` "
                f"({lesson['occurrences']}x, {lesson['workspace_name']}): "
                f"{one_line(lesson['resolution'], 220)}"
            )
        return

    if action == "list":
        lessons = store.list_lessons(
            limit=args.limit if args.limit is not None else 50,
            workspace=workspace,
            include_candidates=not args.verified_only,
            all_projects=args.all_projects,
        )
        if args.json:
            _echo(lessons, True)
            return
        print(f"Lessons ({len(lessons)}):")
        if not lessons:
            print("  - none yet")
            return
        for lesson in lessons:
            marker = "verified" if lesson["verified"] else "candidate"
            print(
                f"- #{lesson['id']} [{marker}] {lesson['category']} "
                f"`{lesson['trigger_tool']}` ({lesson['occurrences']}x, "
                f"{lesson['workspace_name']}): {one_line(lesson['resolution'], 140)}"
            )
        return

    if action == "show":
        lesson_id = _lesson_id(args)
        lesson = store.get_lesson(lesson_id, workspace=workspace)
        if not lesson:
            print(f"Error: lesson #{lesson_id} was not found.", file=sys.stderr)
            sys.exit(1)
        if args.json:
            _echo(lesson, True)
            return
        for key, value in lesson.items():
            print(f"{key}: {value}")
        return

    if action == "add":
        missing = [
            flag
            for flag, value in (
                ("--trigger-tool", args.trigger_tool),
                ("--error-pattern", args.error_pattern),
                ("--resolution", args.resolution),
            )
            if not value
        ]
        if missing:
            print(
                f"Error: {', '.join(missing)} required to add a lesson.",
                file=sys.stderr,
            )
            sys.exit(1)
        try:
            lesson_id = store.add_lesson(
                trigger_tool=args.trigger_tool,
                error_pattern=args.error_pattern,
                resolution=args.resolution,
                category=args.category or "command",
                verified=bool(args.verified),
                workspace=workspace,
                confidence=args.confidence if args.confidence is not None else 0.9,
            )
        except ValueError as exc:
            print(f"Error: {exc}", file=sys.stderr)
            sys.exit(1)
        if args.json:
            _echo({"id": lesson_id}, True)
        else:
            marker = "verified" if args.verified else "candidate"
            print(f"[+] Added {marker} lesson #{lesson_id}")
        return

    if action == "edit":
        lesson_id = _lesson_id(args)
        fields: dict[str, Any] = {}
        if args.trigger_tool is not None:
            fields["trigger_tool"] = args.trigger_tool
        if args.error_pattern is not None:
            fields["error_pattern"] = args.error_pattern
        if args.resolution is not None:
            fields["resolution"] = args.resolution
        if args.category is not None:
            fields["category"] = args.category
        if args.verified:
            fields["verified"] = True
        elif args.candidate:
            fields["verified"] = False
        if args.confidence is not None:
            fields["confidence"] = args.confidence
        if not fields:
            print(
                "Error: provide at least one field to edit (--trigger-tool, "
                "--error-pattern, --resolution, --category, --verified/--candidate, "
                "--confidence).",
                file=sys.stderr,
            )
            sys.exit(1)
        try:
            lesson = store.update_lesson(lesson_id, workspace=workspace, **fields)
        except ValueError as exc:
            print(f"Error: {exc}", file=sys.stderr)
            sys.exit(1)
        if lesson is None:
            print(f"Error: lesson #{lesson_id} was not found.", file=sys.stderr)
            sys.exit(1)
        if args.json:
            _echo(lesson, True)
        else:
            print(f"[+] Updated lesson #{lesson_id}: {', '.join(sorted(fields))}")
        return

    if action == "delete":
        lesson_id = _lesson_id(args)
        if not store.get_lesson(lesson_id, workspace=workspace):
            print(f"Error: lesson #{lesson_id} was not found.", file=sys.stderr)
            sys.exit(1)
        if not args.yes:
            if args.json:
                print(
                    "Error: --yes is required to delete a lesson in JSON mode.",
                    file=sys.stderr,
                )
                sys.exit(1)
            try:
                answer = input(f"Delete lesson #{lesson_id}? [y/N] ").strip().lower()
            except EOFError:
                answer = "n"
            if answer not in ("y", "yes"):
                print("Cancelled.")
                return
        deleted = store.delete_lesson(lesson_id, workspace=workspace)
        if not deleted:
            print(f"Error: lesson #{lesson_id} was not found.", file=sys.stderr)
            sys.exit(1)
        if args.json:
            _echo({"deleted": True, "id": lesson_id}, True)
        else:
            print(f"[+] Deleted lesson #{lesson_id}")
        return

    if action == "sync":
        store.sync_experience_md()
        if args.json:
            _echo({"synced": True, "experience_path": str(store.exp_md_path)}, True)
        else:
            print(f"[+] Synchronized {store.exp_md_path}")
        return

    if action == "export":
        path_value = args.path_option or (args.terms[0] if args.terms else "")
        if not path_value:
            print("Error: export path is required (use --path).", file=sys.stderr)
            sys.exit(1)
        path = store.export_data(path_value, workspace=workspace)
        if args.json:
            _echo({"exported": True, "path": str(path)}, True)
        else:
            print(f"[+] Exported memory to {path}")
        return

    if action == "import":
        path_value = args.path_option or (args.terms[0] if args.terms else "")
        if not path_value:
            print("Error: import path is required (use --path).", file=sys.stderr)
            sys.exit(1)
        try:
            counts = store.import_data(path_value, merge=not args.replace)
        except ValueError as exc:
            print(f"Error: {exc}", file=sys.stderr)
            sys.exit(1)
        if args.json:
            _echo(counts, True)
        else:
            print(f"[+] Imported: {counts}")
        return

    if action == "prune":
        settings, _warnings = load_settings()
        counts = store.prune(
            max_lessons=args.max_lessons
            if args.max_lessons is not None
            else int(get_setting(settings, "memory.max_lessons", 2000)),
            max_sessions=args.max_sessions,
            max_rule_edits=args.max_rule_edits,
            days=args.days
            if args.days is not None
            else int(get_setting(settings, "memory.prune_days", 365)),
            candidate_days=args.candidate_days
            if args.candidate_days is not None
            else int(get_setting(settings, "memory.candidate_days", 30)),
        )
        if args.json:
            _echo(counts, True)
        else:
            print(f"[+] Pruned: {counts}")
        return

    if action in ("reset", "forget"):
        if not workspace:
            print("Error: reset requires --workspace PATH.", file=sys.stderr)
            sys.exit(1)
        if not args.yes:
            if args.json:
                print("Error: --yes is required to reset memory in JSON mode.", file=sys.stderr)
                sys.exit(1)
            try:
                answer = input(f"Delete memory for {workspace}? [y/N] ").strip().lower()
            except EOFError:
                answer = "n"
            if answer not in ("y", "yes"):
                print("Cancelled.")
                return
        counts = store.reset_workspace(workspace)
        if args.json:
            _echo({"reset": True, "workspace": str(workspace), "counts": counts}, True)
        else:
            print(f"[+] Reset workspace memory: {counts}")
        return

    print(f"Unknown memory action: {action}", file=sys.stderr)
    sys.exit(1)


# ------------------------------------------------------------------------- rules
def _rule_matches(query: str, rule_text: str) -> bool:
    normalized_query = normalize_for_compare(query, keep_code=True)
    normalized_rule = normalize_for_compare(rule_text, keep_code=True)
    if not normalized_query or not normalized_rule:
        return False
    if normalized_query == normalized_rule:
        return True
    if len(normalized_query) >= 4 and (
        normalized_query in normalized_rule or normalized_rule in normalized_query
    ):
        return True
    return jaccard(query, rule_text, keep_code=True) >= 0.5


def cmd_rules(args: argparse.Namespace) -> None:
    action = args.action or "show"
    workspace = _workspace(args) or Path.cwd().resolve()
    manager = AgentsMdManager(workspace)
    targets = ["workspace", "plugin"] if args.target == "both" else [args.target]
    text = (getattr(args, "text", "") or "").strip()

    if action in ("show", "view"):
        if args.json:
            _echo(
                {
                    target: {
                        "path": str(
                            manager.workspace_agents_md if target == "workspace" else manager.plugin_agents_md
                        ),
                        "content": manager.show(target),
                    }
                    for target in targets
                },
                True,
            )
            return
        for target in targets:
            print(f"--- {target} ---")
            content = manager.show(target)
            print(content if content else "(not created)")
        return

    if action in ("sanitize", "repair"):
        results = manager.repair(target=args.target, dry_run=args.dry_run)
        if args.json:
            _echo(results, True)
            return
        for target, result in results.items():
            print(
                f"[{target}] updated={result.get('updated')} kept={result.get('kept')} "
                f"removed={result.get('removed', 0)} bytes={result.get('bytes')} "
                f"{'(dry run)' if args.dry_run else ''}"
            )
            for warning in result.get("warnings", []):
                print(f"  warning: {warning}")
        return

    if action == "why":
        if not text:
            print("Error: provide rule text to explain.", file=sys.stderr)
            sys.exit(1)
        store = MemoryStore(workspace=workspace)
        matches: list[dict[str, Any]] = []
        all_conflicts: list[dict[str, Any]] = []
        for target in targets:
            target_conflicts = manager.find_conflicts(target)
            for conflict in target_conflicts:
                all_conflicts.append({**conflict, "target": target})
            for rule in manager.managed_rules(target):
                if not _rule_matches(text, rule["text"]):
                    continue
                provenance = store.get_rule(rule["text"], rule["target_file"])
                if provenance is None:
                    provenance = store.get_rule(rule["text"])
                matches.append(
                    {
                        **rule,
                        "target": target,
                        "in_file": True,
                        "provenance": provenance,
                        "conflicts": [
                            conflict
                            for conflict in target_conflicts
                            if conflict.get("first") == rule["text"]
                            or conflict.get("second") == rule["text"]
                        ],
                    }
                )
        direct = store.get_rule(text)
        if direct and not any(
            match.get("provenance")
            and match["provenance"].get("rule_hash") == direct.get("rule_hash")
            for match in matches
        ):
            matches.append(
                {
                    "text": direct["text"],
                    "section": direct["section"],
                    "target_file": direct["target_file"],
                    "scope": direct["scope"],
                    "target": direct["scope"],
                    "in_file": False,
                    "provenance": direct,
                    "conflicts": [
                        conflict
                        for conflict in all_conflicts
                        if conflict.get("first") == direct["text"]
                        or conflict.get("second") == direct["text"]
                    ],
                }
            )
        if args.json:
            _echo(
                {
                    "query": text,
                    "found": bool(matches or direct),
                    "matches": matches,
                    "provenance": direct,
                    "conflicts": all_conflicts,
                },
                True,
            )
            return
        if not matches and not direct:
            print(f"No managed rule or provenance found for: {text}")
            return
        for match in matches:
            print(f"[{match['target']}] {match['text']}")
            print(f"  section : {match['section']}")
            if match.get("in_file"):
                print(f"  file    : {match['target_file']}")
            else:
                print("  file    : not present (database provenance only)")
            provenance = match.get("provenance")
            if provenance:
                print(
                    "  memory  : "
                    f"occurrences={provenance.get('occurrences', 0)} "
                    f"surfaced={provenance.get('surfaced_count', 0)} "
                    f"helpful={provenance.get('helpful_count', 0)} "
                    f"harmful={provenance.get('harmful_count', 0)}"
                )
                print(
                    "  seen    : "
                    f"{provenance.get('created_at', '')} -> "
                    f"{provenance.get('last_seen_at', '')}"
                )
                if provenance.get("conversation_id"):
                    print(f"  session : {provenance['conversation_id']}")
                if provenance.get("evidence"):
                    print(f"  evidence: {provenance['evidence']}")
            else:
                print("  memory  : no provenance recorded")
            for conflict in match.get("conflicts", []):
                print(f"  conflict: {conflict.get('reason')}")
        return

    if action == "forget":
        if not text:
            print("Error: provide rule text to forget.", file=sys.stderr)
            sys.exit(1)
        store = MemoryStore(workspace=workspace)
        matched_texts: set[str] = set()
        matched_by_target: dict[str, str] = {}
        for target in targets:
            for rule in manager.managed_rules(target):
                if _rule_matches(text, rule["text"]):
                    matched_texts.add(rule["text"])
                    matched_by_target.setdefault(target, rule["text"])
        results = {
            target: manager.remove_rule(
                matched_by_target.get(target, text),
                target=target,
                dry_run=args.dry_run,
            )
            for target in targets
        }
        db_removed = False
        if not args.dry_run:
            for candidate in {text, *matched_texts}:
                db_removed = store.forget_rule(candidate) or db_removed
        removed_any = db_removed or any(
            int(result.get("removed", 0)) for result in results.values()
        )
        if args.json:
            _echo(
                {
                    "query": text,
                    "removed": bool(removed_any),
                    "dry_run": bool(args.dry_run),
                    "db_removed": bool(db_removed),
                    "targets": results,
                },
                True,
            )
            return
        for target, result in results.items():
            if result.get("removed"):
                print(
                    f"[{target}] removed 1 rule "
                    f"{'(dry run)' if args.dry_run else ''}: {result['path']}"
                )
                for warning in result.get("warnings", []):
                    print(f"  warning: {warning}")
        if db_removed:
            print("[memory] removed provenance record")
        if not removed_any:
            print(f"No managed rule or provenance found for: {text}")
        return

    if action == "conflicts":
        results = {target: manager.find_conflicts(target) for target in targets}
        if args.json:
            _echo(results, True)
            return
        total = sum(len(items) for items in results.values())
        if not total:
            print("No clear conflicts found.")
            return
        for target, conflicts in results.items():
            for conflict in conflicts:
                print(f"[{target}] {conflict['reason']}")
                print(f"  first : {conflict['first']}")
                print(f"  second: {conflict['second']}")
        return

    print(f"Unknown rules action: {action}", file=sys.stderr)
    sys.exit(1)


# ------------------------------------------------------------------------- doctor
def _fts5_available() -> bool:
    """Return whether this Python's SQLite can create an FTS5 table."""
    try:
        conn = sqlite3.connect(":memory:")
    except sqlite3.DatabaseError:
        return False
    try:
        conn.execute("CREATE VIRTUAL TABLE fts_probe USING fts5(value)")
        return True
    except sqlite3.DatabaseError:
        return False
    finally:
        conn.close()


def cmd_doctor(args: argparse.Namespace) -> None:
    workspace = _workspace(args)
    plugin = get_plugin_dir()
    checks: list[dict[str, Any]] = []
    critical = False

    def add(name: str, ok: bool, detail: str, critical_check: bool = False) -> None:
        nonlocal critical
        checks.append({"check": name, "ok": ok, "detail": detail, "critical": critical_check})
        if critical_check and not ok:
            critical = True

    add("python", sys.version_info >= (3, 10), sys.version.split()[0], True)
    add("plugin dir", plugin.exists(), str(plugin))

    manifest_path = plugin / "plugin.json"
    manifest_ok = manifest_path.exists()
    manifest_detail = str(manifest_path)
    if manifest_ok:
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8-sig"))
            if not isinstance(manifest, dict):
                raise ValueError("manifest is not a JSON object")
            extra = set(manifest) - PLUGIN_MANIFEST_FIELDS
            if extra:
                raise ValueError("unsupported fields: " + ", ".join(sorted(extra)))
            if manifest.get("name") != "harness":
                raise ValueError("name must be 'harness'")
            if not manifest.get("description"):
                raise ValueError("description is missing")
        except Exception as exc:
            manifest_ok = False
            manifest_detail = f"{manifest_path}: {exc}"
    add("plugin.json", manifest_ok, manifest_detail, True)

    hooks_path = plugin / "hooks.json"
    hooks_ok = hooks_path.exists()
    events: set[str] = set()
    if hooks_ok:
        try:
            hooks = json.loads(hooks_path.read_text(encoding="utf-8-sig"))
            for group in hooks.values():
                if isinstance(group, dict):
                    events.update(group.keys())
            missing = {"SessionStart", "PostToolUse", "Stop"} - events
            if missing:
                raise ValueError("missing events: " + ", ".join(sorted(missing)))
        except Exception as exc:
            hooks_ok = False
            manifest_detail = f"{hooks_path}: {exc}"
    add("hooks.json", hooks_ok, str(hooks_path) if hooks_ok else manifest_detail, True)
    add("hook events", hooks_ok, ", ".join(sorted(events)) or "none")

    runner = plugin / "scripts" / "hook_runner.py"
    add("hook runner", runner.exists(), str(runner), True)

    db_path = get_memory_db_path()
    stats: dict[str, Any]
    if db_path.exists():
        try:
            store = MemoryStore(
                workspace=workspace, initialize=False, read_only=True
            )
            stats = store.get_stats(workspace, all_projects=workspace is None)
            fts = bool(stats["fts"])
            fts_detail = "SQLite FTS5" if fts else "LIKE fallback"
        except Exception as exc:
            stats = {
                "sessions": 0, "verified_lessons": 0, "candidate_lessons": 0,
                "skills": 0, "rule_edits": 0, "fts": False,
            }
            add("memory db", False, f"{db_path}: {exc}")
            fts_detail = "LIKE fallback"
    else:
        stats = {
            "sessions": 0, "verified_lessons": 0, "candidate_lessons": 0,
            "skills": 0, "rule_edits": 0, "fts": False,
        }
        add("memory db", True, f"{db_path} (not created)")
        fts_detail = (
            "SQLite FTS5 available (not created)"
            if _fts5_available()
            else "LIKE fallback (FTS5 unavailable)"
        )
    if db_path.exists():
        add(
            "memory db",
            True,
            f"{db_path} ({stats['verified_lessons']} lessons, "
            f"{stats['candidate_lessons']} candidates)",
        )
    add("fts search", True, fts_detail)

    builder = SkillBuilder(workspace)
    try:
        skills = builder.list_installed_skills(workspace)
        invalid = [skill for skill in skills if not skill.get("valid")]
        add("skills", not invalid, f"{len(skills)} found, {len(invalid)} invalid")
    except Exception as exc:
        add("skills", False, str(exc))

    for label, path in (
        ("workspace rules", get_workspace_rules_path(workspace) if workspace else None),
        ("global rules", get_plugin_rules_path(create=False)),
    ):
        if path is None:
            continue
        if path.exists():
            size = path.stat().st_size
            add(label, size <= 24 * 1024, f"{path} ({size} bytes)")
        else:
            add(label, True, f"{path} (not created)")

    wrappers = [plugin / "bin" / "harness.cmd", plugin / "bin" / "harness"]
    add("bin wrappers", any(path.exists() for path in wrappers), ", ".join(str(path) for path in wrappers))

    if args.json:
        _echo({"version": __version__, "critical": critical, "checks": checks}, True)
    else:
        print(f"Harness² doctor (v{__version__})")
        print("=" * 68)
        for check in checks:
            marker = "OK  " if check["ok"] else "FAIL"
            print(f"[{marker}] {check['check']}: {check['detail']}")
        print("=" * 68)
        if critical:
            print("Critical checks failed.")
        elif any(not check["ok"] for check in checks):
            print("Review the checks marked FAIL above.")
        else:
            print("All checks passed.")
    if critical:
        sys.exit(1)


# ------------------------------------------------------------------------ recall
def cmd_recall(args: argparse.Namespace) -> None:
    query = " ".join(args.query).strip()
    if not query:
        print("Error: provide a request to recall experience for.", file=sys.stderr)
        sys.exit(1)
    workspace = _workspace(args) or Path.cwd().resolve()
    try:
        limit = max(1, min(int(args.limit), 10))
    except (TypeError, ValueError):
        limit = 3
    found = find_recall(query, workspace, limit=limit)
    message = format_recall_message(found["lessons"], found["skills"])
    if args.json:
        _echo(
            {
                "query": query,
                "workspace": str(workspace),
                "lessons": found["lessons"],
                "skills": found["skills"],
                "message": message,
            },
            True,
        )
        return
    if not message:
        print("No matching verified lessons or skills.", file=sys.stderr)
        return
    print(message)


# ------------------------------------------------------------------------ route
def cmd_route(args: argparse.Namespace) -> None:
    """Detect and persist the project-local skills/tools for a task."""
    workspace = _workspace(args) or Path.cwd().resolve()
    task = " ".join(args.query).strip()
    router = TaskRouter(workspace)
    if not router.enabled:
        message = "Task routing is disabled (routing.enabled = false)."
        if args.json:
            _echo({"enabled": False, "workspace": str(workspace), "message": message}, True)
        else:
            print(message, file=sys.stderr)
        return
    result = router.apply(task, dry_run=args.dry_run)
    payload = {
        "workspace": str(workspace),
        **result.as_dict(),
        "dry_run": bool(args.dry_run),
    }
    if args.json:
        _echo(payload, True)
        return
    print(f"[HARNESS] Task routing for {workspace}")
    if payload["task"]:
        print(f"  task    : {payload['task']}")
    print(
        "  skills  : "
        + (", ".join(payload["skills"]) if payload["skills"] else "none matched")
    )
    print(
        "  tools   : "
        + (", ".join(payload["tools"]) if payload["tools"] else "none matched")
    )
    if payload["inventory"]:
        print("  available project skills: " + ", ".join(payload["inventory"]))
    print(
        f"  agents  : {payload['path']} "
        f"({'updated' if payload['updated'] else 'unchanged'}"
        f"{', dry run' if args.dry_run else ''})"
    )
    for warning in payload["warnings"]:
        print(f"  warning : {warning}")


# ----------------------------------------------------------------------- privacy
def cmd_privacy(args: argparse.Namespace) -> None:
    workspace = _workspace(args) or Path.cwd().resolve()
    report = audit_privacy(
        plugin_dir=get_plugin_dir(),
        workspace=workspace,
        sample_limit=args.limit,
    )
    if args.json:
        _echo(report, True)
        return
    print("[HARNESS] Privacy audit")
    print(f"Plugin directory : {report['plugin_dir']}")
    print(f"Workspace        : {report['workspace']}")
    print()
    print("State files:")
    for name, info in report.get("files", {}).items():
        if isinstance(info, dict):
            print(
                f"  {name}: {'present' if info.get('exists') else 'absent'}"
                f" ({int(info.get('bytes') or 0)} bytes)"
            )
    counts = report.get("counts", {})
    print()
    print("Memory counts:")
    for key in ("sessions", "lessons", "verified_lessons", "skills", "rule_edits"):
        if key in counts:
            print(f"  {key}: {counts[key]}")
    print()
    print("Recommendations:")
    for item in report.get("recommendations", []):
        print(f"  - {item}")


# ------------------------------------------------------------------------ install
def cmd_install(args: argparse.Namespace) -> None:
    script = Path(__file__).resolve().parent.parent / "install_plugin.py"
    if not script.exists():
        print("Error: install_plugin.py is not bundled in this plugin.", file=sys.stderr)
        sys.exit(1)
    command = [sys.executable, str(script)]
    if args.target:
        command.extend(["--target", str(Path(args.target).expanduser())])
    if args.force:
        command.append("--force")
    if args.reset:
        command.append("--reset")
    if args.dry_run:
        command.append("--dry-run")
    if getattr(args, "keep_backups", None) is not None:
        command.extend(["--keep-backups", str(args.keep_backups)])
    if getattr(args, "uninstall", False):
        command.append("--uninstall")
    if getattr(args, "rollback", False):
        command.append("--rollback")
    completed = subprocess.run(command)
    sys.exit(completed.returncode)


# --------------------------------------------------------------------------- sync
def cmd_sync(args: argparse.Namespace) -> None:
    workspace = _workspace(args)
    store = MemoryStore(workspace=workspace)
    store.sync_experience_md()
    print(f"[+] Synchronized {store.exp_md_path}")


# --------------------------------------------------------------------------- main
def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="harness",
        description="Harness²: recursive agent harnessing for Antigravity",
    )
    parser.add_argument("--version", action="version", version=f"Harness² {__version__}")
    subparsers = parser.add_subparsers(dest="command")

    p_status = subparsers.add_parser("status", help="show memory, skills, and rule status")
    _add_workspace(p_status)
    p_status.add_argument("--json", action="store_true", help="machine-readable output")
    p_status.add_argument(
        "--effectiveness",
        action="store_true",
        help="include outcome-aware lesson and skill utility rankings",
    )
    p_status.set_defaults(func=cmd_status)

    p_reflect = subparsers.add_parser("reflect", help="analyse a session and update the harness")
    p_reflect.add_argument("--session", "-s", help="conversation id")
    p_reflect.add_argument("--transcript", "-t", help="path to transcript.jsonl")
    p_reflect.add_argument("--force", action="store_true", help="re-analyse an already processed session")
    p_reflect.add_argument("--global", dest="global_scope", action="store_true", help="target global rules instead of the workspace")
    p_reflect.add_argument("--json", action="store_true", help="machine-readable output")
    _add_workspace(p_reflect)
    p_reflect.set_defaults(func=cmd_reflect)

    p_skills = subparsers.add_parser("skills", help="list, inspect, install, validate, or remove skills")
    p_skills.add_argument(
        "action",
        nargs="?",
        default="list",
        choices=["list", "ls", "show", "inspect", "install", "remove", "validate", "check", "why", "diagnose"],
    )
    p_skills.add_argument("target", nargs="?", help="skill name or install source")
    p_skills.add_argument("--limit", type=int, default=10, help="sessions to inspect for 'why' (default 10)")
    p_skills.add_argument("--name", "-n", help="skill name")
    p_skills.add_argument("--source", help="local path, GitHub repo, or URL to install")
    p_skills.add_argument(
        "--scope",
        choices=["workspace", "global", "plugin", "user"],
        default=None,
        help="install/remove scope (defaults to workspace when available, otherwise global)",
    )
    p_skills.add_argument("--force", action="store_true", help="replace an existing installed skill")
    p_skills.add_argument("--all", action="store_true", help="install every skill found in a repository")
    p_skills.add_argument("--timeout", type=int, default=30, help="download timeout in seconds")
    p_skills.add_argument("--yes", "-y", action="store_true", help="skip removal confirmation")
    p_skills.add_argument("--json", action="store_true", help="machine-readable output")
    _add_workspace(p_skills)
    p_skills.set_defaults(func=cmd_skills)

    p_memory = subparsers.add_parser(
        "memory", help="list, show, add, edit, delete, search, export, import, prune, or reset memory"
    )
    p_memory.add_argument(
        "action",
        nargs="?",
        default="stats",
        choices=[
            "stats", "status", "search", "find", "list", "show", "add", "edit",
            "delete", "sync", "export", "import", "prune", "reset", "forget",
        ],
    )
    p_memory.add_argument("terms", nargs="*", help="search terms, lesson id, or a positional export/import path")
    p_memory.add_argument("--path", dest="path_option", help="export/import file path")
    p_memory.add_argument("--limit", type=int, default=None)
    p_memory.add_argument("--include-candidates", action="store_true")
    p_memory.add_argument(
        "--verified-only",
        action="store_true",
        help="list only verified lessons",
    )
    p_memory.add_argument("--trigger-tool", help="lesson trigger tool (add/edit)")
    p_memory.add_argument("--error-pattern", help="lesson error pattern (add/edit)")
    p_memory.add_argument("--resolution", help="lesson resolution (add/edit)")
    p_memory.add_argument("--category", help="lesson category (add/edit)")
    p_memory.add_argument("--confidence", type=float, default=None, help="lesson confidence (add/edit)")
    memory_verified = p_memory.add_mutually_exclusive_group()
    memory_verified.add_argument("--verified", action="store_true", help="mark the lesson verified")
    memory_verified.add_argument(
        "--candidate", action="store_true", help="keep the lesson as a candidate observation"
    )
    p_memory.add_argument(
        "--all-projects",
        action="store_true",
        help="include every project when no workspace is given",
    )
    p_memory.add_argument("--max-lessons", type=int, default=None)
    p_memory.add_argument("--max-sessions", type=int, default=1000)
    p_memory.add_argument("--max-rule-edits", type=int, default=500)
    p_memory.add_argument("--days", type=int, default=None)
    p_memory.add_argument("--candidate-days", type=int, default=None)
    p_memory.add_argument("--replace", action="store_true", help="replace conflicting import rows")
    p_memory.add_argument("--yes", "-y", action="store_true")
    p_memory.add_argument("--json", action="store_true")
    _add_workspace(p_memory)
    p_memory.set_defaults(func=cmd_memory)

    p_rules = subparsers.add_parser("rules", help="show, explain, forget, or sanitize AGENTS.md rules")
    p_rules.add_argument(
        "action",
        nargs="?",
        default="show",
        choices=[
            "show", "view", "sanitize", "repair", "why", "forget", "conflicts",
        ],
    )
    p_rules.add_argument("text", nargs="?", default="", help="rule text for why/forget")
    p_rules.add_argument("--target", choices=["workspace", "plugin", "both"], default="workspace")
    p_rules.add_argument("--dry-run", action="store_true")
    p_rules.add_argument("--json", action="store_true")
    _add_workspace(p_rules)
    p_rules.set_defaults(func=cmd_rules)

    p_recall = subparsers.add_parser(
        "recall", help="search verified lessons and skills for a request"
    )
    p_recall.add_argument("query", nargs="+", help="request text or error message")
    p_recall.add_argument("--limit", type=int, default=3)
    p_recall.add_argument("--json", action="store_true")
    _add_workspace(p_recall)
    p_recall.set_defaults(func=cmd_recall)

    p_route = subparsers.add_parser(
        "route",
        help="detect project-local skills/tools for a task and write them to AGENTS.md",
    )
    p_route.add_argument(
        "query",
        nargs="*",
        help="task text; omit to refresh the project skill inventory",
    )
    p_route.add_argument("--dry-run", action="store_true", help="show the selection without writing")
    p_route.add_argument("--json", action="store_true")
    _add_workspace(p_route)
    p_route.set_defaults(func=cmd_route)

    p_privacy = subparsers.add_parser(
        "privacy", help="audit stored state, memory, and learned artifacts"
    )
    p_privacy.add_argument(
        "action", nargs="?", default="audit", choices=["audit"]
    )
    p_privacy.add_argument("--limit", type=int, default=20)
    p_privacy.add_argument("--json", action="store_true")
    _add_workspace(p_privacy)
    p_privacy.set_defaults(func=cmd_privacy)

    p_doctor = subparsers.add_parser("doctor", help="check the deployment and data health")
    p_doctor.add_argument("--json", action="store_true")
    _add_workspace(p_doctor)
    p_doctor.set_defaults(func=cmd_doctor)

    p_sync = subparsers.add_parser("sync", help="regenerate EXPERIENCE.md")
    _add_workspace(p_sync)
    p_sync.set_defaults(func=cmd_sync)

    p_install = subparsers.add_parser("install", help="deploy this plugin into Antigravity")
    p_install.add_argument("--target", help="plugin install directory")
    p_install.add_argument("--force", action="store_true", help="overwrite bundled files")
    p_install.add_argument("--reset", action="store_true", help="reset memory and rules")
    install_action = p_install.add_mutually_exclusive_group()
    install_action.add_argument(
        "--uninstall", action="store_true", help="back up and remove the deployed plugin"
    )
    install_action.add_argument(
        "--rollback", action="store_true", help="restore the newest valid backup"
    )
    p_install.add_argument("--keep-backups", type=int, default=5)
    p_install.add_argument("--dry-run", action="store_true")
    p_install.set_defaults(func=cmd_install)

    return parser


def main(argv: list[str] | None = None) -> None:
    parser = build_parser()
    args = parser.parse_args(argv)
    if not hasattr(args, "func"):
        parser.print_help()
        return
    try:
        args.func(args)
    except KeyboardInterrupt:
        print("\nCancelled.", file=sys.stderr)
        sys.exit(130)
    except SkillError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        sys.exit(1)
    except Exception as exc:
        if os.environ.get("HARNESS_DEBUG"):
            raise
        print(f"Error: {exc}", file=sys.stderr)
        print("Set HARNESS_DEBUG=1 for a traceback.", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
