---
name: verification-before-completion
description: >-
  Use before claiming a task is complete, fixed, tested, deployed, or working.
  Requires fresh command output and concrete evidence for every completion
  claim, and forbids "should work" or "probably fixed" statements.
---

# Verification Before Completion

## The rule

**Claims need evidence.** "Done" is a factual statement, and the proof is output
from a command you just ran. What happened earlier, or what the code looks like,
is not proof.

## Steps

1. Write down each claim you plan to make.
2. For each one, name the command or observation that proves it.
3. Run that check now, in this workspace.
4. Read the full output and the exit status.
5. Report the claim next to its evidence. If a check could not run, say that
   plainly instead of implying success.

## What counts as proof

| Claim | Minimum evidence |
| :--- | :--- |
| Tests pass | Test command, summary line, zero exit status |
| Build succeeds | Build command and the produced artifact |
| Bug is fixed | Regression test that failed before and passes now |
| Lint or typecheck is clean | The command and its clean output |
| API contract works | Request/response or contract test output |
| UI works | Manual steps with a screenshot or recording, or an E2E test |
| Dependency is installed | Install from a clean environment, then an import check |
| Migration is safe | Run against a representative database and check rollback |
| Deployment is healthy | Health endpoint, logs, and version or commit confirmation |
| Performance improved | Before and after measurements on the same workload |

## How to phrase results

Use:

- "`python -m pytest` passed: 42 passed in 3.1s."
- "Build succeeded; artifact at `dist/app.js`."
- "I could not run the browser test because X, so the UI is unverified."

Avoid:

- "Should work now."
- "Probably fixed."
- "Tests likely pass."
- "I believe the build is clean."
- Any claim based only on reading code.

## When a check cannot run

Name the blocker, what you tried, and what remains unverified. Offer the smallest
next check the user can run. Never turn an unverified change into a learned rule
or a success message.

## Finish

If the task is complete and verified, record the recovery with Harness². If it is
not verified, leave it as a candidate observation, not a rule.
