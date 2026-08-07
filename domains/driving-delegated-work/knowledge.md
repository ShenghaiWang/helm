---
id: driving-delegated-work
applies_to: Driving work delegated to other agents instead of doing it yourself.
use_when:
  - an agent is responsible for an outcome it will not produce itself
  - work must be written by one agent and checked by another
not_for:
  - doing the work
  - reviewing a change (compose `code-review` for that)
selectable: false
---
# Driving delegated work

How an agent responsible for an outcome gets there without producing it
itself. Small by design: compose it with
`{"extends": ["driving-delegated-work"]}`.

It says nothing about what a driver is *allowed* to do. Authority is the
coordinator's and lives in its own rules; this is procedure only.

## Delegating is the work, and it is not free

A driver that starts editing has quietly become a worker nobody is driving,
and the review it was supposed to arrange never happens. "It is only one
file" is where this fails every time: the small task is the one that looks
cheaper to do than to hand over.

The exchange is worth it because it buys two things doing it yourself cannot:
a second agent that did not write the change gets to judge it, and the driver
keeps enough attention free to notice a worker that has stopped.

## One task, one worker, one worktree

Give the worker what done looks like, not how to get there. A brief that
specifies the implementation gets the implementation you described, including
the parts you were wrong about — and leaves the worker with nothing to check
its own approach against.

Size the task so one agent can finish it. Two agents on one task is not
parallelism; it is a merge conflict with extra steps.

Decide at this moment, before the coder starts, whether the behavior has to
be agreed in writing first — the rubric and what such a document covers are
in the composed `spec-driven-development` domain. It is the driver's call and
a routine one: no human approves it and no task waits on it.

Then write the decision into the brief itself, not only into the project
record. The record is not in the worker's context, so a verdict kept there
reaches your own history and never reaches the coder. The brief carries the
verdict, the one-line reason, and — when a spec is called for — which
convention the repository already uses and where it lives, or that it has
none and finding a compliant location is part of the task. "No spec,
mechanical rename, no behavior change" is a complete entry; silence is not.
Record it in progress reporting as well, for whoever drives this next.

When the document exists, name its path as the task goes to review, so the
reviewer reads the contract rather than inferring one.

## Drive it, because a worker that stopped will not tell you

A delegated worker fails in ways that look identical to progress from
outside: it asks a question nobody answers, it hits an error and sits in it,
it finishes and keeps its session open. So check liveness on a schedule
rather than waiting to be told, and treat an unanswered question as *stopped*
— it is, and it is stopped on the driver.

Answer from the goal and the project's own material. A driver that forwards
routine confirmations upward has added a queue, not a decision-maker. What
genuinely goes up is narrow: a protected action, missing credentials, a
decision that changes scope, a contradiction no source resolves, or repeated
failure.

## Review only work that needs an independent judgment

Do not burn a reviewer on a purely operational task whose judgment gate has
already been cleared. Uploading or scheduling an already-produced video is the
canonical case: if the human has approved the content gate and explicitly
approved the protected publish action, the worker's job is to execute the
checklist, verify the scheduled/live state, update the tracker, and report the
URL. A second agent does not add a useful judgment there unless the worker had
to change the artifact, metadata, eligibility, policy interpretation, or factual
claims.

Use review for work that produces or changes something whose correctness needs
independent scrutiny: code, generated media, research conclusions, factual
analysis, candidate eligibility, metadata rewrites, or any fix after a finding.
For straight-through operations, verify the operation instead of reviewing it.

## The produced result goes to an independent reviewer before it goes anywhere else

The author is the worst available reviewer of its own change — see
`code-review` for who qualifies and why. The driver's job is to *arrange*
that review, not to perform it:

- hand over the diff and let the reviewer read it before any of the author's
  reasoning, so it does not inherit the author's blind spot;
- let the two exchange rounds directly until they agree or a bounded limit is
  reached, keeping the same author session and the same live reviewer session
  for that one task's loop;
- before replacing a failed or blocked reviewer, stop its stale session if it
  still has a live pane/process. A dead review is evidence, not a second
  reviewer to keep running beside the replacement;
- never talk a reviewer out of a finding. An objection that survives is the
  loop working.

Agreement reached because one side gave up is not agreement. If the objection
still stands when the rounds run out, that is an unresolved review, and it
goes upward as a blocker rather than downward as a result.

## Report the outcome, not the activity

What the next reader needs: what was built, who checked it, what they
checked, and what they found. Not a transcript. A driver that reports only
when everything is finished is indistinguishable from one that died, so push
progress as it happens and write each decision to the durable record rather
than holding it in the conversation.

For meaningful intermediate outcomes, use an explicit summary status rather
than an ordinary heartbeat. A worker can wake the foreman with
`helm worker message <id> --type status --payload '{"summary":true}' --text
"round 3 implemented; waiting on reviewer"`. A foreman uses the same summary
payload for commander-facing progress lines such as "task round 5: reviewer
approved, waiting on merge decision"; Helm records those in project
status. Worker summaries are recorded in project status too and wake the
foreman; foreman summaries are the curated line for the commander. Do not
summarize every command or test line -- summarize a changed state of play.

Treat a worker result as a milestone, not final delivery. The driver still has
to move or monitor the outcome on the project's delivery surface: local work
finishes at `merged` after the task branch lands in the project worktree, while
PR work finishes at `pr-merged` after the open PR has been monitored through
comments, checks, and merge. `pr-open` is active work for the project's single
foreman to watch. Once final delivery is recorded, release the worker sessions;
the durable outcome is the branch, PR record, artifacts, project status,
messages, and logs, not a stale agent tab.
