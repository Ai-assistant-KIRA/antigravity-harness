# Changelog

All notable changes to Harness² are documented here.

## 2.2.0 — 2026-10-26

### Added

- `harness skills why`: replays recent sessions and reports, per task, whether a
  workflow was found, its name and quality, and the first gate that blocked it.
- Skill extraction now evaluates every actionable task in a session instead of
  only the first prompt, and caps output at three workflows per session.

### Fixed

- Skills are no longer lost because a later, unrelated task ended the session
  "partial": success is decided per task segment, after any verified recoveries.
- The actionable-goal gate accepts real multi-sentence requests (up to 48 words)
  while still rejecting questions and research prompts.
- Non-code work (video/audio edits, MCP setup, PowerShell) gets a derived skill
  name instead of being dropped by the fixed rule list.

### Changed

- Two tasks in one session can no longer overwrite each other's skill file; the
  second workflow gets a distinct name.

### Added (routing and isolation)

- Project-aware task routing. Every new prompt, every completed `Stop` cycle, and
  `harness route "task"` select the project-local skills and tools that match the
  work and write them into the managed `### Task Routing (auto-selected)` section
  of that project's `AGENTS.md`. The section is replaced per task, never
  accumulated, and preserves all user text outside the managed block.
- Routing is deterministic and offline: lexical overlap plus project signals,
  generated-skill quality, and evidence of reuse. No model call is made.
- `.agents/tools.json` lets a project declare its own tool vocabulary; when
  present it replaces the built-in catalog.
- Project memory isolation: each workspace gets
  `<plugin>/memory/projects/<fingerprint>/harness_memory.db` and `EXPERIENCE.md`.
  A new project database is seeded once with pre-existing global environment
  lessons; another project's lessons are never imported.
- `harness route` and routing output in `harness reflect`.

### Changed

- Lesson ids are allocated from a shared monotonic counter, so
  `harness memory show <id>` cannot resolve to a different lesson in another
  project now that stores are physically separate.
- `harness memory search/list --all-projects` unions every project database and
  the global store instead of only scanning one database.
- Skill discovery is project-local by default; global and other-project skills
  are not routed into a workspace.

### Configuration

- New settings default to on: `[project] isolate_memory`, `project_skills_only`,
  and `[routing] enabled`, `auto_apply`, `max_skills`, `max_tools`.

## 2.1.0 — 2026-10-25

First public release.

This release includes hardening from an end-to-end audit of the deployed plugin
and a new stress suite. The audit found real process-contention corruption,
dropped Stop cycles, write amplification on every hook open, and false candidate
lessons extracted from successful file reads.

### Fixed

- JSONL journal and pending-event appends now use a per-file process lock.
  Concurrent `PostToolUse` hooks can no longer interleave partial lines, lose
  events, or duplicate observations.
- File locks retry transient Windows sharing violations instead of immediately
  giving up, and pending-event lock failures are recorded in
  `state/hook_errors.log` rather than silently dropping an observation.
- Pending-event read, append, and clear operations share one lock. `Stop` only
  removes the event IDs consumed by a processed cycle; skipped sessions retain
  their observations for a later turn.
- Concurrent `Stop` cycles wait up to 30 seconds for the cycle lock instead of
  failing as `busy` after 1.5 seconds.
- Memory opens use a schema-ready fast path. Normal `SessionStart`/`Stop` hooks
  no longer take `BEGIN IMMEDIATE` or rebuild the FTS index on every call.
- Successful `read_file`/`view_file`/search output is no longer scanned as a
  diagnostic. Source text such as `raise RuntimeError(...)` or `# error: ...`
  cannot create a candidate lesson; explicit file errors still can.
- Transcript analysis is bounded to 64 MB / 100,000 lines. Oversized
  transcripts keep the newest steps and report a truncation warning.
- Rule merging deduplicates once instead of once per inserted rule, removing an
  O(n³) path when many rules are applied at once.
- The 20 KB managed-rule budget counts the bytes actually written, including
  CRLF expansion and UTF-16 encoding, instead of assuming LF/UTF-8.
- `Stop` reads transcripts incrementally through a per-conversation cursor
  instead of re-parsing the whole growing file every turn. The cursor validates
  cheap head/tail hashes and falls back to a full parse on rewrite or truncation.
- The bounded failure/recovery pairing window no longer emits a user-visible
  warning on every long-session Stop; it is an internal cap, not an analysis
  failure.

### Added

- Public GitHub repository with a one-prompt Antigravity install, release
  metadata, community health files, and a contributor guide.
- Live validation on Antigravity 44.4.3: `SessionStart`, `PreInvocation`,
  `PostToolUse`, and `Stop` all fired in a real session with no hook errors.
- Per-workspace cycle locks and per-conversation pending queues. Unrelated
  projects reflect concurrently, and one busy conversation cannot starve
  another; the legacy queue remains readable during upgrades.
- `PreInvocation` prompt-aware recall: extracts the newest user request or tool
  error from the transcript tail, searches verified lessons and matching skills,
  injects one compact `ephemeralMessage`, and dedupes per prompt/error signature.
- Bundled `harness-recall` skill and `harness recall QUERY` CLI command as a
  manual fallback.
- Rule provenance (schema v3): `rules_index`, `rule_hash`, occurrence/surfacing
  counters, `rules why/forget/conflicts`, conflict supersede, and value-based
  rule eviction.
- Outcome-aware retrieval (schema v4): surfaced counts and post-surface
  recurrence on lessons, surfaced counts on skills, utility ranking in search and
  briefings, and `status --effectiveness`.
- `harness/core/trust.py`: prompt-injection/exfiltration scanning, high-severity
  install blocking, redacted findings, privacy audit, and `docs/SECURITY.md`.
  `harness privacy audit` reports retained state and memory counts without
  dumping content.
- Portable `bin/hook` / `bin\hook.cmd` launchers; the installer rewrites the
  deployed `hooks.json` for the current OS and keeps backups executable.
- Installer lifecycle: `--uninstall`, `--rollback`, and `--keep-backups N`
  (default 5), with backups including the memory database.
- Optional `harness/core/settings.py` configuration via `harness.toml` or
  `harness.json`, wired into rule budgets, skill caps, briefing limits, memory
  pruning defaults, and deployed Stop/PostToolUse timeouts.
- GitHub Actions CI matrix for Ubuntu/Windows and Python 3.10/3.12/3.13.
- `scripts/stress_test.py`: real-process hook contract, concurrency, idempotence,
  large-transcript, rule-budget, and SQLite/FTS integrity scenarios with
  `--quick`, `--full`, `--json`, `--keep`, and `--plugin-root` controls.
- Regression tests for concurrent `PostToolUse`, concurrent `Stop`, skipped-Stop
  pending retention, file-content false positives, transcript line caps,
  incremental cursors, pre-invocation recall, rule provenance/conflicts,
  effectiveness ranking, settings, trust scanning, and CRLF rule budgets.

## 2.0.0 — 2026-10-23

A correctness and quality release. The v1 prototype proved the concept but
polluted rules with transcript timestamps and exit codes, duplicated skill
sections, overwrote user-visible files, and wrote project rules globally.

### Fixed

- Replaced noisy transcript parsing with evidence-first analysis: FIFO tool
  output pairing, verified recovery pairing, transient-error handling, and
  unresolved failures kept as candidates.
- Rules are sanitized before persistence. Timestamps, UUIDs, bare exit codes,
  truncated output, mojibake, low-signal advice, and secrets are rejected.
- Workspace and global rules are scoped correctly. Project commands and paths
  never enter the global rules file.
- Skill updates use a managed block and are bounded/idempotent. The duplicated
  "Sequential Refinement" sections are gone.
- Manually authored skills with the same name are never overwritten.
- Memory is workspace-scoped and idempotent per conversation; re-running a
  transcript does not double-count lessons.
- Skill reuse is tracked separately from skill updates.
- Hook runner no longer hardcodes a workspace path and returns valid JSON on
  every failure path.
- Installer preserves memory, state, user rules, and generated skills; it backs
  up before resetting and smoke-tests the Stop hook.
- Rules and skills are read losslessly. Invalid encodings are reported or skipped
  instead of being rewritten with replacement characters.
- `plugin.json` now validates against the official Antigravity schema and contains
  only `$schema`, `name`, and `description`.

### Added

- SQLite schema v2 with migration from v1 and FTS5 search (LIKE fallback).
- Verified vs. candidate lessons, project fingerprints, pruning, export/import,
  workspace reset, and a clean generated `EXPERIENCE.md`.
- `harness` CLI: `status`, `reflect`, `skills` (list/show/install/remove/
  validate), `memory` (search/export/import/prune/reset), `rules` (show/
  sanitize), `doctor`, `sync`, and `install`.
- `bin/harness.cmd` and `bin/harness` wrappers that work from any cwd.
- Local and GitHub skill installation with zip-slip protection, size limits,
  license preservation, and provenance metadata.
- Bundled skills: harness-control, harness-reflector, harness-skill-author,
  systematic-debugging, verification-before-completion, and harness-handoff.
- `NOTICE` with the project copyright.
- Expanded test suite covering isolation, sanitization, budget limits,
  idempotence, hooks, installer preservation, and CLI wrappers.

### Changed

- Global rules are curated environment guidance, not an append-only log.
- `python -m harness` remains available when the plugin root is on
  `PYTHONPATH`; the wrappers are the supported zero-setup path.
- PostToolUse journals failures for diagnostics; Stop performs the authoritative
  transcript analysis.

## 1.0.0 — 2026-10-23

- Initial prototype: Propose-Probe-Compose cycle, AGENTS.md updates, skill
  synthesis, SQLite memory, SessionStart/PostToolUse/Stop hooks.
