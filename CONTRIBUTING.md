# Contributing to Antigravity Harness

Thanks for taking the time to improve the project. This is a small Python
codebase with a strong safety rule: do not make the harness noisier.

## Before you start

- For a bug, open an issue with the bug report template.
- For a large change, open an issue first so we can agree on the scope.
- For a security problem, follow [SECURITY.md](SECURITY.md). Do not open a
  public issue.

## Development setup

Requirements: Python 3.10 or newer. There are no third-party runtime
dependencies.

```powershell
git clone https://github.com/Ai-assistant-KIRA/antigravity-harness.git
cd antigravity-harness
python -m unittest discover -s tests -p "test_*.py" -v
python scripts/stress_test.py --quick
python install_plugin.py --dry-run
```

The tests set `HARNESS_PLUGIN_DIR` and `HARNESS_GEMINI_CONFIG_DIR` to temporary
directories. They must never touch your real plugin memory or rules.

## What to work on

Good contributions:

- a reproducible bug with a regression test;
- a smaller, clearer implementation of an existing function;
- a Windows, macOS, or Linux compatibility fix;
- a documentation fix that makes installation or recovery easier;
- a new test for a failure mode that is currently untested.

Please avoid:

- new runtime dependencies;
- broad refactors that are not tied to a bug;
- rules or skills that are generated from a single unverified failure;
- changes that overwrite user-authored content outside the managed block;
- commits that include transcripts, databases, credentials, or personal paths.

## Safety invariants

These are not style preferences. A pull request that weakens them will not be
merged.

1. A failure becomes a rule only after a later successful recovery.
2. Unresolved failures stay candidates and never become rules.
3. Workspace lessons never enter the global rules file.
4. User-authored skills and text outside the managed block are preserved.
5. Credentials are redacted before persistence.
6. Replaying the same transcript does not double-count a lesson.
7. Hook failures are logged and return valid JSON. They do not break the agent.

If your change touches the analyzer, rule writer, skill writer, or memory store,
add a test that proves the invariant still holds.

## Code style

- Python 3.10+ with type hints on public functions.
- Standard library only. `sqlite3`, `pathlib`, `json`, and `unittest` are the
  main tools.
- Keep functions small and name the failure mode in the test.
- Prefer explicit paths and atomic writes over clever abstractions.
- Keep comments about *why*, not *what*.
- Do not reformat unrelated code in the same pull request.

## Tests

Run the full suite before opening a pull request:

```powershell
python -m unittest discover -s tests -p "test_*.py" -v
python scripts/stress_test.py --quick
```

For a concurrency or Windows locking change, also run:

```powershell
python scripts/stress_test.py
```

If a test needs a transcript, generate it in a temporary directory. Do not commit
a real conversation.

## Documentation

- Use simple English and short sentences.
- Put user-facing install steps in [INSTALL.md](INSTALL.md).
- Put architecture and trade-offs in [docs/DESIGN.md](docs/DESIGN.md).
- Add a line to [CHANGELOG.md](CHANGELOG.md) for user-visible changes.
- Keep commands copy-pasteable and test them on the platform you claim.

## Pull request checklist

- [ ] The change is focused on one problem.
- [ ] Tests pass locally.
- [ ] A regression test was added for a bug fix.
- [ ] Safety invariants are unchanged or strengthened.
- [ ] README, INSTALL, or docs were updated if behavior changed.
- [ ] CHANGELOG has an entry for user-visible changes.
- [ ] No secrets, transcripts, databases, or personal paths are included.

Use an imperative commit message, for example:

```text
Fix Windows lock retry on a sharing violation
Add a regression test for duplicate Stop cycles
Document the one-prompt Antigravity install
```

## Release process

Maintainers:

1. Update the version in `harness/__init__.py`, `pyproject.toml`, and
   `plugin.json`.
2. Move the changelog entries into a dated release section.
3. Run the unit tests, the stress test, and a clean install into a temporary
   target.
4. Commit, tag `vX.Y.Z`, and publish a GitHub release.
5. Upload `assets/social-preview.png` in the repository settings if the card
   changed.

## License

By contributing, you agree that your contribution is licensed under the
Apache-2.0 license used by this project. See [LICENSE](LICENSE).

## Community standards

All participants are expected to follow [CODE_OF_CONDUCT.md](CODE_OF_CONDUCT.md).
