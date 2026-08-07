---
id: spec-driven-development
applies_to: Deciding whether a change needs a written spec before it is coded, and working against one when it does.
use_when:
  - a driver is about to brief a coder and what "correct" means is not obvious
  - a change alters behavior or a contract that another component depends on
  - review keeps returning to the same tradeoff, or the work is already on its third round
not_for:
  - narrow, well-understood, low-risk mechanical changes
  - choosing, installing, or scaffolding a spec framework
selectable: false
---
# Spec-driven development

When to agree on the behavior in writing before anyone codes it, and what to
do with that agreement afterwards. Small by design: compose it with
`{"extends": ["spec-driven-development"]}`.

This is procedure, not authority. A spec is a description of intended
behavior. It does not approve anything, it cannot authorize a protected
action, and it never widens the brief it came from.

## The decision belongs to the driver, and it is made before coding starts

A spec written after the code exists is a description, not an agreement --
it inherits every assumption the author already made, which is exactly the
thing it was supposed to catch. So the driver decides at brief time, before
the coder starts, and records the decision and its reason in the task's
progress reporting. Deciding "no spec" is a decision too, and it is worth one
line: the next reader should not have to guess whether it was considered.

The decision is a coordination call. It is not an approval gate, it does not
wait on a human, and it does not put the task in a special state.

## The decision has to reach the coder, and the brief is the only thing that does

A worker's context is its brief plus composed domain and project knowledge.
The project's progress record is not in it. A decision written only to progress
reporting therefore reaches the driver's own history and nobody else, and the
coder starts having never been told -- which is the exact outcome deciding
early was supposed to prevent.

So put it in the task brief the worker is created with, in plain words:

- **the verdict** -- spec first, or no spec;
- **the reason**, one line, in the terms of the rubric: "changes the expiry
  contract other callers read", or "no behavior change, mechanical rename";
- **which convention and where**, when a spec is called for and the repository
  already has one, so the coder does not re-derive it;
- **that there is none**, when the repository has no convention, and that
  finding a compliant location is part of the task.

Record the same verdict and reason in progress reporting as well. The brief is
what the coder reads; the record is what the next driver reads. Neither
substitutes for the other, and writing one is not writing the other.

The document's path travels the same way once it exists: name it when handing
the task to review, and keep it on the task branch, so the reviewer reads the
contract inside the diff it is already reading instead of guessing that one
exists.

## The rubric

Ask for a spec first when any of these is true:

- **The behavior is ambiguous.** More than one reasonable reading of the goal
  would produce different code, and picking one silently is a coin flip
  nobody sees until review.
- **It changes a contract across components.** An API shape, a schema, an
  event, a file format, a flag another caller reads. The cost of getting it
  wrong is paid by code that is not in this diff.
- **Auth, permissions, or security boundaries.** Who may do what is the class
  of question where a plausible implementation and a correct one look
  identical from the diff.
- **Data loss is possible.** Migrations, deletions, destructive backfills,
  anything that rewrites existing records. There is no second attempt.
- **Billing, payments, or publishing.** Work whose mistakes are visible
  outside the system and cannot be quietly reverted.
- **It is a user-facing workflow.** Multi-step flows, states, and the empty,
  error, and partial cases -- the parts a brief almost never enumerates and a
  reviewer cannot infer.
- **Review keeps relitigating the same tradeoff.** Two reviewers reaching
  opposite conclusions about the same choice is a missing agreement, not a
  code problem.
- **The work already needs multiple rounds.** By round three the disagreement
  is about what the change should do, and another round of diffs will not
  settle it.

Skip it when the change is narrow, well understood, and low risk: a typo, a
dependency bump, a rename, a log line, a test for behavior that already
exists, a fix whose correct outcome nobody would state differently. Spec-
gating those buys nothing and trains everyone to skim the spec that mattered.

## No behavior change outranks every trigger

The rubric asks what the change *does*, not which directory it lands in or
which words appear near it. A typo in publishing copy, a mechanical rename in
billing code, a formatting pass over an auth module, a log line added to a
migration -- none of them changes behavior, so none of them needs a spec,
however alarming the surrounding area sounds. Applying a trigger because the
area matched its name is how spec-gating becomes a tax on touching the scary
directory, and the tax is paid in attention that the real migration then does
not get.

Apply a trigger only when the change actually alters the behavior that trigger
is about.

The one place this reverses: if you cannot tell whether the change alters
behavior, treat it as though it does. "Just a rename" that moves a serialized
field name, a public symbol, a config key, or anything another component
matches on is a contract change wearing mechanical clothes -- and it is a
contract change on the rubric, so it gets the spec.

When in doubt on a change that alters behavior covered by any item above,
write the spec. When in doubt on a change that alters none of it, do not.

## Follow the repository's convention; never bring your own

Read the project's own files before proposing any format. A repository that
already has a spec convention has one for reasons that are not in view, and
a second convention beside it is worse than either alone.

Look for what is actually there: a specs, rfcs, proposals, design, or adr
directory; a docs tree with an obvious home for this; a template checked into
the repository; a contributing guide that says where behavior is described; or
a spec framework the repository has already adopted. OpenSpec, Spec Kit, and
BMAD are examples of the last kind, named here only so a driver recognizes
them in a repository that uses them. None of them is required, none is
assumed, and nothing here depends on any of them.

If the repository uses one, follow it exactly -- its layout, its file naming,
its lifecycle. If it uses none, do not install, initialize, or scaffold one as
a step in doing something else. Write a plain document in the location the
repository already keeps its documentation, and say in the brief which
location that is.

### When the repository has no documentation location at all

Some repositories have nowhere obvious to put it. Do not silently invent a
permanent convention for them -- the first agent to add a specs directory has
decided the layout for everyone who comes after, on the authority of having
gone first.

Work down instead:

1. **Infer from the repository's own norms.** A contributing guide, a
   README section, the naming style of whatever documents do exist, an
   adjacent per-feature or per-package layout. If the repository implies a
   home, use it and say what implied it.
2. **Otherwise write it task-local and temporary.** Put it in the task
   worktree at a path that is obviously scoped to this task and not a new
   convention -- next to the change, named for the task rather than for the
   category, so nobody mistakes it for an adopted layout. Report it with
   `--type artifact --path`, and say in your result that the repository has
   no documentation location and that this file is temporary.

Either way, name the path when the task goes to review. Commit it on the task
branch when it belongs in the change, so the reviewer meets it in the diff;
when it does not belong in the change, leave it in the worktree for the review,
where the reviewer reads the author's checkout anyway. What must not happen is
a spec the reviewer never learns exists.

### A temporary file has an end, and it comes before approval

An uncommitted document is work in progress, and work in progress is not
something to approve around. A task worktree is expected to be clean when the
change is put up for approval -- untracked files included -- so a temporary
spec left lying in it is not a harmless extra: it is the difference between a
reviewed tree and an approved one, and the right response is to finish the
file's life, never to loosen the check.

So run it out in order:

1. **Keep it through review.** It is the contract the reviewer reads against,
   and deleting it mid-loop removes the thing the findings refer to.
2. **Capture what it decided, durably.** Every decision it settled, every open
   question and how it closed, and every follow-up it created goes into the
   task's result and the project's progress record -- the places that outlive
   the worktree. A decision that exists only in a file about to be deleted has
   not been recorded; it has been staged for loss.
3. **Then remove it, before approval.** Leave the worktree clean.

If at that point the document is worth keeping, it was never temporary: commit
it into the repository as part of the change instead of deleting it. Deciding
that is the driver's call, and either answer is fine -- what is not fine is
leaving it untracked and calling the tree ready.

Proposing a permanent documentation convention for a repository that lacks one
is worth doing -- as its own follow-up, recorded and scoped, never as a side
effect of the change that noticed the gap.

## What a lightweight spec covers

When there is no local template to follow, a short document in the
repository's own documentation style, covering:

- **Problem** -- what is wrong or missing, and who it affects.
- **Desired behavior** -- what the change must do, described so someone who
  did not write it could tell whether it does.
- **Non-goals** -- what this change deliberately does not do, so scope does
  not drift into it later.
- **Acceptance criteria** -- checks a reviewer can run or observe, one per
  line, each true or false rather than a matter of opinion.
- **Verification** -- how the behavior is proven: which tests, which commands,
  and what an observed run should show.
- **Open questions and action items** -- what is still undecided, and who
  decides it. An open question is a reason to ask, not a reason to guess.
- **Follow-ups created** -- work deliberately deferred, and where it was
  recorded so it is not lost.

Short is the point. A spec long enough to need its own review has replaced
the problem it was solving.

## The spec lives with the change

It belongs in the task worktree, on the task branch, in the repository's own
documentation location. It is part of the change when the repository keeps
that kind of document; where the repository clearly does not, it still stays
in the worktree so the reviewer reads the same text the coder worked from.
It never goes into another project, and it is never invented from another
project's conventions.

## Same coder, independent reviewer, one contract

The coder that wrote the spec implements against it. The reviewer -- who did
not write either -- reviews the behavior against it: does the change do what
the spec says, and does the spec still describe what the change does. A
review with the spec in hand is checking a claim; a review without one is
guessing at intent, which is how the same tradeoff gets relitigated every
round.

A finding that the spec is wrong is a normal, useful finding. Fix the spec
and say so; do not let the code and the document drift apart quietly.

## Spec changes and open questions are outcomes, not bookkeeping

Both are reportable events, pushed as intermediate outcomes to the driver and
recorded in the project's progress reporting:

- the spec changed after coding started, and what changed;
- an open question is blocking, and what decision it needs;
- an acceptance criterion cannot be met as written, and why.

Resolution is stated, never inferred. Do not read a document's prose and
conclude that its open questions have been settled -- an unanswered question
that nobody restated is still unanswered. It is closed when the driver or the
author says which decision closed it.
