---
id: pull-request-lifecycle
applies_to: Driving a change from the moment its pull request opens to the moment it merges.
use_when:
  - a task delivers through a pull request rather than a local branch
  - review comments or failing checks have arrived on an open change
selectable: false
---
# Pull request lifecycle

Getting an opened change to merged.
Small by design: compose it with `{"extends": ["pull-request-lifecycle"]}`.

## Opening it

**An independent review returns before anything reaches the remote.** Launched
is not returned. The tempting order is fix, push, then review: the push makes
the work visible, and a reply to a review comment is only honest once the fix is
actually pushed. Both of those are true, and neither survives the consequence —
a review that lands after the push finds its defects in code reviewers are
already reading, and the branch becomes a record of churn rather than a change.
On one project this order put four P1 defects, two of them consent failures in
clinical audio capture, onto a branch that had already been pushed and replied
to. The single exception is a repair that changes no behaviour, such as a
dead-code error breaking a platform build; take it explicitly, and say in the
request that you are taking it and why, so it is a stated choice rather than an
assumed one.

**Open it as a regular, non-draft change.** Automated review bots typically only
trigger on non-draft changes, so a draft skips the automated pass entirely. "Not
ready for a human yet" is expressed by **withholding the ready-for-review
label**, not by draft status — two separate signals that must not be conflated.
The label goes on only after the automated pass is addressed, and it, not a
comment, is the signal to humans.

Each change description carries: a link to the parent ticket, which piece it
covers and its stack position with adjacent links, the relevant slice of the
traceability list, a summary of what was verified and how, a link to the
implementation notes, and the verification evidence — screenshots inline,
recordings linked. Where a dependent repository also changed, open its change
too and cross-link the two.

**Implementation notes are not an artifact to file away.** They capture the
decisions made while implementing and must be linked from each change
description, so reviewers see the reasoning and not only the diff.

How large the change should be in the first place is a separate topic: compose
`{"extends": ["change-sizing"]}` for that.

## Opening is not finishing

**A task that delivers through a pull request stays live until that request
merges.** Opening it is the middle of the task, not the end. Reporting the work
as done at the moment the request opens hands back something nobody has agreed
to take, and the comments that arrive afterwards then land on no one.

**Done means approved and merged.** For coding work, the default end-to-end path
is: finish the author/reviewer coding loop, open the PR, monitor it, address
review comments and failing checks on the same branch, push the fixes under the
project's push authority, reply to and resolve handled threads, repeat until no
actionable comments remain and the PR is approved, then keep monitoring until
the PR is actually merged. `pr-open` is active work. The task is eventually
done only when Helm records `pr-merged`, or when the commander explicitly
chooses a different handoff state.

**Watch it rather than waiting to be told.** Checks and comments arrive minutes
to hours later, from humans and from automated reviewers, and neither announces
itself. A request left sitting with unanswered comments is indistinguishable
from an abandoned one, which is the same failure as a worker that reports
nothing.

## Answering what arrives

Sort each comment before acting on it, because three different things arrive
wearing the same clothes:

- **A real defect.** Fix it, push to the same branch, and reply saying what
  changed. A reply is not an answer unless it points at a fix or gives a
  concrete reason the comment does not apply.
- **An artifact of how the work was sliced.** A helper with no caller yet
  because its consumer lands two changes further up a stack is not dead code,
  it is the stack. Say which later change resolves it and leave it alone.
- **A question that needs a human.** A reviewer asking whether a behaviour
  change is acceptable is not asking for a patch. Escalate it with the
  trade-off stated; do not answer it with a code change that buries the
  decision.

**Escalate rather than absorb** anything that changes scope, and anything a
reviewer explicitly routes to a human. Severity labels are the reviewer's
opinion, not a verdict: a critical note on an approved change usually means
"someone should decide this", and quietly patching it is how a decision gets
made by nobody.

**Find the comments before answering them.** Inline comments live on threads
attached to lines, and the change's summary view does not show them: a change
can read approved, all checks green, and still carry a dozen unanswered
threads. Query the threads themselves, on every change in a stack — a re-review
fires on each push, so a rebase or a force-push generates a fresh set on changes
nobody has looked at since.

**Answering is a reply *and* a resolution.** A reply alone leaves the thread
open, so the next reader cannot tell what was handled from what was missed, and
the count never falls. Resolve each thread as it is answered.

**Leave a thread open only on purpose, and say why in it.** A question routed
to a human is not resolved by explaining it; resolving it removes the marker
that a person still has to act. An open thread should mean "waiting on a named
decision", never "nobody got to this".

**A fallback is not automatically the safe value.** Where a change replaces a
hard failure with a default, check the direction of each default rather than the
uniformity of the rule: a permissive default converts a crash into a silently
weakened control, and the two look identical in the diff. Safety-critical
switches deserve the safe value or the original failure, and the difference is a
decision to raise, not a rule to apply.

## Keeping it mergeable

**The base moves under an open change.** Check whether it still merges rather
than waiting to be told; a conflict is not announced and does not resolve
itself, and the longer it sits the more of the base it has to absorb.

**In a stack, only the bottom sits on the base branch.** Every change above it
is measured against its sibling, so they keep reporting mergeable while the
bottom is already conflicting — that green is about the sibling, not the base,
and reading it as safety is how a whole stack goes stale. Rebase bottom-up, in
order, and re-check the top.

**Resolve by keeping both sides whenever the base added something in the same
area.** A migration whose conflict resolution quietly drops what landed while it
was open is how other people's work disappears, and the diff will look clean.
Then apply the change's own transformation to whatever the base added, or the
change is no longer true of the file it edits.

**When the base has added new instances of the very thing the change removes,
say so where the change is being reviewed.** It is not an inconvenience, it is
the evidence that the cleanup needs an enforcing check rather than a one-time
sweep — and it is the strongest argument that check will ever have.

**Rebasing rewrites history**, so it needs a force push with lease, and that is
still a push: it belongs to whoever holds the authorization, not to whoever did
the rebase.

## What stays with the human

**Never merge on your own authority.** Merging is the human's decision and no
volume of green checks, approvals, or resolved comments converts into it. That
does not make PR opening the finish line: keep monitoring and addressing the PR
until it is approved and merged by the authorized actor, then record the merge
as the final delivery state.

**Pushing belongs to whoever holds the authorization**, which may not be the
agent that wrote the fix. When it is not, the fix is committed to the branch and
handed over for pushing, rather than attempted and reported as delivered.
