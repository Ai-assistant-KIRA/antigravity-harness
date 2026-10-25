---
name: harness-skill-author
description: >-
  Use when creating, reviewing, or improving an Antigravity skill (SKILL.md), or
  when a repeated workflow should become a reusable skill. Covers frontmatter,
  trigger descriptions, progressive disclosure, verification, and safe
  attribution.
---

# Harness² Skill Author

A skill is a short procedure that an agent can reuse. It is not a changelog, a
tutorial, or a saved transcript.

## When a skill is worth writing

Write one when:

- the same workflow will come back in other sessions or projects;
- the steps are stable enough to repeat;
- there is a real way to check the result;
- another agent would otherwise rediscover the setup, flags, or traps.

Do not write one for a single command, a one-off incident, or a workflow that
never succeeded.

## Shape

```markdown
---
name: kebab-case-name
description: >-
  Use when ... Name the symptoms, tools, and task types that should trigger
  this skill. Keep it under 1024 characters.
---

# Title

## When to Use
- concrete triggers

## Procedure
1. command or action, with the expected result
2. ...

## Verification
- how to prove it worked

## Pitfalls
- error → verified fix
```

Keep the main file short. Move long references, scripts, and examples into
`references/`, `scripts/`, or `assets/` and link them. That is progressive
disclosure: the description is always visible, and the body loads only when the
skill is chosen.

## Writing rules

- **Name:** lowercase kebab-case, identical to the folder name.
- **Description:** explain *when to use it*, not only what it is. Include likely
  keywords and error strings.
- **Procedure:** use commands that can be copied and run. Mark placeholders.
  Prefer the project's own scripts over ad-hoc shell.
- **Verification:** require something observable, such as a test, build, health
  check, exit code, or file.
- **Pitfalls:** include only fixes that were actually verified.
- **No secrets:** no tokens, keys, cookies, private URLs, or customer data.
  Remove paths that identify a person.
- **No transcript noise:** no timestamps, UUIDs, bare exit codes, or truncated
  output.
- **Attribution:** if you copy or adapt someone else's text, keep their license
  notice and a source link in the skill and in `NOTICE`.

## Review checklist

- [ ] Frontmatter parses; the name is kebab-case; the description names triggers.
- [ ] At least two distinct steps; three for a generated skill.
- [ ] Verification is explicit.
- [ ] Check for duplicates with `bin/harness skills list`.
- [ ] `bin/harness skills validate` passes.
- [ ] No credentials or private data.
- [ ] The file is well below the 30 KB hard limit.
