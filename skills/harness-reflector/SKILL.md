---
name: harness-reflector
description: >-
  Use when the user asks to "learn from this task", "save this workflow as a
  skill", "update AGENTS.md with lessons", or reflect on a completed debugging
  or implementation session. Produces verified, scoped rules and reusable
  skills instead of speculative advice.
---

# Harness² Deep Reflector

Run this protocol after a substantial task when the user asks for deliberate
reflection, or when the automatic Stop cycle did not capture something valuable.

## 1. Gather evidence

- What was the user's actual goal?
- Which commands, edits, or tool calls changed the outcome?
- What failed? Include the exact error, not a paraphrase.
- What fixed it? Only count a fix that was followed by a successful command,
  passing test, or otherwise observable verification.
- Was the failure transient (timeout, rate limit, flaky network)? If so, do not
  turn it into a permanent rule.

## 2. Write bounded lessons

Good rule shape:

> `pytest` failed with `ModuleNotFoundError: No module named 'pytest'`.
> Verified fix: run `python -m pip install pytest` in the active environment.

Bad rule shape:

> Command failed. Try again or adjust parameters.

Reject rules containing timestamps, UUIDs, exit codes alone, truncated output,
or no actionable recovery.

## 3. Decide scope

- **Workspace**: project commands, paths, package manager, migrations, tests,
  local conventions. Write to the workspace `AGENTS.md`.
- **Global**: cross-project environment quirks such as PowerShell quoting,
  Python UTF-8 behavior, or hook cwd. Never copy project paths or commands into
  the global file.
- **Skill**: a repeatable multi-step procedure, not a single command and not a
  one-off incident. Require at least three meaningful steps, or two steps plus a
  verification step.

## 4. Register through Harness

```powershell
# Re-analyse the current session and update workspace rules/skills
bin\harness.cmd reflect --workspace . --force

# Inspect what was learned
bin\harness.cmd status
bin\harness.cmd skills
bin\harness.cmd rules show --target workspace
```

If the transcript is not available, create a skill with `skills install` from a
local folder, or add a concise directive to `AGENTS.md` manually. Never invent
a verified recovery.

## 5. Verify

- `bin\harness.cmd skills validate`
- `bin\harness.cmd rules sanitize --dry-run`
- Confirm the rule/skill contains no credentials or project-irrelevant noise.
- Confirm the managed block stays under the 20 KB budget and the file stays
  under Antigravity's 24 KB cap.
