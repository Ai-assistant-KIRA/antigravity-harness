---
name: harness-handoff
description: >-
  Use when a task is long, context is getting crowded, work is being paused, or
  another agent/session must continue. Produces a compact, deterministic handoff
  with state, decisions, files, commands, failures, and the next action.
---

# Harness² Handoff

Long tasks fail in familiar ways: old assumptions survive, work is repeated,
constraints are forgotten, and the context fills with raw output. Write a handoff
before that happens.

## When to write one

- The user wants to pause, change machines, or continue later.
- The task will span more than one session or agent.
- Most of the context is exploration rather than decisions.
- A debugging attempt is unfinished and must be resumed.

## Template

```markdown
# Handoff: <task>

## Goal
One paragraph, including the user's acceptance criteria.

## Current state
- Done:
- In progress:
- Not started:
- Verified by:

## Decisions
- Decision — reason — alternatives rejected.

## Files
- `path` — what changed and why.
- `path` — context only, no changes.

## Commands
- `<command>` — passes or fails, with a short output summary.

## Failures and hypotheses
- Error — evidence — ruled out or still suspected.

## Constraints
- Rules from AGENTS.md, user preferences, compatibility requirements.

## Open questions
- Question — who can answer it, or which experiment will settle it.

## Next action
The single next concrete step, with the exact command or file.

## Verification
How the next session proves the task is complete.
```

## Rules

1. **Facts over story.** Include exact commands, error signatures, paths, and
   versions. Summarize or link raw logs.
2. **Keep proof and guesses apart.** A hypothesis must not read like a confirmed
   cause.
3. **Carry the constraints forward.** Copy the relevant `AGENTS.md` directives
   and user requirements into the handoff.
4. **No secrets.** Remove credentials, tokens, cookies, and private URLs.
5. **One next action.** Five competing next steps is not a handoff.
6. **Stay small.** Aim for one page. Move reference material into `docs/` and
   link it.

## After writing

- Save it in the repository, for example `docs/HANDOFF.md`, if the project will
  use it. Otherwise return it in the conversation for a short pause.
- Put durable rules in `AGENTS.md`, not task status.
- Record lasting verified lessons with Harness² and keep temporary state in the
  handoff.
