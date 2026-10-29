"""
Deploy Harness² into the Antigravity plugin directory.

The installer is deliberately non-destructive:

* memory, state, and user-authored skills survive an upgrade;
* rules are only replaced when missing, with ``--force``, or with ``--reset``;
* ``--reset`` backs up data first, then clears memory and generated skills;
* old backups are pruned to ``--keep-backups`` (default 5) after a good backup;
* ``--uninstall`` and ``--rollback`` always back up current data first;
* a smoke test verifies that the hook entry point returns valid JSON.

Usage:
    python install_plugin.py [--target PATH] [--force] [--reset] [--dry-run]
                             [--keep-backups N]
    python install_plugin.py [--target PATH] --uninstall [--dry-run]
    python install_plugin.py [--target PATH] --rollback [--dry-run]
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import shutil
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
DEFAULT_TARGET = Path(
    os.environ.get("HARNESS_PLUGIN_DIR", Path.home() / ".gemini" / "config" / "plugins" / "harness")
).expanduser()

MANAGED_SKILL_MARKERS = (
    "<!-- HARNESS:SKILL_START -->",
    "<!-- harness-meta:",
    "synthesized autonomously by harness",
    "autonomously learned by harness",
)
# Fields accepted by https://antigravity.google/schemas/v1/plugin.json.
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
_IGNORED_TARGET_ENTRIES = {".DS_Store", "Thumbs.db"}

STATIC_FILES = (
    "plugin.json",
    "hooks.json",
    "README.md",
    "LICENSE",
    "NOTICE",
    "CHANGELOG.md",
    "install_plugin.py",
)
STATIC_DIRS = ("harness", "scripts", "agents", "assets", "docs", "bin")
BUNDLED_SKILLS_DIR = "skills"
IGNORE = shutil.ignore_patterns(
    "__pycache__", "*.pyc", "*.pyo", ".pytest_cache", ".mypy_cache", ".git", "*.db", "*.sqlite3"
)
# A backup must include the runtime database; it is the reason to back up.
BACKUP_IGNORE = shutil.ignore_patterns(
    "__pycache__", "*.pyc", "*.pyo", ".pytest_cache", ".mypy_cache", ".git"
)
BACKUP_DIR_NAME = ".harness-backups"
_HOOK_EVENT_ARGS = {
    "SessionStart": "session-start",
    "PostToolUse": "post-tool-use",
    "PreInvocation": "pre-invocation",
    "Stop": "stop",
}


def _log(message: str, dry_run: bool = False) -> None:
    prefix = "[dry-run] " if dry_run else ""
    print(prefix + message)


def _is_within(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def _source_version() -> str:
    try:
        text = (ROOT / "harness" / "__init__.py").read_text(encoding="utf-8")
    except OSError:
        return "unknown"
    match = re.search(r'__version__\s*=\s*["\']([^"\']+)["\']', text)
    return match.group(1) if match else "unknown"


def _is_harness_installation(target: Path) -> bool:
    """Recognise an existing Harness² deployment before touching its files."""
    manifest = target / "plugin.json"
    if manifest.exists():
        try:
            data = json.loads(manifest.read_text(encoding="utf-8"))
            if isinstance(data, dict) and data.get("name") == "harness":
                return True
        except (OSError, UnicodeError, json.JSONDecodeError):
            pass
    hooks = target / "hooks.json"
    if hooks.exists():
        try:
            data = json.loads(hooks.read_text(encoding="utf-8"))
            if isinstance(data, dict) and "harness-lifecycle" in data:
                return True
        except (OSError, UnicodeError, json.JSONDecodeError):
            pass
    runner = target / "scripts" / "hook_runner.py"
    if runner.exists():
        try:
            text = runner.read_text(encoding="utf-8", errors="replace")
            if "Harness" in text and "hook_runner" in text:
                return True
        except OSError:
            pass
    return False


def _target_problem(target: Path) -> str:
    """Refuse targets where a mistaken copy or reset would be destructive."""
    resolved = target.resolve()
    home = Path.home().resolve()
    if resolved.parent == resolved:
        return "refusing to use a filesystem root as the target"
    if resolved == home:
        return "refusing to use the home directory as the target"
    protected: list[Path] = [
        home / ".gemini",
        home / ".gemini" / "config",
        home / ".gemini" / "config" / "plugins",
    ]
    configured = os.environ.get("HARNESS_GEMINI_CONFIG_DIR")
    if configured:
        config_dir = Path(configured).expanduser()
        protected.extend([config_dir, config_dir / "plugins"])
    seen: set[Path] = set()
    for directory in protected:
        try:
            directory = directory.resolve()
        except OSError:
            continue
        if directory in seen:
            continue
        seen.add(directory)
        if resolved == directory:
            return (
                "refusing to use Antigravity's config directory "
                f"({directory}) as the target"
            )
    source = ROOT.resolve()
    if resolved == source or _is_within(resolved, source) or _is_within(source, resolved):
        return "target must be separate from the source checkout"
    if target.exists():
        if not target.is_dir():
            return "target exists and is not a directory"
        entries = [
            entry for entry in target.iterdir() if entry.name not in _IGNORED_TARGET_ENTRIES
        ]
        if entries and not _is_harness_installation(target):
            return (
                "target is a non-empty directory that does not look like a Harness² plugin; "
                "refusing to overwrite unrelated files"
            )
    return ""


def _is_managed_skill(folder: Path) -> bool:
    skill_file = folder / "SKILL.md"
    if not skill_file.is_file():
        return False
    try:
        text = skill_file.read_text(encoding="utf-8", errors="replace").lower()
    except OSError:
        return False
    return any(marker.lower() in text for marker in MANAGED_SKILL_MARKERS)


def _copy_file(source: Path, target: Path, dry_run: bool) -> None:
    if not source.exists():
        return
    _log(f"  + {source.relative_to(ROOT)} -> {target}", dry_run)
    if dry_run:
        return
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(f".{target.name}.harness-tmp-{os.getpid()}")
    try:
        shutil.copy2(source, temporary)
        temporary.replace(target)
    finally:
        if temporary.exists():
            temporary.unlink(missing_ok=True)


def _copy_tree(source: Path, target: Path, dry_run: bool) -> None:
    if not source.exists():
        return
    _log(f"  + {source.relative_to(ROOT)}/ -> {target}/", dry_run)
    if dry_run:
        return
    target.parent.mkdir(parents=True, exist_ok=True)
    staging = target.parent / f".{target.name}.harness-tmp-{os.getpid()}"
    try:
        if staging.exists():
            shutil.rmtree(staging)
        shutil.copytree(source, staging, ignore=IGNORE)
        if target.exists():
            shutil.rmtree(target)
        staging.replace(target)
    finally:
        if staging.exists():
            shutil.rmtree(staging, ignore_errors=True)


def _backup(target: Path, dry_run: bool) -> tuple[Path | None, list[str]]:
    if not target.exists():
        return None, []
    stamp = time.strftime("%Y%m%d-%H%M%S")
    backup_root = target.parent / ".harness-backups" / stamp
    suffix = 1
    while backup_root.exists():
        suffix += 1
        backup_root = target.parent / ".harness-backups" / f"{stamp}-{suffix}"
    _log(f"  + backup -> {backup_root}", dry_run)
    if dry_run:
        return backup_root, []

    problems: list[str] = []
    backup_root.parent.mkdir(parents=True, exist_ok=True)
    try:
        shutil.copytree(target, backup_root, ignore=BACKUP_IGNORE)
    except BaseException:
        # A partial directory must never be mistaken for a usable backup.
        shutil.rmtree(backup_root, ignore_errors=True)
        raise

    # Copying a live SQLite database together with its WAL is not a consistent
    # snapshot. Replace every database in the backup with an online backup.
    for database in target.rglob("*.db"):
        if not database.is_file():
            continue
        relative = database.relative_to(target)
        destination = backup_root / relative
        try:
            destination.parent.mkdir(parents=True, exist_ok=True)
            source_connection = sqlite3.connect(str(database), timeout=10)
            try:
                source_connection.execute("PRAGMA busy_timeout = 5000")
                destination_connection = sqlite3.connect(str(destination), timeout=10)
                try:
                    source_connection.backup(destination_connection)
                finally:
                    destination_connection.close()
            finally:
                source_connection.close()
        except (sqlite3.Error, OSError) as exc:
            problems.append(
                f"database backup for {relative} used a plain file copy: {exc}"
            )
            try:
                shutil.copy2(database, destination)
            except OSError as copy_error:
                problems.append(f"could not back up {relative}: {copy_error}")
        for suffix_name in ("-wal", "-shm"):
            stale = destination.with_name(destination.name + suffix_name)
            if stale.exists():
                stale.unlink(missing_ok=True)
    return backup_root, problems


def _backup_directories(target: Path) -> list[Path]:
    """Existing backup directories, oldest name first (timestamps sort)."""
    root = target.parent / BACKUP_DIR_NAME
    if not root.is_dir():
        return []
    try:
        return sorted(
            (
                path
                for path in root.iterdir()
                if path.is_dir() and not path.name.startswith(".")
            ),
            key=lambda path: path.name,
        )
    except OSError:
        return []


def _prune_backups(
    target: Path,
    keep: int,
    current: Path | None = None,
    dry_run: bool = False,
) -> list[Path]:
    """Keep the newest ``keep`` backups, always including the one just made."""
    try:
        keep_count = max(1, int(keep))
    except (TypeError, ValueError):
        keep_count = 5

    candidates = _backup_directories(target)
    if current is not None and current not in candidates:
        candidates.append(current)
        candidates.sort(key=lambda path: path.name)
    if len(candidates) <= keep_count:
        return []

    protected = {path.name for path in candidates[-keep_count:]}
    if current is not None:
        protected.add(current.name)

    root = target.parent / BACKUP_DIR_NAME
    removed: list[Path] = []
    for path in candidates:
        if path.name in protected:
            continue
        if path.parent != root or not path.is_dir():
            continue
        _log(f"  - prune old backup {path}", dry_run)
        if not dry_run:
            try:
                shutil.rmtree(path)
            except OSError as exc:
                _log(f"    (could not prune {path.name}: {exc})")
                continue
        removed.append(path)
    return removed


def _valid_backup(path: Path) -> bool:
    """A restore source must look unmistakably like a Harness² deployment."""
    if not path.is_dir():
        return False
    manifest_path = path / "plugin.json"
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return False
    return (
        isinstance(manifest, dict)
        and manifest.get("name") == "harness"
        and (path / "scripts" / "hook_runner.py").is_file()
    )


def _find_latest_backup(target: Path) -> Path | None:
    for candidate in reversed(_backup_directories(target)):
        if _valid_backup(candidate):
            return candidate
    return None


def _replace_target_with_backup(target: Path, backup: Path) -> None:
    """Stage a copy, move the current target aside, then swap the copy in.

    The old target is only deleted after the restore succeeds. If the final
    rename fails, the previous target is put back so a rollback can never leave
    a half-restored directory.
    """
    token = f"{os.getpid()}-{time.time_ns()}"
    staging = target.parent / f".{target.name}.harness-restore-{token}"
    previous = target.parent / f".{target.name}.harness-previous-{token}"
    moved_previous = False
    try:
        shutil.copytree(backup, staging, ignore=BACKUP_IGNORE)
        if target.exists():
            target.replace(previous)
            moved_previous = True
        staging.replace(target)
    except BaseException:
        if moved_previous and not target.exists():
            try:
                previous.replace(target)
                moved_previous = False
            except OSError:
                pass
        raise
    finally:
        if staging.exists():
            shutil.rmtree(staging, ignore_errors=True)
        if moved_previous and target.exists():
            shutil.rmtree(previous, ignore_errors=True)


def _launcher_command(event_arg: str) -> str:
    if os.name == "nt":
        return f"cmd /c bin\\hook.cmd {event_arg}"
    return f"sh bin/hook {event_arg}"


def _deployed_hook_timeouts(target: Path) -> dict[str, int]:
    """Read optional hook timeouts from settings next to the deployed plugin."""
    try:
        from harness.core.settings import get_setting, load_settings

        settings, _warnings = load_settings(target)
    except Exception:
        return {}
    timeouts: dict[str, int] = {}
    for event, key in (
        ("Stop", "hooks.stop_timeout"),
        ("PostToolUse", "hooks.post_tool_use_timeout"),
    ):
        value = get_setting(settings, key, None)
        if type(value) is int and value > 0:
            timeouts[event] = value
    return timeouts


def _rewrite_hook_entries(
    entries: object, event_arg: str, timeout: int | None = None
) -> bool:
    changed = False
    if not isinstance(entries, list):
        return changed
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        if "command" in entry or entry.get("type") == "command":
            entry["command"] = _launcher_command(event_arg)
            if timeout is not None and entry.get("timeout") != timeout:
                entry["timeout"] = timeout
            changed = True
        nested = entry.get("hooks")
        if isinstance(nested, list) and _rewrite_hook_entries(
            nested, event_arg, timeout
        ):
            changed = True
    return changed


def _rewrite_deployed_hooks(target: Path, dry_run: bool) -> list[str]:
    """Point the deployed hooks at ``bin/hook`` instead of a literal python name.

    The event key determines the argument, so an older command string (``py``,
    ``python3``, an absolute interpreter path, ...) is irrelevant. Type,
    matcher, and group structure are left untouched. Stop and PostToolUse
    timeouts honor ``harness.toml`` / ``harness.json`` when present.
    """
    if dry_run:
        _log("  + rewrite hooks.json to use the bin/hook launcher", dry_run)
        return []

    path = target / "hooks.json"
    if not path.exists():
        return []
    try:
        hooks = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        return [f"could not rewrite hooks.json: {exc}"]
    if not isinstance(hooks, dict):
        return ["could not rewrite hooks.json: expected a JSON object"]

    timeouts = _deployed_hook_timeouts(target)
    changed = False
    for group in hooks.values():
        if not isinstance(group, dict):
            continue
        for event, entries in group.items():
            event_arg = _HOOK_EVENT_ARGS.get(event)
            if event_arg and _rewrite_hook_entries(
                entries, event_arg, timeouts.get(event)
            ):
                changed = True
    if not changed:
        return []

    text = json.dumps(hooks, indent=2, ensure_ascii=False) + "\n"
    try:
        json.loads(text)  # validate before touching the deployed file
    except json.JSONDecodeError as exc:  # pragma: no cover - defensive
        return [f"could not rewrite hooks.json: {exc}"]

    _log("  + rewrite hooks.json to use the bin/hook launcher")
    temporary = path.with_name(f".{path.name}.harness-tmp-{os.getpid()}")
    try:
        temporary.write_text(text, encoding="utf-8", newline="\n")
        temporary.replace(path)
    except OSError as exc:
        return [f"could not rewrite hooks.json: {exc}"]
    finally:
        temporary.unlink(missing_ok=True)
    return []


def _record_interpreter(target: Path, dry_run: bool) -> None:
    """Pin the interpreter that ran the installer for the hook launchers.

    Without it, every hook call on Windows spawned `where py`, then `py.exe`,
    then Python: about 100 ms of process start-up on each tool call. The
    launchers fall back to the PATH search if this interpreter disappears.
    """
    _log(f"  - record interpreter {sys.executable}", dry_run)
    if dry_run:
        return
    path = target / "bin" / "python.path"
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(str(Path(sys.executable).resolve()) + "\n", encoding="utf-8")
    except OSError as exc:
        _log(f"    (could not record interpreter: {exc})")


def _normalize_launcher_newlines(target: Path, dry_run: bool) -> None:
    """Give each launcher the line endings its shell needs.

    A ZIP download or a checkout with unusual git settings can deliver
    LF-only batch files (cmd.exe then misreads `goto` labels) or CRLF shell
    scripts (`sh` then fails with `$'\\r': command not found`).
    """
    if dry_run:
        return
    bin_dir = target / "bin"
    wanted = {"\r\n": [*bin_dir.glob("*.cmd")], "\n": [bin_dir / "hook", bin_dir / "harness"]}
    for newline, paths in wanted.items():
        for path in paths:
            if not path.is_file():
                continue
            try:
                raw = path.read_bytes()
                normalized = raw.replace(b"\r\n", b"\n")
                if newline == "\r\n":
                    normalized = normalized.replace(b"\n", b"\r\n")
                if normalized != raw:
                    path.write_bytes(normalized)
            except OSError:
                continue


def _ensure_launcher_permissions(target: Path, dry_run: bool) -> None:
    """Keep the POSIX launcher executable even if the checkout mode was lost."""
    if dry_run or os.name == "nt":
        return
    # Both the hook launcher and the `harness` CLI wrapper are run directly.
    for launcher in (target / "bin" / "hook", target / "bin" / "harness"):
        if not launcher.is_file():
            continue
        try:
            launcher.chmod(launcher.stat().st_mode | 0o111)
        except OSError:
            pass


def _reset_data(target: Path, dry_run: bool) -> None:
    _log("  - resetting memory, state, rules, and generated skills", dry_run)
    if dry_run:
        return
    for name in ("memory", "state"):
        path = target / name
        if path.exists():
            shutil.rmtree(path)
    for name in ("harness_memory.db", "harness_memory.db-wal", "harness_memory.db-shm"):
        path = target / "memory" / name
        if path.exists():
            path.unlink()
    bundled = (
        {path.name for path in (ROOT / BUNDLED_SKILLS_DIR).iterdir() if path.is_dir()}
        if (ROOT / BUNDLED_SKILLS_DIR).exists()
        else set()
    )
    skills = target / BUNDLED_SKILLS_DIR
    if skills.exists():
        for folder in skills.iterdir():
            # Only generated skills are reset. A manually authored or
            # third-party skill must survive even an explicit --reset.
            if (
                folder.is_dir()
                and folder.name not in bundled
                and _is_managed_skill(folder)
            ):
                shutil.rmtree(folder, ignore_errors=True)
    rules = target / "rules" / "AGENTS.md"
    if rules.exists():
        rules.unlink()


def _copy_bundled_skills(target: Path, force: bool, dry_run: bool) -> None:
    source = ROOT / BUNDLED_SKILLS_DIR
    if not source.exists():
        return
    destination = target / BUNDLED_SKILLS_DIR
    _log(f"  + {source.relative_to(ROOT)}/ -> {destination}/", dry_run)
    if dry_run:
        return
    destination.mkdir(parents=True, exist_ok=True)
    for folder in sorted(source.iterdir()):
        if not folder.is_dir():
            continue
        dest = destination / folder.name
        # Bundled skills are code: always refresh them. User/auto skills are
        # left alone unless --reset asked for a clean library.
        staging = destination / f".{folder.name}.harness-tmp-{os.getpid()}"
        try:
            if staging.exists():
                shutil.rmtree(staging)
            shutil.copytree(folder, staging, ignore=IGNORE)
            if dest.exists():
                shutil.rmtree(dest)
            staging.replace(dest)
        finally:
            if staging.exists():
                shutil.rmtree(staging, ignore_errors=True)


def _validate(target: Path) -> list[str]:
    problems: list[str] = []
    for filename in ("plugin.json", "hooks.json"):
        path = target / filename
        if not path.exists():
            problems.append(f"missing {filename}")
            continue
        try:
            json.loads(path.read_text(encoding="utf-8-sig"))
        except Exception as exc:
            problems.append(f"invalid {filename}: {exc}")

    manifest_path = target / "plugin.json"
    if manifest_path.exists():
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8-sig"))
            if isinstance(manifest, dict):
                extra = set(manifest) - PLUGIN_MANIFEST_FIELDS
                if extra:
                    problems.append(
                        "plugin.json has unsupported fields: " + ", ".join(sorted(extra))
                    )
                if manifest.get("name") != "harness":
                    problems.append("plugin.json name must be 'harness'")
            else:
                problems.append("plugin.json must contain a JSON object")
        except Exception:
            pass

    hooks_path = target / "hooks.json"
    if hooks_path.exists():
        try:
            hooks = json.loads(hooks_path.read_text(encoding="utf-8-sig"))
            events: set[str] = set()
            for group in hooks.values():
                if isinstance(group, dict):
                    events.update(group.keys())
            missing = {"SessionStart", "PostToolUse", "Stop"} - events
            if missing:
                problems.append(f"hooks.json missing events: {', '.join(sorted(missing))}")
        except Exception:
            pass

    for path in target.rglob("*.py"):
        try:
            compile(path.read_bytes(), str(path), "exec")
        except (SyntaxError, UnicodeError, ValueError) as exc:
            problems.append(f"syntax error in {path}: {exc}")

    runner = target / "scripts" / "hook_runner.py"
    if not runner.exists():
        problems.append("missing scripts/hook_runner.py")
    return problems


def _find_hook_command(hooks: object, event: str) -> tuple[str, int | None]:
    """Return the first command configured for an event, plus its timeout."""
    if not isinstance(hooks, dict):
        return "", None
    for group in hooks.values():
        if not isinstance(group, dict):
            continue
        entries = group.get(event)
        if not isinstance(entries, list):
            continue
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            command = entry.get("command")
            if isinstance(command, str) and command.strip():
                timeout = entry.get("timeout")
                return command.strip(), timeout if isinstance(timeout, (int, float)) else None
            nested = entry.get("hooks")
            if isinstance(nested, list):
                for hook in nested:
                    if isinstance(hook, dict):
                        command = hook.get("command")
                        if isinstance(command, str) and command.strip():
                            timeout = hook.get("timeout")
                            return (
                                command.strip(),
                                timeout if isinstance(timeout, (int, float)) else None,
                            )
    return "", None


def _scrub_state(target: Path) -> list[str]:
    """Re-redact transcript cursors written by an older version.

    Cursors before v2.3 stored raw transcript steps, including credentials
    pasted into prompts. The deployed code does the work so the rules match
    exactly what the hooks will use.
    """
    if not (target / "state" / "transcript_cursors").is_dir():
        return []
    env = dict(os.environ)
    env["HARNESS_PLUGIN_DIR"] = str(target)
    env["PYTHONIOENCODING"] = "utf-8"
    code = (
        "import json, sys; sys.path.insert(0, sys.argv[1]); "
        "from harness.core.incremental import scrub_cursor_files; "
        "print(json.dumps(scrub_cursor_files()))"
    )
    try:
        completed = subprocess.run(
            [sys.executable, "-X", "utf8", "-c", code, str(target)],
            cwd=str(target),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=120,
            env=env,
        )
    except Exception as exc:
        return [f"cursor scrub could not run: {exc}"]
    if completed.returncode != 0:
        return [f"cursor scrub failed: {completed.stderr[-300:]}"]
    try:
        stats = json.loads(completed.stdout.strip() or "{}")
    except json.JSONDecodeError:
        stats = {}
    if stats.get("rewritten") or stats.get("removed"):
        print(
            f"  - scrubbed transcript cursors: {stats.get('rewritten', 0)} rewritten, "
            f"{stats.get('removed', 0)} removed"
        )
    return []


def _smoke_test(target: Path) -> list[str]:
    """Run the exact Stop-hook command Antigravity will run after deployment."""
    problems: list[str] = []
    hooks_path = target / "hooks.json"
    try:
        hooks = json.loads(hooks_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        return [f"cannot read hooks.json for the smoke test: {exc}"]
    command, configured_timeout = _find_hook_command(hooks, "Stop")
    if not command:
        return ["hooks.json has no Stop command to smoke-test"]

    arguments = shlex.split(command, posix=os.name != "nt")
    if not arguments:
        return ["Stop hook command is empty"]
    timeout = max(30, min(int(configured_timeout or 30), 120))
    env = dict(os.environ)
    env["HARNESS_PLUGIN_DIR"] = str(target)
    env["PYTHONIOENCODING"] = "utf-8"
    try:
        completed = subprocess.run(
            arguments,
            cwd=str(target),
            input="{}",
            text=True,
            encoding="utf-8",
            errors="replace",
            capture_output=True,
            timeout=timeout,
            env=env,
        )
    except FileNotFoundError:
        return [
            f"Stop hook interpreter was not found: {arguments[0]!r}. "
            "Install it on PATH or edit hooks.json."
        ]
    except Exception as exc:
        return [f"hook smoke test could not run: {exc}"]
    if completed.returncode != 0:
        problems.append(
            f"hook runner exited {completed.returncode}: {completed.stderr[-400:]}"
        )
    try:
        payload = json.loads(completed.stdout.strip() or "{}")
    except json.JSONDecodeError:
        problems.append(f"hook runner did not return JSON: {completed.stdout[:200]!r}")
    else:
        if payload.get("decision") != "stop":
            problems.append(f"Stop hook returned {payload!r}, expected decision=stop")
    return problems


def uninstall(target: Path, dry_run: bool = False, keep_backups: int = 5) -> int:
    """Back up, then remove a Harness² deployment (never the source checkout)."""
    target = target.expanduser().resolve()
    if not target.exists():
        print(f"Error: target does not exist: {target}.")
        return 2
    problem = _target_problem(target)
    if problem:
        print(f"Error: {problem}.")
        return 2
    if not _is_harness_installation(target):
        print(
            f"Error: {target} does not look like a Harness² plugin; "
            "refusing to uninstall."
        )
        return 2

    print(f"Uninstalling Harness² from: {target}")
    try:
        backup, backup_problems = _backup(target, dry_run)
    except (OSError, shutil.Error) as exc:
        print(f"Error: final backup failed ({exc}); nothing was removed.")
        return 2
    if backup is None:
        print("Error: could not create a final backup; nothing was removed.")
        return 2
    if backup_problems:
        print("[ERROR] Final backup completed with problems; nothing was removed:")
        for backup_problem in backup_problems:
            print(f"  - {backup_problem}")
        print(f"Backup: {backup}")
        return 2

    _prune_backups(target, keep_backups, backup, dry_run)
    if dry_run:
        _log(f"  - remove {target}", dry_run)
        print("\nDry run complete; nothing was removed.")
        return 0

    try:
        shutil.rmtree(target)
    except OSError as exc:
        print(f"Error: could not remove {target}: {exc}")
        print(f"Backup: {backup}")
        return 1

    print("\n[SUCCESS] Harness² uninstalled.")
    print(f"Backup: {backup}")
    return 0


def rollback(target: Path, dry_run: bool = False, keep_backups: int = 5) -> int:
    """Restore the newest valid backup, backing up the current target first."""
    target = target.expanduser().resolve()
    problem = _target_problem(target)
    if problem:
        print(f"Error: {problem}.")
        return 2
    backup_source = _find_latest_backup(target)
    if backup_source is None:
        backup_root = target.parent / BACKUP_DIR_NAME
        print(
            f"Error: no valid Harness² backup found under {backup_root}."
        )
        return 2

    print(f"Rolling back Harness² in: {target}")
    print(f"Restore source: {backup_source}")
    if dry_run:
        try:
            hypothetical_backup, _ = _backup(target, True)
        except (OSError, shutil.Error) as exc:
            print(f"Error: current installation backup failed ({exc}).")
            return 2
        if hypothetical_backup is not None:
            _prune_backups(target, keep_backups, hypothetical_backup, True)
        _log(f"  + restore {backup_source} -> {target}", True)
        print("\nDry run complete; nothing was restored.")
        return 0

    try:
        current_backup, backup_problems = _backup(target, False)
    except (OSError, shutil.Error) as exc:
        print(
            f"Error: could not back up the current installation ({exc}); "
            "nothing was restored."
        )
        return 2
    if target.exists() and current_backup is None:
        print("Error: could not back up the current installation; nothing was restored.")
        return 2
    if backup_problems:
        print("[ERROR] Backup of the current installation had problems; nothing was restored:")
        for backup_problem in backup_problems:
            print(f"  - {backup_problem}")
        print(f"Backup: {current_backup}")
        return 2

    try:
        _replace_target_with_backup(target, backup_source)
    except (OSError, shutil.Error) as exc:
        print(f"Error: rollback failed; the current installation was left in place: {exc}")
        return 1

    _prune_backups(target, keep_backups, current_backup, False)
    print(f"\n[SUCCESS] Restored Harness² from {backup_source}.")
    if current_backup:
        print(f"Backup of previous installation: {current_backup}")
    return 0


def install(
    target: Path,
    force: bool = False,
    reset: bool = False,
    dry_run: bool = False,
    keep_backups: int = 5,
) -> int:
    target = target.expanduser().resolve()
    problem = _target_problem(target)
    if problem:
        print(f"Error: {problem}.")
        if "source checkout" in problem:
            print(
                "  Clone the repository somewhere else (for example a temporary folder) "
                "and run the installer from there; it copies the plugin into the target."
            )
        _set_status_reason(problem)
        return 2
    print(f"Deploying Harness² v{_source_version()} to: {target}")

    try:
        backup, backup_problems = _backup(target, dry_run)
    except (OSError, shutil.Error) as exc:
        print(f"Error: backup failed ({exc}); nothing was changed.")
        _set_status_reason(f"backup failed: {exc}")
        return 2
    if backup is not None and not backup_problems:
        _prune_backups(target, keep_backups, backup, dry_run)
    if not dry_run:
        target.mkdir(parents=True, exist_ok=True)
    if reset:
        _reset_data(target, dry_run)

    _log("Copying plugin files", dry_run)
    for filename in STATIC_FILES:
        _copy_file(ROOT / filename, target / filename, dry_run)
    for dirname in STATIC_DIRS:
        _copy_tree(ROOT / dirname, target / dirname, dry_run)

    _copy_bundled_skills(target, force=force, dry_run=dry_run)

    rules_source = ROOT / "rules" / "AGENTS.md"
    rules_target = target / "rules" / "AGENTS.md"
    if rules_source.exists() and (force or reset or not rules_target.exists()):
        _copy_file(rules_source, rules_target, dry_run)
    elif not rules_target.exists() and not dry_run:
        rules_target.parent.mkdir(parents=True, exist_ok=True)

    if not dry_run:
        (target / "memory").mkdir(exist_ok=True)
        (target / "state").mkdir(exist_ok=True)

    _normalize_launcher_newlines(target, dry_run)
    _ensure_launcher_permissions(target, dry_run)
    _record_interpreter(target, dry_run)
    rewrite_problems = _rewrite_deployed_hooks(target, dry_run)

    if dry_run:
        print("\nDry run complete; nothing was written.")
        return 0

    problems = list(backup_problems)
    problems.extend(rewrite_problems)
    problems.extend(_validate(target))
    problems.extend(_scrub_state(target))
    problems.extend(_smoke_test(target))
    doctor_problems, doctor_notes = _post_install_doctor(target)
    problems.extend(doctor_problems)
    if problems:
        print("\n[WARNING] Deployment completed with problems:")
        for problem in problems:
            print(f"  - {problem}")
        print(f"Backup: {backup}" if backup else "")
        _set_status_reason(problems[0])
        return 1

    print(f"\n[SUCCESS] Harness² v{_source_version()} deployed, smoke-tested, and checked.")
    for note in doctor_notes:
        print(f"  note: {note}")
    if backup:
        print(f"Backup: {backup}")
    print(
        "Next: restart Antigravity (quit it fully and reopen) so it loads the plugin hooks."
    )
    return 0


def _post_install_doctor(target: Path) -> tuple[list[str], list[str]]:
    """Run the deployed `harness doctor --json`.

    Critical checks (manifest, hooks, runner, Python) fail the install.
    Anything else, such as learning history from an older version or an
    invalid hand-written skill, is reported as a note: the deployment itself
    is fine and the doctor output explains what to look at.
    """
    cli = target / "scripts" / "harness_cli.py"
    if not cli.exists():
        return [f"missing {cli}"], []
    env = dict(os.environ)
    env["HARNESS_PLUGIN_DIR"] = str(target)
    env["PYTHONIOENCODING"] = "utf-8"
    try:
        completed = subprocess.run(
            [sys.executable, "-X", "utf8", str(cli), "doctor", "--json"],
            cwd=str(target),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=120,
            env=env,
        )
    except Exception as exc:
        return [f"doctor could not run: {exc}"], []
    try:
        report = json.loads(completed.stdout.strip() or "{}")
    except json.JSONDecodeError:
        return [f"doctor did not return JSON: {completed.stderr[-300:] or completed.stdout[:200]}"], []
    checks = report.get("checks") if isinstance(report, dict) else None
    if not isinstance(checks, list):
        return ["doctor returned no checks"], []
    critical = [
        f"doctor: {item.get('check')}: {item.get('detail')}"
        for item in checks
        if isinstance(item, dict) and not item.get("ok") and item.get("critical")
    ]
    notes = [
        f"doctor: {item.get('check')}: {item.get('detail')}"
        for item in checks
        if isinstance(item, dict) and not item.get("ok") and not item.get("critical")
    ]
    _log(f"  - doctor: {sum(1 for item in checks if item.get('ok'))}/{len(checks)} checks passed")
    return critical, notes


# The final line of every run is machine-readable so an agent driving the
# install can tell success from failure without parsing prose.
_STATUS = {"reason": ""}


def _set_status_reason(reason: str) -> None:
    if not _STATUS["reason"]:
        _STATUS["reason"] = " ".join(str(reason).split())[:300]


def _print_status(code: int, action: str, target: Path, dry_run: bool) -> None:
    if code == 0:
        mode = " dry_run=true" if dry_run else ""
        print(
            f"HARNESS_INSTALL: ok action={action} version={_source_version()} "
            f"target={target.expanduser().resolve()}{mode}"
        )
    else:
        reason = _STATUS["reason"] or f"exit code {code}; see the output above"
        print(f"HARNESS_INSTALL: failed action={action} reason={reason}")


def _positive_int(value: str) -> int:
    try:
        number = int(value)
    except ValueError:
        raise argparse.ArgumentTypeError("must be an integer")
    if number < 1:
        raise argparse.ArgumentTypeError("must be at least 1")
    return number


def main() -> None:
    parser = argparse.ArgumentParser(description="Deploy Harness² into Antigravity")
    parser.add_argument("--target", type=Path, default=DEFAULT_TARGET)
    parser.add_argument("--force", action="store_true", help="replace bundled rules and skills")
    parser.add_argument("--reset", action="store_true", help="back up then clear memory, state, generated skills, and rules")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument(
        "--keep-backups",
        type=_positive_int,
        default=5,
        metavar="N",
        help="keep the newest N backups after a successful backup (default: 5)",
    )
    parser.add_argument(
        "--uninstall",
        action="store_true",
        help="back up and remove the deployed plugin directory",
    )
    parser.add_argument(
        "--rollback",
        action="store_true",
        help="restore the newest valid backup, after backing up the current install",
    )
    args = parser.parse_args()

    if args.uninstall and args.rollback:
        parser.error("--uninstall and --rollback are mutually exclusive")
    if args.reset and (args.uninstall or args.rollback):
        parser.error("--reset cannot be combined with --uninstall or --rollback")
    if args.force and (args.uninstall or args.rollback):
        parser.error("--force cannot be combined with --uninstall or --rollback")

    if sys.version_info < (3, 10):
        print(f"Error: Harness² needs Python 3.10 or newer; this is {sys.version.split()[0]}.")
        _set_status_reason("Python 3.10 or newer is required")
        _print_status(2, "install", args.target, args.dry_run)
        sys.exit(2)

    if args.uninstall:
        action, code = "uninstall", uninstall(args.target, args.dry_run, args.keep_backups)
    elif args.rollback:
        action, code = "rollback", rollback(args.target, args.dry_run, args.keep_backups)
    else:
        action = "install"
        code = install(
            args.target,
            args.force,
            args.reset,
            args.dry_run,
            keep_backups=args.keep_backups,
        )
    _print_status(code, action, args.target, args.dry_run)
    sys.exit(code)


if __name__ == "__main__":
    main()
