---
id: verification
applies_to: Deciding whether a change needs an observed run, and proving it.
use_when:
  - a change has user-facing behaviour
  - tests pass but nothing has been run
not_for:
  - verifying facts, sources, or claims -- that is editorial checking, not this
selectable: false
---
# Verification

Deciding whether observed behaviour is required, and proving it.
Small by design: compose it with `{"extends": ["verification"]}`.

## The verifier

**Decide first whether an end-to-end run is needed.** A change with no
user-facing behaviour can be verified by unit coverage — confirm the tests are
green and say so in the report. Anything a user interacts with needs a real run:
*code review is not verification; observed behaviour is.* When unsure, ask
rather than guessing.

**Confirm what you are actually testing.** When several pieces are in flight,
check which worktree or branch corresponds to the piece under test. Testing the
wrong branch produces a false pass or false fail — worse than no result.

1. Confirm the runtime environment is up and running the current build. **Look
   for the project's existing build and install scripts** — README, Makefile,
   package scripts, fastlane — before assuming manual steps or inventing a build
   path.
2. Locate the flows matching the piece under test. If the test repository is not
   where expected, ask rather than guessing a path.
3. Run each flow.
4. Capture evidence as you go: screenshots of key states and assertions, and a
   recording of the full flow, saved with the work it proves.
5. Compare observed behaviour against the requirements summary and the full
   traceability list.
6. Report pass or fail to the coordinator — **not** to the author. On failure
   include logs, which step failed, and the relevant screenshots.
7. Do not call the work verified until **every** traceability item has passed,
   by a real run or by the unit-only exception.
8. Stop after a bounded number of attempts at the same thing and report a hard
   wall rather than continuing indefinitely.

---
