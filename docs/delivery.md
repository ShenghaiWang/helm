# Delivery: from a worker's result to a finished task

A worker's `result` is a milestone, not the end of the task. This is what
happens after it: the delivery decision, local merge or pull-request
delivery, build outputs, and the cleanup gate that finishes the task.

## The outcome lives in durable surfaces

The outcome is kept in the task worktree, the branch, the PR, delivered
artifacts, the project status record, messages and logs — never by keeping
an agent session alive as storage. Every terminal report is written into the
project's status record as it arrives, so a final summary flows worker →
foreman → Helm and survives the pane, the session and the conversation.

While the project's foreman is live, the result is pushed to it and nobody is
asked to merge anything. Once no driver is left — the foreman reported,
stood down, or the project declined one — Helm records a **delivery
decision** as a commander-visible action item on that project: read the
outcome, then choose review, another round, local merge, PR delivery, or
cleanup.

```sh
helm status                 # "Waiting on you": every open decision, project-labelled
helm project status <id>    # the same items marked [decision], with the task they name
helm pending                # only what waits on a human; silent when nothing does
helm watch                  # repeats DECISION REQUIRED until somebody answers it
```

Recording the outcome is not the same as delivering it. A worker reports by
running a Helm command *inside its own pane*, so the confirmation is printed
onto the surface that releasing the tab is about to remove. Helm therefore
routes the final summary and the decision before any of that runs — to the
project's live foreman, to the project's own overview pane, and to the
durable status record — and records which of those accepted it. A project
with no driver is the case that most needs telling. A finished tab whose
outcome reached nothing at all is not released, because that pane is then the
only copy.

The decision is deduplicated, so the several paths that can raise it — a
worker result, a foreman's final report, a foreman standing down — produce one
item. It names the single unresolved task when there is one and stays
project-scoped when there are several. It closes itself once the decision has
been taken: the task reaching `merged` or `pr-merged`, being continued with
`helm task continue`, or being cleaned up. A free-text follow-up recorded with
`helm project action` is never auto-closed — Helm knows when a delivery
decision was taken and cannot know whether somebody's caveat was dealt with.

The one exception is a task that has left the live document. `helm state
archive` takes a settled task's whole record with it, and an item still
pointing at that task — a delivery gate, a failure decision, or a free-text
follow-up — has nothing left to decide. `helm state tidy` closes those as
"task archived", after re-running the derived gates so a delivery decided
elsewhere or a failure since retried closes too; `helm watch` and `helm state
archive` run the same pass. A failed foreman or reviewer never raises a
failure decision at all: neither owns a worktree, so none of retry, continue
or cleanup applies — the review loop re-runs a failed round, and the next
command that starts work appoints a driver.

A free-text follow-up on live work is still never closed by Helm. What
`helm state tidy` does with one older than a fortnight is show it to the
commander, with its id and age, so it is looked at rather than forgotten;
`helm project resolve <project> <item> --note "..."` closes it on their word.
That command is root-only, like the approvals: an agent that could close a
decision would have decided it.

## A project declares its checks; the worker runs them; Helm judges the record

```json
// projects/<id>/.helm/project.json
{"checks": [
  {"name": "unit", "command": "npm test", "cases": true},
  {"name": "lint", "command": "npm run lint"},
  "npm audit --audit-level=high"
]}
```

A worker is handed the list with its task and the exact evidence command
for each. Approval of a standard or critical task is refused until every
declared check has a green record at the tip, recorded with `helm task
evidence <task> --check <name> ...`; a check with `"cases": true` also needs
a case count above zero. A small task is told to run them and is not gated.
Helm runs none of them: a project's code is the worker's to execute, and
Helm's part is the boundary and the record.

## The pull request carries its provenance

`helm task provenance <task>` prints a short block for the PR body: which
task and ticket, its shape, which agent and model wrote it at what effort,
how many independent review rounds ran and what they said, and the suite
evidence at the tip. The foreman pastes it; Helm writes nothing to the PR.
When Helm syncs a PR it reads the body, records whether the block is there,
and notes a missing one on the project's record once, naming the command.
A later reader asking who wrote a change, how it was checked, or whether a
model with a licence question touched it, answers from the PR itself.

## Local delivery

For local delivery the final state is `merged`. An approved operator uses
the explicit sequence below; each command is optional automation, not part of
the native conversation:

```sh
helm task inspect <task-id>
helm task approve <task-id> --note "reviewed"     # or --grant <grant-id>
helm task merge <task-id>                        # fast-forward only
helm task cleanup <task-id>
```

Approval records the terminal worker, branch tip, and tree hash — identically
whether a person approved in the moment or a standing grant did, with the
grant's id recorded as the authority. Any later mutation invalidates the
approval and requires another review. A task worktree must be clean to be
approved, untracked files included. Merge fast-forwards the task branch into
the project's base branch, copies declared local artifacts into the base
worktree, and can then release the worker session. Never use a raw Git merge
for this: the merge path is what delivers build outputs.

Another round on a finished task reuses its worktree and branch:

```sh
helm task continue <task-id> --brief "..." --read-only        # investigation only
helm task continue <task-id> --brief "..." --state-changing   # may edit and commit
helm task reopen <task-id>                                    # first, for a failed or blocked task
```

A `completed`, `approved` or `pr-open` task can be continued directly; a
failed or blocked one is reopened by the root first.

## Pull-request delivery

For PR delivery the final state is `pr-merged`. Helm records the branch push
as an intermediate delivery event, records the PR URL as `pr-open` when a PR
is created or supplied, and keeps that task visible until monitoring records
the PR as merged:

```sh
helm task pr <task-id> --confirm       # push and create a PR when gh is available
helm task pr-status <task-id> --state open --url https://example/pull/1
helm task pr-sync <task-id>            # read comments, checks and state with gh
helm task pr-status <task-id> --state merged --url https://example/pull/1
```

`pr-open` is deliberately not final: checks, review comments and human
replies can still arrive, so the project's single foreman stays responsible
for monitoring it. PR delivery still requires the explicit protected push
command; monitoring records observations and never approves or merges on its
own. `helm watch` and the watchdog read every open PR on their own, at most
once per task every ten minutes and quietly skipping a remote they cannot
reach, so a merged PR is recorded as `pr-merged` within minutes and its
cleanup decision raised; `helm task pr-sync` does the same read by hand.

## Delivering build outputs

A merge moves tracked files only, so a rendered video — often the actual
product — would stay in the task worktree and die with it. Delivery copies a
task's outputs into the project:

```sh
helm task deliver <task-id>            # runs automatically after a merge too
helm task deliver <task-id> --force    # replace a differing project copy
```

It copies what the worker declared with `--type artifact --path`, plus
anything under the directories a project names in `.helm/project.json`:

```jsonc
{"label": "YT", "deliver": ["renders", "clips"]}
```

That second list matters because a worker that forgets to report a render
would otherwise lose it. Copying never escapes the worktree or the project
root, an identical file is a no-op, and a differing file is reported and
skipped rather than replaced — the copy already in the project may be the
human's own cut. After a merge, verify that the expected outputs are present
in the base worktree before reporting the task final.

## Delivery is not finalization: the cleanup gate

A task that reaches `merged` or `pr-merged` still owns its task worktree, its
`helm/<project>/<task>` branch, and its worker directories, and the delivery
decision closes the moment the merge lands — so the commander would see
nothing outstanding while the disk still held everything. Helm therefore
raises a second gate, also shown as `[decision]` and repeated by `helm watch`,
naming exactly what that task still holds and the command that sheds it:

```sh
helm task cleanup <task>                   # worktree, worker directories, merged branch
helm task cleanup <task> --delete-branch   # also discard a branch with unmerged commits
helm project release <id>                  # the same, task by task, reporting what it kept
```

What it names is read from Helm's own record of what the task owns, not from
a live look at the disk: `helm status` and `helm watch` run no git or
filesystem probe for it, and a project root that has moved or gone unreadable
cannot be mistaken for "the branch is already gone". A resource is held until
Helm records letting go of it, and cleanup is the only thing that does —
including for resources removed outside Helm, which it reconciles as removed
on the way past.

Once a task holds nothing, its record can no longer change, so `helm task
cleanup` and `helm project release` move it — with its workers, messages and
artifacts — out of the live state document into
`state/archive/tasks/<task-id>.json`. Nothing is deleted: `helm inspect`,
`helm task cost` and `helm task outcome` read the archive when a task is not
live, and `helm reflect` counts archived evidence inside its window. The
live document is read and rewritten by every command, so it stays fast only
while it holds what can still change; `helm state stats` shows its size and
what could move, `helm state archive` moves everything eligible (with
`--reconcile` for records older than the cleanup that would have marked
their branch or worker directory removed), and `helm doctor` warns when the
document has grown past what it needs to carry. A project whose work is over
is forgotten the same way: `helm project remove <id>` archives what it still
holds and drops the record, once its directory has left `projects/`.

Like the delivery decision it is derived rather than flagged: raised once
however many times it is recomputed, never raised for a task that holds
nothing, and resolved as soon as the record says the residue is gone. It
resolves only for what cleanup actually shed, so a branch kept because it
holds unmerged commits leaves the item open, naming just the branch. Cleanup
stays explicit — Helm never runs it for you, because it deletes a checkout
and a branch — unless you have granted it in advance (`helm approval grant
cleanup`, see [approvals.md](approvals.md#standing-approvals)), in which
case `helm watch` and the watchdog shed delivered residue under that grant
and say so. Its refusals are unchanged either way: a dirty workspace, a live
session, work still awaiting approval, and an unmerged branch are all
preserved and reported. The work is not finalized until that approved cleanup
decision is resolved.
