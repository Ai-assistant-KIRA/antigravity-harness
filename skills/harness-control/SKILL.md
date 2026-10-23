---
name: harness-control
description: >-
  Use to inspect, trigger, and manage the Harness² self-improving agent system.
  Allows viewing memory statistics, synthesized skills, evolved AGENTS.md rules, and manually triggering reflection.
---

# Harness² Control & Management

The Harness² plugin continuously improves your agent by evolving `AGENTS.md` rules and synthesizing reusable skills from completed tasks.

## Quick Reference Commands

Execute these shell commands when the user asks about Harness memory, learned rules, or skills:

```powershell
# Check current learning statistics, active skills, and evolved rules
python -m harness status

# View the catalog of all synthesized skills
python -m harness skills

# Inspect the full content of a specific synthesized skill
python -m harness skills --name <skill-name>

# Manually trigger the Propose-Probe-Compose reflection loop on the current session
python -m harness reflect --workspace

# Force-sync the human-readable EXPERIENCE.md log
python -m harness sync
```

## How Harness Works Under the Hood

1. **Autonomous Observation**: When you execute tasks, Harness monitors tool calls, errors, and recoveries.
2. **Contrastive Propose-Probe-Compose**: On session stop or turn completion, Harness extracts:
   - What failed and what fixed it (contrastive invariants).
   - Reusable multi-step procedures (skills).
   - Standing constraints and environment quirks.
3. **No Cold Starts**: Newly synthesized skills and evolved rules are automatically indexed and available in future sessions.
