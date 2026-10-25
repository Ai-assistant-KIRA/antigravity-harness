## Summary

<!-- What changed, and why? -->

## Type of change

- [ ] Bug fix
- [ ] New feature
- [ ] Documentation
- [ ] Installer or release change
- [ ] Test or CI change
- [ ] Refactor with no behavior change

## Related issue

Closes #

## How it was tested

<!-- Paste the commands you ran and the result. -->

```text
python -m unittest discover -s tests -p "test_*.py" -v
python scripts/stress_test.py --quick
```

## Safety checklist

- [ ] A failure still becomes a rule only after a verified recovery.
- [ ] Unresolved failures still stay candidates.
- [ ] Workspace lessons still cannot enter global rules.
- [ ] User-authored skills and text outside the managed block are preserved.
- [ ] Credentials are still redacted before persistence.
- [ ] Replaying a transcript still does not double-count a lesson.
- [ ] No secrets, transcripts, databases, or personal paths are included.

## Documentation

- [ ] README, INSTALL, or docs updated if behavior changed.
- [ ] CHANGELOG has an entry for user-visible changes.
