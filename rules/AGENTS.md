<!-- HARNESS:AUTONOMOUS_RULES_START -->
## Harness Evolved Directives (Global Environment)
> Maintained automatically by Harness². User content outside this block is preserved.
> Verified failure/recovery lessons only; no transcript timestamps or exit-code noise.

### Standing Project Directives
- Use existing learned skills before re-deriving a known multi-step procedure.
- Use the harness CLI (`bin/harness.cmd` on Windows, `bin/harness` elsewhere) to inspect memory and validate skills.
- Keep the Harness-managed `AGENTS.md` block below 20 KB; Antigravity caps each rules file at 24 KB.
- Never persist credentials, tokens, private keys, or connection strings in rules, skills, or memory exports.

### Environment & Tooling Quirks
- PowerShell: quote paths containing spaces and avoid multi-line inline commands; write complex logic to a temporary script.
- Python on Windows: run with `-X utf8` when output contains non-ASCII, and prefer `python -m module` over bare entry points.
- Antigravity hooks run with the plugin directory as the working directory; resolve project paths from the hook payload instead of the process cwd.
- When a command fails, capture the full stderr and the first actionable error line; an exit code alone is not a diagnosis.

### Learned Pitfalls & Recovery Invariants
- Write and read text files as UTF-8 to avoid cp1252 corruption on Windows.
- Do not copy project-specific commands or paths into the global rules file; put them in the workspace rules instead.
- Only promote a lesson after observing a successful recovery; unresolved failures remain candidates.
- Before overwriting a skill, check whether it is manually authored; Harness must never replace user content.

<!-- HARNESS:AUTONOMOUS_RULES_END -->
