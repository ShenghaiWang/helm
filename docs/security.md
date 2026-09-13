# What Helm defends against, and what it does not

Helm's authority boundary is real, and it is narrower than some of the
prose around it has implied. This page is the honest statement of the
model, so that every limitation below reads as a documented limitation
rather than a failed guarantee.

## The threat model

Every agent Helm starts runs **as the same operating-system user as Helm
itself**, on the same machine, with the same filesystem permissions. There
is no sandbox, no separate uid, and no privilege boundary between the
coordinator and a worker. Helm therefore defends against exactly one kind
of adversary:

> **An agent that follows the protocol but reasons badly, or is
> prompt-injected into trying something it should not.**

Against that agent the boundary holds: a worker's own `helm` commands
identify it by the marker every agent inherits and by process ancestry;
protected core operations refuse any caller that is not the root; an
approval is bound by content to the exact revision, index, working tree,
untracked files and declared artifacts it was asked for, and is spent once;
a standing grant is checked at the moment it is used; a merge re-verifies
under the lock. A model that has talked itself into "I should just merge
this" cannot, and is told why.

It does **not** defend against an agent that sets out to circumvent Helm.
Such an agent shares Helm's uid and can:

- edit `state/state.json` directly, including approval grants, a hold's
  recorded authorization, and the `config.authority_hash`;
- read `state/private/authority.secret` when a capability is configured;
- clear its environment and reparent itself to init, after which process
  ancestry no longer ties it to a worker record and its commands read as
  the root's session;
- restore write permission to a read-only worktree with one `chmod`.

Closing those needs operating-system isolation Helm does not provide: a
separate user per worker, a store the workers cannot write, or a sandbox
that keeps `state/` out of reach. Some runtimes offer part of this (Codex's
sandbox is one), but the worker protocol itself requires every worker to
write the shared store through Helm's own CLI, so the store cannot be taken
away from workers without redesigning the protocol. Until then, treat the
boundary as what it is: a strong check on a cooperating agent's judgement,
and a speed bump for a defector.

## What the checks actually are

| Check | Where | What it establishes |
| --- | --- | --- |
| Caller identity | `caller_identity` | the marker `HELM_WORKER_ID`, else a recorded worker pid in the process ancestry, else the root's session |
| Root-only operations | `Coordinator.authority()` | refuses a non-root caller; with a capability configured, also requires it in `HELM_AUTHORITY` |
| Approval snapshot | `protection.py` | content digests of revision, tree, index, diff, untracked files and artifacts; re-taken at release and at `action-start`; single use |
| Grants | `approval grant` / `check` | checked against the task's project at the moment of use; revocation is honoured immediately |
| Merge | `merge_task` | re-verifies the reviewed snapshot under the lock, fast-forward only |

The capability (`helm authority init`) is opt-in. A root that has not set
one relies on the session-role check alone, and every approval record says
which of the two modes actually verified it.

## Worker-performed protected actions run unsupervised

For an action Helm performs itself (a local merge, an archive), the gate is
the action. For an action the **worker** performs after authorization (a
push, a publish, an external delete), `helm worker action-start` is the
last thing Helm sees: it re-checks the snapshot, spends the authorization,
and returns. The worker then acts on its own. Helm records what it reports
afterwards as outcome data; it does not watch the action happen.

## Read-only worktrees are a courtesy, not a wall

A read-only task's worktree has its write bits stripped so that an agent
which respects the boundary fails fast on an accidental edit. The files are
still owned by the same user; `chmod -R u+w .` undoes it in one command.
The git index and object store were never protected at all. Read "cannot
write" in that code as "will notice it is not supposed to".

## What to do with this

- Run Helm as the only thing that user does, on a machine whose secrets
  you would not hand to the agents anyway.
- Configure a capability with `helm authority init` and keep the secret out
  of every agent's environment; it raises the bar from "clear the marker"
  to "read a 0600 file the agent could read".
- Prefer runtimes with a sandbox for state-changing work, and keep the
  read-only investigation tasks on the cheap path.
- Review the ledger and the project records, not the agents' word: the
  record is what Helm can vouch for.
