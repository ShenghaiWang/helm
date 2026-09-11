# Task worktrees and the base they are cut from

Every task gets its own worktree and branch, cut from a base that was
verified moments before. This is the procedure, the one setting that changes
it, and the rule that one ticket keeps one worktree across rounds.

## A fresh, verified base before every new task worktree

A task worktree inherits whatever the base was at the moment it was cut, so
that moment has to happen before the task exists, not be checked afterward.
`domains/branch-isolation/` carries the procedure, and Helm enforces it:

- Resolve the project's *configured* base branch — never a hardcoded or
  inferred name, and never whatever the checkout currently sits on.
- When it has an upstream, fetch it and verify the fetch **succeeded**. A
  fetch that succeeds and moves nothing is still a fresh, verified base; only
  a failed fetch blocks, and it never falls back to a cached ref.
- A branch with no upstream configured but a remote that has a same-named
  branch is not treated as local: Helm fetches that one unambiguous match
  rather than trusting an unverified local tip, and blocks if none or more
  than one remote matches.
- A local branch that is ahead of or diverged from its freshly fetched
  upstream blocks, so a task is never built on unmerged local commits mixed
  in without review; equal-or-behind uses the fetched upstream tip.
- An uncommitted change to a tracked file, or an unresolved merge, rebase or
  cherry-pick in the project's own checkout, blocks the same way. Helm never
  merges, rebases, resets or discards anything to get past it. An untracked
  file (an uncommitted `.helm/project.json`, a build artifact) does not
  block, since it changes nothing about what the base resolves to.
- Record the exact verified commit the worktree and branch are cut from.

| Composed into | Reaches | What it does there |
| --- | --- | --- |
| `branch-isolation` | every worktree-backed task | the procedure itself |
| `driving-delegated-work` | a project's foreman | before `helm task create` / `helm worker launch` |

For a genuinely local-only project (no remote at all), the gate uses the
local base tip and records explicitly that no remote exists, rather than
treating "nothing to fetch" as freshness.

## Naming the base branch explicitly

Most projects need nothing here: a repository with a discoverable remote
default, or no remote at all, resolves its base automatically at
registration. Name it in `.helm/project.json` when discovery would be
ambiguous, or to pin a base other than what the repository would resolve to:

```jsonc
// projects/api/.helm/project.json — pin the base explicitly
{"label": "API", "base_branch": "trunk"}
```

The explicit setting always wins. Only a project that never named one falls
back to the repository's own default, resolved once at registration: a
remote's recorded default when locally known (as a real `git clone` leaves
it), otherwise a direct, read-only query of the remote, otherwise — when
there is no remote at all — the branch checked out at that moment. A
repository **with** a remote never falls back to the checked-out branch: when
its default cannot be determined unambiguously, registration fails and asks
for an explicit `base_branch` rather than guessing. An already registered
project keeps its recorded base branch even if its checkout later switches;
only an explicit re-setting changes it. A single task can start from another
branch with `helm task create --base <branch>`, for a fix that must sit on a
release branch.

## One ticket, one worktree

`helm task create --ticket X` refuses when the project already has a worker
task for X on the same base whose worktree still exists, and points at `helm
task continue <id>` (after `helm task reopen <id>` for a failed or blocked
one). A foreman that made a task per round gave one ticket a dozen checkouts,
each with its own install and cold build; the round belongs on the task that
already holds the branch. Read-only tasks and a different base are exempt,
and `--new` starts a deliberate second line of work.

The branch is `helm/<project>/<ticket>-<task>` when a ticket was passed and
`helm/<project>/<task>` otherwise — one definition, used everywhere Helm
decides whether a branch is its own to touch.

## What a worker may touch

One task means one project and one task worktree. A worker never modifies
another project, Helm's own state, a foreman's files, or a user-owned
worktree, and never edits a project root as a shortcut. If the harness did
not supply a worktree, the worker creates a unique one under Helm `state/`
before editing. A task worktree must be clean to be approved, untracked
files included, and that check is not the thing to loosen.
