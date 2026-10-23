---
name: harness-reflector
description: >-
  Use when conducting deep reflection on a complex task or when the user asks to
  "learn from this task", "save this workflow as a skill", or "update agents.md with lessons".
---

# Harness² Deep Task Reflector

This skill provides an interactive, structured protocol for deep recursive reflection (Harness² Task-Level & Harnessing-Level Recursion).

## Reflection Protocol

When completing a challenging task, follow these steps to cement learnings:

### Step 1: Identify Friction & Invariants
- What errors or friction occurred during execution? (e.g. command syntax, OS quirks, missing packages)
- What was the verified fix?
- Formulate a concise rule: `When doing X, avoid Y because Z; instead use W.`

### Step 2: Formulate Procedural Skill
If this procedure is likely to be repeated:
1. Define a clear, kebab-case skill identifier (e.g., `setup-fastapi-uv`, `docker-compose-deploy`).
2. Draft a step-by-step recipe with copy-pasteable commands and verified options.
3. List pitfalls and the exact fixes verified in this session.

### Step 3: Trigger Autonomous Registration
Run the harness reflect command or use Python to register the findings:

```powershell
python -m harness reflect --workspace
python -m harness sync
```

### Step 4: Verify
Verify that the new skill is discovered:
```powershell
python -m harness skills
```
Once registered, Antigravity will automatically load this skill in the relevant next sessions!
