# FAQ

## Does it send my code or transcripts anywhere?

No. The plugin is local-first. It reads Antigravity transcripts on your machine,
stores lessons in a local SQLite database, and does not call an LLM or a remote
service.

## Does it call an LLM?

No. Analysis is local and heuristic. That keeps it fast, private, and free to
run. The trade-off is that it can miss lessons or accept a weak recovery.

## Which Antigravity versions are supported?

It needs an Antigravity build with plugin support. The current release is tested
on Antigravity **44.4.3** on Windows. CI also runs the Python code on Ubuntu.
If you test another version, please open an issue with the result.

## What files does it change?

- `<workspace>/AGENTS.md`, only inside the Harness-managed block.
- `<plugin>/rules/AGENTS.md`, global environment and tooling rules.
- `<workspace>/.agents/skills/<name>/SKILL.md`, generated skills.
- `<plugin>/memory/` and `<plugin>/state/`, runtime data.

It does not edit your source code.

## Will it overwrite my `AGENTS.md`?

No. It only replaces text between its own markers:

```html
<!-- HARNESS:AUTONOMOUS_RULES_START -->
...
<!-- HARNESS:AUTONOMOUS_RULES_END -->
```

Everything outside that block is preserved.

## Will it overwrite a skill I wrote?

No. A skill without Harness metadata is treated as user-authored and is never
overwritten. Generated skills use their own managed block.

## Why did my last session not create a rule?

A failure becomes a rule only when a later action fixes it. An unresolved failure
stays in memory as a candidate. This is intentional: a noisy rule is worse than
no rule.

## How do I see what it learned?

```powershell
& "$HOME\.gemini\config\plugins\harness\bin\harness.cmd" status
& "$HOME\.gemini\config\plugins\harness\bin\harness.cmd" memory search "pytest"
& "$HOME\.gemini\config\plugins\harness\bin\harness.cmd" skills list
& "$HOME\.gemini\config\plugins\harness\bin\harness.cmd" rules show --target both
```

## How do I remove a bad rule or skill?

```powershell
& "$HOME\.gemini\config\plugins\harness\bin\harness.cmd" rules why "rule text"
& "$HOME\.gemini\config\plugins\harness\bin\harness.cmd" rules forget "rule text"
& "$HOME\.gemini\config\plugins\harness\bin\harness.cmd" skills remove NAME --yes
```

You can also edit the text outside the managed block by hand. Harness will leave
your edits alone.

## Does it work with multiple projects?

Yes. Lessons are scoped by a privacy-preserving workspace fingerprint. Project
knowledge stays in the project. Global rules are filtered to environment and
tooling guidance.

## Does it work on macOS and Linux?

The Python code is cross-platform and CI runs on Ubuntu. The installer has
Windows and POSIX wrappers. The current live validation was on Windows with
Antigravity 44.4.3. If you hit a platform-specific issue, please report it.

## How do I uninstall it?

```powershell
python install_plugin.py --uninstall
```

This backs up the plugin, including the memory database, then removes the
installed target. To restore a backup:

```powershell
python install_plugin.py --rollback
```

## Is it production-ready?

Yes, for local development use. The code is tested, the install is reversible, and
the plugin fails open. The analyzer is heuristic and learned text is untrusted, so
review rules and skills before relying on them in a sensitive environment.

## How do I contribute?

Read [CONTRIBUTING.md](../CONTRIBUTING.md). Bug reports, regression tests, and
documentation fixes are welcome. For security issues, follow
[SECURITY.md](../SECURITY.md).
