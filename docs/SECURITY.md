# Security and Privacy Model

Harness² learns from completed sessions and then injects the result back into
future agent context. That feedback loop is useful, but it is also a trust
boundary: a learned rule or skill is executable advice, not inert documentation.

## What Harness² learns and where it goes

| Artifact | Location | Injection point |
| :--- | :--- | :--- |
| Workspace rules | `AGENTS.md` managed block | Antigravity reads the file each session |
| Global plugin rules | `<plugin>/rules/AGENTS.md` | Plugin instructions |
| Verified lessons | `memory/harness_memory.db` | `SessionStart` briefing and `EXPERIENCE.md` |
| Synthesized skills | workspace/plugin `skills/*/SKILL.md` | Progressive disclosure: description first, body on selection |
| State journals | `state/events.jsonl`, `state/pending/*.jsonl` | Diagnostics only, not directly injected |
| Hook diagnostics | `state/hook_errors.log` | Operator inspection only |

Transcript text, tool output, failure messages, and remote skill bodies are all
**untrusted input**. The analyzer extracts from them, but extraction does not
make their contents trustworthy.

## Trust levels

1. **Bundled skills** — reviewed and attributed in `NOTICE`. They are the
   highest-trust skill source, but they are still text and can be wrong or
   stale.
2. **User-authored skills and rules** — trusted to the same degree as the user.
   Harness² avoids overwriting manual skills, but it does not prove their safety.
3. **Remote installed skills** — untrusted. Installation validates structure,
   archive safety, known high-risk content, and *some* intent heuristics. It does
   **not** sandbox or execute the skill, and it cannot prove the author is benign.
4. **Transcript-derived content** — untrusted by default. A tool output or web
   page can contain instructions aimed at the next model. Derived skills are
   validated before write, and credential-looking values are redacted, but the
   underlying text can still be adversarial.

## Threats

### Prompt injection

A skill, rule, or lesson can tell the agent to ignore earlier instructions,
adopt a different role, reveal its system prompt, hide behavior from the user,
or follow embedded instructions in fetched content. The scanner flags common
override phrases and hidden Unicode/control characters at **high** severity.
These phrases block installation and skill validation.

### Exfiltration

Malicious content can pipe a download into a shell, post a local credential file
to a webhook, or direct the agent to read `~/.ssh`, `.env`, or cloud credential
files and send them elsewhere. Pipe-to-shell chains, credential-file uploads,
and webhook/paste endpoints are detected. Credential-file uploads and
pipe-to-shell chains are **high** severity; a bare webhook endpoint is a
**medium** warning.

### Destructive commands

Recursive deletion of a system or home root, formatting a volume, recursively
deleting a drive root, force-pushing to `main`/`master`, and `DROP TABLE`
without `IF EXISTS` are flagged. Most are **medium** warnings; they escalate to
**high** when the same line also contains exfiltration or an override. Ordinary
commands such as `rm -rf node_modules` and `git push --force-with-lease` are not
blocked.

### Credential disclosure

The scanner warns when text asks the agent or user to send, paste, export, or
reveal a credential. That is a **medium** warning because legitimate setup
instructions sometimes mention API keys. Do not treat a warning as permission.

## Scanning behaviour

`harness.core.trust.scan_text()` scans logical lines and returns `Finding`
objects with `category`, `severity`, `message`, `line`, a redacted `snippet`,
and `source`. `scan_skill()` scans `SKILL.md` plus text-like support files,
bounded to 50 files and 1 MB per file. Binary, oversized, hidden, and symlinked
files are skipped.

Only **high** findings block installation (`is_safe_to_install()`). Medium
findings are surfaced as `untrusted content:` warnings. Low findings are
informational. Skill validation applies the same rule: high findings are errors,
medium findings are warnings.

This is a heuristic scanner. False positives are possible, and a determined
author can phrase an instruction in a way no pattern recognizes. A clean scan is
not a guarantee of safety.

## Redaction limits

Learned content is passed through `harness.core.redaction` before persistence.
It recognizes common high-confidence formats: GitHub/GitLab tokens, Slack
tokens, cloud API keys, JWTs, PEM private-key blocks,
`Authorization`/`Bearer` headers, connection strings, and `*_TOKEN=...` /
`*_SECRET=...` assignments. The whole PEM block is replaced; elsewhere only the
credential value is replaced.

Redaction is pattern-based and incomplete:

- Unknown token formats, split secrets, encrypted blobs, and credentials inside
  images or archives can survive.
- Context can hide a secret from the regex while still exposing it to a model.
- Over-redaction is possible when a value merely resembles a credential.

Never place a real credential in a transcript, skill, rule, or memory record.
Rotate any credential that may have been persisted.

## Retention

- `memory/harness_memory.db` stores sessions, lessons, skills index metadata, and
  rule-edit summaries. `harness memory prune` defaults to at most 1000 sessions,
  2000 lessons, and 500 rule edits; candidates expire after 30 days and verified
  lessons after 365 days.
- `state/events.jsonl` and `state/pending/*.jsonl` are transcript-derived
  diagnostics. They have `.1` rotations. Treat them as sensitive; delete or
  rotate them when no longer needed (for example after 30 days).
- `state/hook_errors.log` rotates at 500 KB and may contain error excerpts.
- `memory/EXPERIENCE.md` is a generated summary derived from the database.
- Workspace `AGENTS.md` and `.agents/skills/` are project files and may be
  committed to git. Review them before sharing a repository.

Run `harness privacy audit` (or call `harness.core.trust.audit_privacy()`)
before sharing a machine, archiving a checkout, or reporting an issue. It
reports file existence/sizes, database row counts, learned-skill paths, and
retention guidance without dumping memory content. `--json` produces the same
report for scripting.

## Review, disable, and remove

Run these from the checkout or the deployed plugin wrapper:

```powershell
bin\harness.cmd rules show --target both
bin\harness.cmd rules sanitize --target both --dry-run
bin\harness.cmd skills list
bin\harness.cmd skills validate
bin\harness.cmd skills remove NAME --scope workspace
bin\harness.cmd memory list --all-projects
bin\harness.cmd memory prune
bin\harness.cmd privacy audit
```

- Use `rules forget` / `rules sanitize` to remove or repair learned rule blocks.
- Use `skills remove` for a skill you no longer trust. Deleting the folder also
  removes it from progressive disclosure.
- Use `memory delete ID`, `memory forget`, or `memory reset` to remove lessons.
- To stop learning entirely, remove the Harness hook entries from the
  Antigravity plugin configuration or uninstall the plugin. Existing files
  remain until you delete them.

## Non-guarantees

Harness² does not sandbox tools, proxy network access, inspect binaries, or
enforce policy at runtime. Scanning covers known textual patterns only. Remote
skill installation validates structure and high-risk content, but it does not
execute or sandbox the skill, and it does not establish that the skill's intent
is safe. Treat all learned and third-party content as untrusted until reviewed.
