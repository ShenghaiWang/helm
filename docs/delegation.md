# Delegation: coordinator, foreman, worker

How work moves from a commander's sentence to a worker's worktree, and who
may do what along the way. Read this before driving a project or changing
`helm route`, `helm foreman`, `helm gate` or `helm task create`.

## The coordinator never does the work

The agent started in the Helm root is the **coordinator**. It does not edit,
build, test, or commit project files, and it does not do the research,
writing, or production a request asks for. Every substantive request is done
by a worker agent spawned for that task — one worker per task, in that
project's one Helm-owned Herdr space, reusing the workspace and worker tabs
recorded in Helm state and creating only what Herdr no longer has. The
coordinator then drives that worker and relays its protocol messages.

"It is only one file" is not an exception; neither is a failed or blocked
worker, which is replaced by a new worker for a new task, not finished by
the coordinator.

Herdr is detected only in a managed session (`HERDR_ENV=1` plus an available
`herdr` executable). When it is unavailable, delegation still applies: the
worker is spawned through Helm's core process launcher into the same
isolated task worktree, and only the presentation surface is lost. To spawn
a worker the coordinator discovers runtime and tool capabilities from the
active environment rather than assuming a provider-specific command; if no
runtime exists at all it asks for one and explains the limitation instead of
doing the work itself. See [herdr.md](herdr.md) and
[agent-adapters.md](agent-adapters.md).

What stays with the coordinator: choosing the project, resolving the domain
and composing bounded context, creating the task and worktree, spawning and
driving the worker (or appointing the foreman that drives it), relaying its
messages to the commander, holding the approval gate, and raising learning
proposals. Read-only inspection needed to do those is expected.

## `route` hands one input to one project's foreman, and returns

```sh
helm route PROJECT "the request, in the commander's own words" [--agent ...] [--model ...] [--no-herdr]
```

This is root Helm's whole job for one piece of commander input: identify the
project, make sure its one foreman is live, hand the request off, and come
straight back. It never waits on what the foreman does with the request — a
missing foreman is appointed without waiting, an existing one is handed the
text with a fire-and-forget pane send, and either way the call returns
without sitting on that foreman's own work. A busy project's foreman can
never delay `route` for another project.

For an *existing* foreman the request is recorded on its task first, always,
and only then does `route` check whether anything is there to send it to.
The order matters. Checking reachability first would let a dead pane's
reconciliation settle the worker to `failed` before the request was written,
silently dropping it while still claiming "recorded". Recording first is safe
because an outbound push never refreshes the worker's own liveness signal:
`last_reported_at` reflects only what the worker itself reported.

What `route` reports depends on what it found:

- **A foreman appointed by this call.** Its agent process was just spawned and
  has no pane ready to receive text, so nothing is sent into one. The output
  says `recorded` and `starting`, never `delivered`; the foreman picks the
  request up when it reads its own status (`foreman_brief`,
  `helm project status`).
- **An existing foreman with a live, provider-confirmed pane.** The text is
  sent into that session, and the output says `[delivered]` only if the send
  itself succeeded (`recorded only; the send itself failed` otherwise). A
  foreman driving its project hard and one sitting correctly idle both read
  as reachable.
- **An existing foreman with no reachable session** — a plain-process foreman
  with no input channel, or a pane the provider says is gone. `route` refuses
  to send into a pane nobody is reading and reports `recorded only`, with the
  request already durably on record. Reconciling a dead pane may settle that
  foreman's worker record to `failed`, in which case the output also says how
  to appoint a replacement (`helm foreman PROJECT`); `route` never replaces
  one automatically.

Reporting from the foreman back to root is push-based after this one
hand-off, like any worker's: `status`, `result`, `blocker` and
`approval-needed` through the worker protocol, landing in the project's
status record. Root reads that record; nothing asks a live pane to wait or
poll. See [worker-protocol.md](worker-protocol.md).

## A foreman drives one project's loops

```sh
helm foreman PROJECT   # one project, one foreman; a second is refused
```

`helm watch` tells you a worker is stalled; somebody still has to answer it,
and that somebody does not have to be the coordinator. A foreman is an agent
started with the project's status record as its brief, and it owns the loops
inside that project: turning a goal into a delegated task, launching the
worker, answering its questions, running `helm review` so an independent
agent cross-checks the change, and reporting the outcome upward.

**Every project gets one automatically.** Any command that starts work —
`helm run`, `helm worker launch`, `helm herdr launch` — appoints the project's
foreman first if it has none. A project that does not want one says so in its
own file, and that is the whole of what a project may say on the subject:

```json
{ "foreman": false }
```

Two documents reach a foreman, and the split is deliberate. `FOREMAN_RULES` in
`helm/core.py` is the **boundary**: what a foreman is and what it may never
do. That has to be code, because a domain file cannot be allowed to define
authority. The `driving-delegated-work` domain is the **craft**: how to brief
a worker, when to answer instead of escalating, what a review is worth. It
extends `code-review` and `spec-driven-development`, so the coder/reviewer
independence rules and the rubric for [when a change needs a spec
first](knowledge.md#deciding-when-a-change-needs-a-spec-first) arrive
composed rather than restated.

A foreman's authority is narrower than the coordinator's, and narrower in
code rather than in prose. Every agent Helm starts inherits `HELM_WORKER_ID`,
so `helm` knows who is calling it: approving, merging, pushing, publishing,
deleting, and granting a standing approval are refused for any agent, and
spawning is refused for anything that is not a foreman — delegation is one
level deep. A foreman escalates to the coordinator exactly where the
coordinator escalates to the commander.

A foreman produces no branch, so it is never offered as work to merge and
gets no board card. `helm watch` lists foremen first and calls a broken one
urgent: a stalled worker costs one task, while a stalled foreman costs
everything the project was going to do next.

### One driver per task

A foreman runs the review loop because its brief says to; a coordinator that
also drives the same task directly runs it too, and both are correct alone.
Started seconds apart they put two reviewers on one worktree and let
whichever finishes first set the verdict. So decide who is driving a task
and stand the other down — `helm worker stop <foreman-id>` when the
coordinator takes it, or leave it to the foreman and ask for status. Helm
refuses to start a second reviewer for a task that already has a live one,
and stops a reviewer that has already failed or blocked before launching its
replacement. The refusal is a backstop; the boundary is knowing which of you
is driving.

## The requirement and solution gates

Before a foreman may launch a state-changing worker, it clears two
commander-decided gates on its own driving task, in order:

```sh
helm gate propose <foreman-task> --type requirement --text "goal, scope, exclusions, acceptance evidence"
helm gate decide  <foreman-task> --type requirement --confirm   # or --skip; root only
helm gate propose <foreman-task> --type solution    --text "approach, boundaries, verification, risks"
helm gate decide  <foreman-task> --type solution    --confirm   # or --skip; root only
```

The foreman proposes; only the root (the commander, or a capability the root
configured) may confirm or skip — the same authority boundary that guards
`approval release`. `helm task create` refuses a `worker`-role task for that
project until both gates on its foreman are decided, unless the task is
`--read-only` (pure investigation that changes nothing). A material change to
the requirement invalidates both gates; a material change to the solution
invalidates only the solution gate — propose it again rather than reusing a
stale decision.

A confirmed pair authorizes exactly one *new* state-changing task, not an
open stream of them. The pair is spent on the task that consumes it, and
`helm task inspect <foreman-task>` shows which one under
`gates.bound_task_id`. A second, separate `helm task create` for the same
project is refused with "already authorized task …" until the foreman
proposes a gate again and the root reconfirms it — even if the first task's
worker never launched, because spending happens at task creation, so the
binding is always on the record. Proposing a new pair overwrites a confirmed
one that no task has bound yet, and a confirmed gate is bound to one foreman
task row, so replacing the foreman discards the decision.

The gate does not apply to `helm task continue` on the same task: a
continuation round is the same task asking for another pass, so it rides on
the binding it already holds and does not re-prompt the commander. Every
`continue` call must still say which kind of round it opens — `--read-only`
or `--state-changing` — with no default and no inheritance from the round
before. Leaving it unstated would let a finished read-only investigation
silently continue as state-changing work.

## What a worker may start from

Workers start in their assigned `helm/<project>/<ticket>-<task>` worktree, cut
from a fresh, verified base ([worktrees.md](worktrees.md)); one ticket keeps
one worktree across rounds. Pass the tracker id — `--ticket TICKET-192` — so
it lands in the branch name, where a human looks for it.
