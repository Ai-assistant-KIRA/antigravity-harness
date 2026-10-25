---
name: harness-control
description: >-
  Use when the user asks about Harness², learned rules, memory, self-improvement,
  synthesized skills, or wants to inspect, reflect on, export, sanitize, or
  manage the Harness plugin. Covers harness status, reflect, skills, memory,
  rules, and doctor commands.
---

# Harness² Control

Harness² learns from completed sessions: it records verified failure/recovery
lessons, evolves workspace `AGENTS.md` rules, and synthesizes or refines reusable
skills. Use this skill when the user wants to inspect or operate that system.

## Commands

Prefer the wrapper installed with the plugin:

```powershell
# Windows (from the plugin directory, or add bin\ to PATH)
bin\harness.cmd status
bin\harness.cmd skills
bin\harness.cmd memory search "pytest import"
bin\harness.cmd rules show --target both
bin\harness.cmd reflect --workspace . --force
```

```sh
# macOS / Linux
bin/harness status
bin/harness skills
bin/harness memory search "pytest import"
bin/harness rules show --target both
bin/harness reflect --workspace . --force
```

From a checkout, `python scripts/harness_cli.py <command>` works from any
working directory.

## Useful operations

- `status` shows sessions, verified lessons, candidates, skills, and rule sizes.
- `skills list` shows workspace, plugin, and user skills. `skills show NAME`
  prints one. `skills validate` checks frontmatter and credentials.
- `skills install SOURCE` installs a local skill folder, `owner/repo`, GitHub
  tree URL, or raw `SKILL.md`. Use `--scope workspace` for project skills.
- `memory search QUERY` searches lessons with FTS5 when available.
- `memory export PATH` / `memory import PATH` moves the store between machines.
- `memory prune` removes stale candidates and bounds the database.
- `rules show` / `rules sanitize` inspects or repairs managed rule blocks.
- `doctor` checks deployment, hooks, rule sizes, and invalid skills.

## Rules of engagement

1. Never hand-edit the managed block in `AGENTS.md`; Harness owns it. User text
   outside the block is preserved.
2. Do not promote an unresolved error into a rule. Only a verified recovery is
   evidence.
3. Keep workspace rules in the workspace `AGENTS.md`. Global rules are for
   environment and tooling guidance only.
4. Never store credentials, tokens, private keys, or connection strings. The
   analyzer redacts before persistence, but do not rely on it as a safety net.
5. If `doctor` reports an invalid skill, fix or remove it before the next
   session; invalid frontmatter is ignored by progressive disclosure.

## What happens automatically

- `SessionStart` injects a short briefing with relevant skills and lessons.
- `PostToolUse` journals failures for diagnostics without blocking the agent.
- `Stop` analyses the transcript, updates rules and skills, and records memory.
- Re-running the same transcript is a no-op unless `--force` is passed.
