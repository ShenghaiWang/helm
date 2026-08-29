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

1. **A different model.** This is the load-bearing half: the shared prior
   that makes self-review worthless lives in the model, not the harness. A
   reviewer on the author's runtime with an explicitly different model is a
   full independent review, not a fallback. Record which model reviewed.
2. **A different runtime as well** — adds different tooling, defaults, and
   habits on top. Worth taking when it is convenient (a gateway runtime
   reaches other vendors' models easily), but it is an addition, not the
   requirement — do not route through a slower runtime solely to change the
   harness when a different model is available on the one already running.
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

## Full-suite evidence belongs to the author, once

The author runs the full unit suite once it believes the change is done, at
the exact tip it is asking to be reviewed -- not an earlier commit, not a
stale run from before the last fix -- and reports the result through the
ordinary worker protocol with a `full_suite` field in the message payload
(`helm worker message <id> --type result --text "..." --payload
'{"full_suite": "<command>: <exact unmasked counts and exit status>"}'`).
"Unmasked" means the real exit status is visible in what is reported, not
piped through something that could swallow it (`... | tail`, a wrapper that
always exits 0) and not simply asserted with no counts behind it. Helm quotes
the latest such report into the reviewer's own brief automatically -- see
`HerdrAdapter._full_suite_evidence` -- so the reviewer never has to go
looking for it or take the author's earlier prose at its word.

The reviewer's job is to inspect code and that evidence, not to reproduce it:
rerunning the full suite is duplicated work, not independent verification,
since a green rerun proves nothing the author's own report did not and a red
one is not something a reviewer is positioned to triage. A reviewer may still
run the type checker, the linter, and a small number of focused,
risk-targeted tests aimed at the lines it is unsure of.

If the author's full-suite evidence is absent, stale (predates the diff
under review), masked (no visible exit status, or piped through something
that could swallow one), or reports a failure, the reviewer does not run the
suite to settle it. That gap is itself a finding: report it and hand the
change back to the author instead of resolving it by rerunning what should
already have been run.

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

## Report suspicions too — labelled as suspicions

A verdict that carries only verified findings is honest but narrow: the
reviewer noticed three other smells and spent its budget proving the two it
reported, so the smells surface one per round. End every verdict with a
clearly separated **Unverified suspicions** section — one line each, file
and reason, no obligation of proof. The author checks them in the same fix
round for the cost of a glance; a suspicion confirmed is a round saved, and
one dismissed costs a sentence in the result ("checked, not real,
because..."). The two lists must never blur: a suspicion presented as a
finding wastes a fix; a finding demoted to a suspicion ships a bug.
