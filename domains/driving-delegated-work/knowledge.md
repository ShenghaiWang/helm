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

## Two checks happen before the brief, not after

Both are the driver's job precisely because the worker never sees the gap
between "the task did not exist yet" and "the worker is now inside its
worktree" -- whatever was true at the moment of creation is what the worker
inherits, silently.

- **The base is fresh and verified.** Creating the task worktree/branch from a
  stale, unfetched, or dirty base contaminates the isolation before a single
  line is written. The composed `branch-isolation` domain carries the full
  procedure; do it before calling `helm task create` / `helm worker launch`,
  not after.
- **The right skills are in view.** Discover the selected project's own
  skill manifests (for example `.claude/skills/`, `.agents/skills/`) and pick
  only the ones whose metadata/description matches this task -- inside that
  one project, never copied into Helm or installed automatically. Prefer a
  runtime that auto-loads the project's skill location; if a different
  runtime is chosen and can read files, name the exact `SKILL.md` paths in
  the brief so it does not start blind. The composed `model-selection`
  domain carries the runtime-fit detail behind this choice. Write what was
  selected (or explicitly "none, because ...") plus the paths, the
  loading method, and the reason into the brief and the project record, so a
  replacement driver can reconstruct the decision without you. A skill is
  guidance a worker reads, not authority: it cannot expand the task's scope,
  authorize a protected action, or override core safety, and a required skill
  that is missing or unreadable by the chosen runtime is a capability
  blocker to report -- not something to improvise around.

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

## Classify review by deliverable, not by whether a file changed

The question is what the task delivers and whether that deliverable needs an
independent judgment before anyone trusts it — not whether carrying it out
happened to touch a file. A script edit inside an established operational
mechanism is not automatically software delivery, and "nothing was written"
is not automatically a safe skip either; classify by the kind of work and its
primary deliverable, then decide review from that.

**Straight-through operational work gets no independent review agent.**
Uploading, publishing, or scheduling an already-produced, already-approved
artifact is the canonical case. Its safety comes from a different chain: the
protected-action approval gate before a human authorizes the action, an
immutable preflight check that what is about to run still matches exactly
what was approved — same artifact, same parameters, nothing drifted since
approval — a fixed execution checklist the worker runs step by step, and a
post-action verification that the operation actually took effect (the
scheduled/live state, the tracker updated, the URL reported). A second agent
re-reading that sequence adds no judgment the checklist and the state check
do not already supply.

**A narrow, bounded adjustment to the operational mechanism itself can stay in
that flow.** Fixing a broken selector in an upload script, correcting a field
mapping, bumping a schedule offset — these change a script, config, or
tracker file, but when the commander has explicitly classified the request as
an operational fix and the change is narrow and bounded to the existing
mechanism, the deliverable is still the operation, not new software behavior.
Do not auto-promote it to software delivery merely because a file with code
or config in it changed — that file-touched trigger is exactly the mechanical
shortcut this section replaces. Route it through the same preflight,
checklist, and post-action verification as any other operational task.

**Everything that produces or changes something needing independent scrutiny
still gets independent review.** Reusable software behavior — a feature, a
logic change other callers depend on, a new capability; generated or changed
media; a factual or research conclusion; a policy interpretation; an
eligibility decision; or a metadata or content change material enough to
affect what a viewer sees or a decision that follows from it. None of those is
made safe by a checklist, because the risk sits in the judgment, not in the
mechanism executing it.

**A request that mixes both kinds of work is classified phase by phase.**
"Produce this and then publish it" has a production phase whose deliverable
needs independent review and a publish phase whose deliverable is the
operation and does not. Do not carry a review requirement from one phase into
the other in either direction, and do not skip review on the phase that needs
it because the request also contains an operational step.

**Scope growth or added risk reclassifies mid-task.** An operational task that
starts rewriting the artifact, materially changing its metadata, touching
eligibility, or interpreting policy has stopped being straight-through the
moment that happens, whatever the brief said at the start. Say concretely what
crossed the line — "the fix now rewrites the description shown to viewers, not
just the upload script's field mapping" — rather than reclassifying silently
or leaving it operational because that is where it began.

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
