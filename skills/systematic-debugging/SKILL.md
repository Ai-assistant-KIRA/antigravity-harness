---
name: systematic-debugging
description: >-
  Use when debugging a crash, failing test, flaky behavior, regression, build
  failure, or unexpected tool output. Enforces reproduction, evidence, root
  cause analysis, and verification before a fix, instead of shotgun changes.
---

# Systematic Debugging

## The rule

**Do not patch code until you can explain the failure.** A change that happens
to work hides the cause and creates the next regression.

## 1. Capture the failure

1. Write down the exact command, environment, inputs, and complete error.
2. Decide whether the failure is consistent, intermittent, or tied to one
   machine or cache.
3. Reduce it to the smallest reproduction. When possible, encode it as a test
   that fails for the expected reason.
4. Check whether a clean checkout or empty cache changes the result.

```sh
# Use the project's real commands, not these examples.
python -m pytest tests/test_feature.py -x -vv
npm test -- --runInBand path/to/test
cargo test failing_test -- --nocapture
```

If the failure will not reproduce, keep the logs and say so. A guess is not a
diagnosis.

## 2. Narrow the search

- Start at the deepest stack frame that points to project code.
- Compare with recent work: `git diff`, `git log -p -- <path>`, or `git bisect`.
- Split the problem by input, feature flag, environment variable, or dependency.
- Turn off one variable at a time and write down each result.
- Test one hypothesis per experiment. State the evidence that would prove it
  wrong before running the experiment.
- Separate the first error from errors that only follow it.

## 3. Prove the cause

Write the chain in plain language:

> X happens, so Y receives Z. The code expects W, and therefore fails with E.

Prove each link with a small experiment. If a link is missing, go back and narrow
further. Do not change production code during this step.

## 4. Fix, then verify

1. Make the smallest change that removes the proven cause.
2. Run the original reproduction. It must pass.
3. Run nearby tests, the type checker, the linter, and the build.
4. Check the behavior around the change for side effects.
5. Remove temporary logs, flags, and scratch files.
6. Record the failure and its verified recovery so the lesson is available later.

```powershell
bin\harness.cmd reflect --workspace . --force
```

## Common mistakes

- Editing several files and hoping one change works.
- Running the same command again without reading the error or changing an input.
- Catching the exception, widening a handler, or skipping the test.
- Saying "fixed" without a passing reproduction.
- Treating a timeout or rate limit as a permanent code bug.
- Adding a package before proving the current one is at fault.

## Report

Finish with: root cause, evidence, fix, checks run, and remaining risk. If
something is still unverified, name it.
