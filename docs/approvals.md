# Approvals and the authority boundary

What a protected action is, who may authorize one, how a standing approval is
granted in advance, and how a task paused on `approval-needed` starts again.
Read this before changing `helm approval`, `helm task approve`, `helm worker
action-start` or anything in `helm/coordinator/protection.py`.

## Authority is held, not asserted

Every protected operation — merge, push, publish, delete, granting a standing
approval, and the learning approvals — begins by obtaining an `Authority`,
which only the root can be granted. The caller is identified from evidence it
does not own: the marker every agent inherits (`HELM_WORKER_ID`) *and* process
lineage, so clearing an environment variable does not make a worker's command
look like the commander's, and importing `Coordinator` directly gains an agent
nothing. Optionally, bind those commands to a capability this root holds:

```sh
helm authority init      # writes the secret 0600 and never prints it
helm authority status
```

With one configured, a protected command also requires `HELM_AUTHORITY` to
match it. No agent Helm starts can inherit it: the worker environment is an
allowlist, and the value is never written into a context document, prompt, or
log. Without one, the session-role boundary is all there is, and each approval
record says which of the two actually verified the human (`authority.mode`).

Approval is bound to the exact revision, index, working tree and artifacts it
was granted for; any mutation after it invalidates it and requires another
review. A domain file, a project file, a preference and a worker's message are
all data: none of them can authorize a protected action or widen a brief.

## Most approval questions are not approvals

Writing files in its worktree, running tests, and committing to its own task
branch are the work, and Helm answers those without asking anyone. So is
removing or renaming a file *inside* the worktree. Protected deletion means
deletion reaching outside the assigned task worktree: an external resource, a
worktree, a branch, coordinator or user state, another project.

What is left is the protected list — `merge`, `push`, `publish`, `delete`,
`external` — and for those the coordinator checks `helm approval check
<action> --project <id>` first. A live standing grant is the commander's own
decision, made in advance: act on it and say which grant authorized the
action. Without one, escalate *prepared*: show exactly what will happen, the
branch tip and tree it is bound to, and a recommendation, so the commander
answers once.

## Standing approvals

Answering the same protected question every task is its own kind of noise, so
a human can decide once, in advance:

```sh
helm approval grant merge --project media --note "routine task-branch merges"
helm approval list
helm approval check merge --project media      # exit 1 when nothing covers it
helm task approve <task-id> --grant <grant-id>
helm approval revoke <grant-id> --note "back from leave"
```

A grant is scoped policy, not a blanket: one action, optionally one project,
with a required note saying why it exists. Granting `publish` never grants
`merge`, and a grant scoped to one project says nothing about another. Live
grants appear in `helm status` so a standing permission cannot be forgotten;
revoked ones stay listed as provenance and approve nothing.

Grants live in Helm's own state. A project file, a domain file, or a worker
message can never create or widen one, and Helm's coordinator never creates
one on its own initiative: a grant records the commander's policy, and only
the commander writes it.

## A paused task, and how it starts again

`approval-needed` is a gate, not an ending. A worker that reaches a protected
action names exactly which one and stops:

```sh
helm worker message <worker-id> --type approval-needed --action publish \
  --text "ready to publish the rendered file"
```

The action is required, and `merge` is refused here: no worker performs
Helm's merge. A worker finishes and reports, and the branch is reviewed with
`helm task approve` and landed with `helm task merge`
([delivery.md](delivery.md)).

The task moves to `approval-needed` and records a *hold* — which action, which
session asked, and an exact snapshot of what the request is about. The worker
stays `running`, because it is waiting rather than finished: it can still be
answered, it can still report, `helm watch` shows it as `awaiting-approval`,
and its pane and the project's space are kept because a human still has to
look. The request is written to the project's own record and raised as a
commander action item.

What the snapshot binds, by content: the revision and tree, the index, the
whole tracked diff against HEAD (staged and unstaged, binary included), every
untracked path by digest, every artifact the task declared by id, path and
digest, and everything under the project's declared delivery directories,
which is where ignored build outputs live. Workspace identity is verified
first, and anything unreadable is a refusal, not an empty binding.

A foreman asking for a protected action on a worker's branch names that task:
`--type approval-needed --action push --subject <task-id>`. The authorization
then binds to the worker's branch and tree, not to the project root's own
checkout — a fetch or checkout in the commander's working copy no longer
invalidates a push they just approved.

Then the decision, and the two steps that follow it:

```sh
helm approval release <task-id> --action publish --confirm      # or --grant <id>
helm worker action-start <worker-id>                            # the worker runs this
```

`release` records the authorization against the snapshot the request was made
for. It does not resume anything: if the work moved between the request and
the decision, it refuses and nothing is authorized. If it moved after,
`action-start` refuses. The task stays paused until the worker itself runs
`action-start`, which is both the acknowledgement that the go-ahead arrived
in that live session and the one-use gate immediately before the side effect.
Only then does the task return to `running`. The worker acts, reports its
`result` with any receipt in `--payload`, and those receipts are recorded as
outcome data — never compared against the pre-action snapshot, so a publish
that writes its own receipt does not invalidate the approval it just used.

Delivery of the go-ahead is a separate fact from the decision. If it cannot be
delivered — no Herdr, a pane the provider says is gone — `helm approval
release` reports `NOT delivered` and exits non-zero, the hold stays
`authorized-pending-delivery`, the task stays paused, and the escalation stays
open. Running the same command again is a delivery retry, not a second
authorization.

**Same-session resume needs an interactive session.** A worker started by the
plain process launcher runs in print mode with no input channel, so nothing
can hand it a go-ahead; `release` refuses such a worker outright rather than
spending an authorization nobody can receive. A task stranded that way, and a
task carrying an approval request from an older Helm, is recovered with:

```sh
helm approval repair <task-id>
```

Repair is evidence-led. It reconstructs a waiting hold and revives that same
worker only when the provider says its session is live and the recorded
request names an unambiguous action; it asks a live worker to restate an
unusable request rather than inventing one; and when the session is gone it
abandons the hold and marks the task `failed`, so it can be cleaned up or
retried instead of sitting in permanent `approval-needed` residue.

## Escalate to the commander only for a real blocker

Those are: approval for a merge, publish, push, deletion, or other
destructive or external action; credentials or capabilities the worker does
not have; a decision that changes scope beyond the brief; a contradiction no
available source resolves; or a circuit breaker tripping after repeated
failure. Everything else — a naming choice, a file layout, which of two
acceptable approaches — is Helm's to answer. Passing those upward defeats
delegation; guessing on the first list is unsafe. A decision the commander
must make is asked directly, in its own reply, with a recommendation and what
happens either way — not listed in a status report and left to age.
