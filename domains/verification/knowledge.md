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

## Filing suite evidence so it counts

A reviewer is shown the author's full-suite result through **one machine-read
channel**: the `full_suite` payload on a task message. Reporting the same facts
in prose puts nothing in that channel, so Helm tells the reviewer no evidence
exists — and the reviewer, doing as it was told, returns a blocking finding
against a branch whose suite ran clean. That has now cost review rounds on two
separate projects, each time to an author who had written the evidence out in
full.

**So file it with the command, not with a sentence:**

```
helm task evidence <task-id> --tip <sha> --command "<the suite command>" --exit 0 \
  --detail '{"packages/foo": "252/252", "packages/bar": "87 pass, 1 skipped"}'
```

`--detail` is a JSON object, not prose. File one call per suite when a project
has more than one runner — a workspace suite and a separate mobile suite are two
calls, because a package outside the workspace is not covered by the recursive
run and a reviewer cannot tell that from a single total.

What the evidence has to be able to survive:

- **Fresh.** Run it *after* the commit it claims to test, and name that tip. A
  run that predates the code proves nothing about the code.
- **Unmasked.** Read the exit status directly (`$?`), never through a pipe that
  can swallow it. Write the log to a file rather than piping it away.
- **Complete in scope.** Name every package with its counts, including any that
  sits outside the workspace and is therefore missed by a recursive run. "All
  tests pass" is not scope; a list is.
- **Clean-tree.** State that the working tree was clean at the tip, so the run
  and the diff describe the same code.

Report the same facts in your own prose too — a human reads that — but the
payload is what a reviewer is shown. Prose alone is a misfiling, and it looks
identical to never having run the suite at all.

## A green run proves nothing until you know what ran

Three separate times in one day, a suite reported success while the thing
under test never executed. Each time the exit code was 0 and the report said
"passed".

**The concrete trap, measured:** `xcodebuild test -only-testing` with a
METHOD-level path into a Swift Testing suite matches zero cases. XCTest prints
`Executed 0 tests` and exits 0. A run that tested nothing is byte-for-byte
indistinguishable, in exit status, from a run that tested everything and
passed. Select at SUITE level, or verify the count.

**The general shape** is wider than that one tool. A test that grades a
copy of the production mechanism passes when production is deleted. A test
asserting on sizing state passes while the view is invisible on a device. A
selector naming a suite that was renamed matches nothing. In every case the
signal is the same and it is worthless.

So: **report the case count per suite, never a total, and never an exit code
alone.** Thirty-five cases with two absent looks exactly like thirty-five with
two passing. A total is the one number that cannot reveal the failure.

And when a test claims a property that matters, **demonstrate its teeth**:
break the production code the test is supposed to guard, confirm the test goes
red, restore it, and report both. A test that cannot fail is worse than no
test, because it retires the doubt that would otherwise have found the bug.

## Verify the artefact, not the process that made it

Four defects in one day survived thorough verification because every check
looked at the machinery and none looked at what the machinery produced.

- A list section was "sized correctly" by a passing test while the rows were
  invisible on a real screen.
- A serialisation test measured a duplicate of the production gate; deleting
  the real one left it green.
- A test selector named cases that did not exist, so the suite passed having
  run nothing.
- A share path cleared two independent review rounds — the provenance gate,
  the sender identity, temp-file cleanup — and nobody opened the PDF. It was
  cutting clauses in half at the page break.

The last one is the clearest: every check was about how the document was
made, and the document itself was malformed. The reviews were not lazy; they
examined the wrong object.

**So make the deliverable the assertion.** Extract the text from the rendered
PDF and confirm concatenating the pages reproduces the source with nothing
lost or duplicated. Capture the screen and look at it. Read the file the user
will receive. A page COUNT says nothing about where the cut fell, and a status
flag says nothing about what is drawn.

The rule generalises: when a change produces something a person will hold —
a document, a screen, a message, an exported file — at least one check must
inspect that thing, in the form they will receive it.
