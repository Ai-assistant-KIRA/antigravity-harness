# Design notes

This document explains the engineering choices behind Harness². It is written
for contributors who need to change the analyzer, storage, or hook behavior.

## Goals

1. Learn from real sessions without asking the user to label data.
2. Keep only lessons that were verified by a later successful action.
3. Keep project knowledge in the project and environment knowledge global.
4. Never overwrite user-authored content.
5. Stay local, dependency-free, and safe to run on every hook.

## Non-goals

- Replacing the model or fine-tuning it.
- Summarizing a whole conversation into a knowledge base.
- Building a general-purpose vector database.
- Enforcing policy at runtime. `AGENTS.md` is text, not a sandbox.

## Lifecycle

Harness uses four Antigravity hook events:

| Event | Work |
| :--- | :--- |
| `SessionStart` | Build a short briefing from relevant skills, verified lessons, and rule sizes. |
| `PreInvocation` | Read the newest user prompt or tool error and inject one matching lesson. |
| `PostToolUse` | Append a redacted observation to a per-conversation queue. Never block. |
| `Stop` | Parse the transcript, pair failures with recoveries, write rules/skills, update memory. |

`Stop` is the only event that can change durable state. The other events are
read-mostly and fail open. If a hook cannot parse its input, it returns valid
JSON and logs the error instead of breaking the agent.

## Evidence model

The analyzer does not treat every error as a lesson.

1. A tool call is paired with its output. Calls and outputs are processed in
   order, and the pairing expires at the next user turn.
2. A failure is a candidate only if it has an error signature and a plausible
   context.
3. A recovery requires a later successful action that addresses the same
   problem. Examples: installing a missing package, fixing a path, or changing a
   command.
4. If no recovery is found, the failure stays in memory as a candidate. It never
   becomes a rule.
5. Transient failures (network timeouts, rate limits) and repeated identical
   commands are handled conservatively.

This is a heuristic system. It can miss a real lesson or accept a weak recovery.
The conservative bias is intentional: a noisy rule is worse than no rule.

## Scope model

There are two rule scopes:

| Scope | File | Allowed content |
| :--- | :--- | :--- |
| Workspace | `<workspace>/AGENTS.md` | Project commands, paths, package manager, local conventions |
| Global | `<plugin>/rules/AGENTS.md` | Environment and tooling guidance only |

The global writer rejects project paths, filenames, and commands. This is a
content filter, not a perfect classifier. If a global rule looks project-specific,
treat it as a bug.

Both files are edited through a managed block:

```html
<!-- HARNESS:AUTONOMOUS_RULES_START -->
...
<!-- HARNESS:AUTONOMOUS_RULES_END -->
```

Everything outside the block is preserved. The managed block is capped at 20 KB
of actual encoded bytes, including CRLF expansion and UTF-16, so the file stays
under Antigravity's 24 KB per-file limit.

### Task routing

The workspace block also carries a `### Task Routing (auto-selected)` section.
On each new prompt (`PreInvocation`), after every processed `Stop` cycle, and on
`harness route`, the router picks the project-local skills and tools that match
the task and replaces that section. It is a single current selection, not a log:
a stale selection would tell the agent to load the wrong skills.

Ranking is lexical overlap with the task and detected project signals, plus
generated-skill quality and reuse. Only skills under
`<workspace>/.agents/skills` are eligible, and the task text is redacted and
injection-scanned before it is written. Tools come from a built-in catalog;
`.agents/tools.json` replaces it for projects that declare their own vocabulary.
No model is called.

## Skills

A skill is much more expensive than a rule: it consumes context and can change
agent behavior. The quality gate is deliberately strict:

- the workflow completed successfully;
- it has at least two distinct steps;
- it scores at least `0.55` on the internal quality model;
- it has a specific name, not `workflow`, `task`, or `procedure`.

Generated skills use a managed block. Updates merge new steps and pitfalls, cap
the procedure at 15 steps and 8 pitfalls, and skip the write when nothing
changed. A skill without Harness metadata is treated as user-authored and is
never overwritten.

## Memory

SQLite is the source of truth:

- WAL mode for concurrent reads and writes;
- FTS5 for search, with a `LIKE` fallback when FTS5 is unavailable;
- schema migrations tracked in a `meta` table;
- a privacy-preserving workspace fingerprint;
- one row per conversation and transcript hash for idempotence.

The database stores lessons, candidates, rule provenance, skill metadata, and
surfacing counters. `EXPERIENCE.md` is generated from the database and should
never be edited by hand.

Stores are isolated per project by default:

- global knowledge lives in `<plugin>/memory/harness_memory.db`;
- each project gets `<plugin>/memory/projects/<fingerprint>/harness_memory.db`
  and its own `EXPERIENCE.md`, outside the user's repository;
- a new project database is seeded once with pre-existing global *environment*
  lessons, never with another project's lessons;
- lesson ids come from a shared monotonic counter, so ids stay unambiguous across
  databases (`harness memory show <id>`);
- `--all-projects` queries union every project database with the global store.

Retrieval is outcome-aware. Lessons and skills track how often they were
surfaced. If the same failure recurs after a lesson was shown, that lesson's
ranking goes down.

## Reliability

The plugin runs inside an editor that may call hooks concurrently.

- **Per-workspace cycle locks.** Two `Stop` cycles in the same workspace are
  serialized. Different workspaces can run at the same time.
- **Per-conversation pending queues.** A busy conversation cannot starve
  another. The legacy single queue is still readable during upgrades.
- **Process-safe appends.** JSONL journal and pending-event writes use a
  per-file process lock. A partial line cannot corrupt the queue.
- **Atomic writes.** Rules and generated files are written through a temporary
  file and replaced.
- **Bounded reads.** Transcript reads are capped at 64 MB / 100,000 lines.
  Incremental analysis keeps a 400-step / 2 MB pairing window.
- **Incremental cursors.** A per-conversation cursor stores cheap head/tail
  hashes. A rewritten or truncated transcript falls back to a full parse.

The goal is not maximum throughput. The goal is that a crash, a concurrent hook,
or a long session cannot silently lose a lesson or duplicate one.

## Security

- Credentials are redacted before persistence.
- Authentication failures are not promoted into rules containing secrets.
- Learned text is scanned for prompt-injection and exfiltration patterns.
- Remote skill installation validates structure and high-risk content, not
  intent or runtime safety.
- Memory and state are local. The plugin does not make network calls.

The trust model is documented in [SECURITY.md](SECURITY.md). The short version:
a malicious transcript is still untrusted input. Review learned rules and skills
before relying on them.

## Trade-offs

| Choice | Benefit | Cost |
| :--- | :--- | :--- |
| Local heuristics, no LLM | Fast, private, deterministic, no API cost | Less semantic understanding; false positives possible |
| Verified recovery only | High signal | Many sessions produce no lesson |
| Managed blocks | Safe coexistence with user content | More complex merging |
| SQLite instead of files | Search, provenance, idempotence | Schema migrations and locking |
| Conservative skill gate | Less context pollution | Fewer generated skills |

## Testing

The test suite covers the analyzer, rule manager, skill builder, memory store,
installer, hook runner, settings, trust scanning, and CLI. The stress suite runs
the real hook runner as separate processes and checks:

- hook JSON contract for valid, malformed, and wrong-typed input;
- concurrent `PostToolUse` events without lost or duplicate observations;
- concurrent `Stop` cycles across workspaces;
- idempotence and pending-event retention;
- large transcripts and the rule-size budget;
- SQLite integrity, FTS row parity, and journal line validity.

Live validation on Antigravity 44.4.3 confirmed all four hook events, the
`PreInvocation` recall path, memory writes, and a clean `Stop` cycle.
