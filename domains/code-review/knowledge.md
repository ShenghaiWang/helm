---
id: code-review
applies_to: Reviewing a code change written by someone else.
use_when:
  - deciding who is allowed to review a change
  - running the author-reviewer loop
not_for:
  - writing the change itself
  - reviewing prose, research, or media
selectable: false
---
# Code review domain

Small by design. Compose it with `{"extends": ["code-review"]}` from any domain
whose work produces code.

## The reviewer is a different agent from the author

An agent reviewing its own output re-runs the reasoning that produced the bug
and reaches the same conclusion. It confirms the assumption rather than
re-examining it. So the reviewer must not be the author, measured by what
actually differs:

1. **A different agent runtime** — different model, tooling, defaults, habits.
   The strong form. Prefer it whenever two runtimes are available.
2. **A different model on the same runtime** — the fallback when only one
   runtime exists. It still breaks the shared prior that makes self-review
   worthless. Record which model reviewed.
3. **A fresh session of the same model** — the weakest form, not sufficient
   alone. It clears the conversation and keeps every prior. Label such a review
   as unindependent rather than implying a check that did not happen.

## The loop runs until both are satisfied, and is bounded

Author fixes and replies; reviewer re-reads the new diff; repeat. Two rounds of
direct disagreement, then the coordinator decides. Agreement reached because
one side gave up is not agreement: a reviewer that still objects records the
objection instead of withdrawing it.

Keep both sessions warm inside that one task's loop. The author session is the
task worker; reviewer findings go back to that same author. If the reviewer has
only completed a round and its pane is still live, send the next round back to
that same reviewer session too, so it keeps the review context it already built.
Do not carry either session into another task.

## What the reviewer reads

The diff and the claim the change makes about itself — not the author's
reasoning. Reading the justification first is how a reviewer inherits the
author's blind spot.

A reviewer that finds nothing says so explicitly and says what it checked.
"Looks good" is not a review; it is indistinguishable from a review that never
ran.

When the change was specced first, the spec is the contract the diff is read
against — see the composed `spec-driven-development` domain. Read it before
the diff and check both directions: does the change do what the spec says,
and does the spec still describe what the change does. A spec that is wrong
is a finding like any other, reported rather than quietly worked around.

## What the reviewer checks

Correctness first. Then whether the tests were kept in sync with the change,
whether the diff stays inside what it claims to do, and whether anything is
silently broken by it — the failure a change causes somewhere it did not
touch is the one the author is least able to see.

Check the comments and names against the code they describe, not against each
other. A comment that overstates what the code does is a defect with a long
half-life: the next reader trusts it instead of the code, and nothing fails
until someone acts on it.

A finding is worth reporting only if it is specific enough to act on: what is
wrong, where, and what would go wrong because of it.
## Approved learning: document-only review
- Fact: When the change under review produces no compilable artifact — a design document, a plan, a README — do not require a build, and do not record 'nothing was compiled' as a caveat on the verdict. There was nothing to compile, so the caveat implies a gap that does not exist. Verify such a change by reading every claim it makes about the code against the code as it stands, and state the base commit those claims were checked against.
- Rationale: For document-only changes, the relevant verification is whether the document's claims match the repository. A build caveat adds noise when no buildable artifact exists.
