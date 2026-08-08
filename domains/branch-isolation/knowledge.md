---
id: branch-isolation
applies_to: Keeping concurrent pieces of work from contaminating each other, and resolving a fresh, verified base before any worktree-backed task is created.
use_when:
  - several changes are in flight at once
  - a new worktree-backed task is about to be created, even a single one
  - a foreman is about to create a worktree-backed task for someone else
not_for:
  - allocating a worktreeless role's own workspace (foreman, reviewer) --
    neither gets a worktree of its own, even though a foreman still reads
    this before creating a worktree-backed task for someone else
selectable: false
---
# Branch isolation

Keeping concurrent pieces of work from contaminating each other.
Small by design: compose it with `{"extends": ["branch-isolation"]}`.

### Isolation when several pieces are in flight

```sh
git worktree add ../wt-<piece-id> <base-branch> -b <piece-branch>
```

Always confirm which worktree you are in before editing or building; once more
than one exists, the main checkout is not the piece you are working on. Remove a
worktree once its change is merged or no longer needed. Do not start a piece
that depends on an earlier one which has not passed verification — building on
code that may still change wastes the work.

### Stacked and independent branches

- The first piece, or the first in an independent group, branches off the target
  base and targets it for review.
- A **stacked** piece branches off its predecessor's branch, not the target
  base, and its review targets that predecessor's branch.
- An **independent** piece branches off the target base directly.
- Every stacked change states its position and dependency — "Stack: 2 of 4 —
  depends on #<n>" — and links the changes immediately before and after it.

**Rebase discipline is the part that rots silently.** When an earlier change in
a stack takes review feedback: fix it on that branch, push, then immediately
rebase every downstream branch onto the updated one and force-push each with
`--force-with-lease`. When an earlier change merges into the target base, rebase
the next branch onto **the target base**, not onto the now-merged branch, and
retarget its review.

### The base must be fresh and verified before a worktree is cut

This happens once, before a new **worktree-backed** task is created -- not
while a worker is already inside its worktree, and not for a driving role
(foreman, reviewer) that never gets a worktree of its own -- because a stale
or unverified base is a defect in the *isolation*, not in the work:
everything downstream (the branch, the diff, the review) inherits whatever
the base actually was. The driver that creates the task is the one that must
have it, so a foreman reads this before calling `helm task create` /
`helm worker launch`, not after.

- Resolve the project's **configured** default/base branch. Never hardcode a
  common name (`main`, `master`) and never take whatever branch the local
  checkout happens to be sitting on -- the configured value is per-project,
  not inferred per task. An explicit setting always wins; only a project
  that never named one falls back to a repository default, resolved once at
  registration: a remote's own recorded default when locally known or
  discoverable by a read-only query, otherwise -- only when there is **no
  remote at all** -- the branch actually checked out. A repository **with**
  a remote never falls back to the checkout: an ambiguous or
  undiscoverable remote default is reported, not guessed past, and neither
  is a detached checkout with nothing to ask.
- When that branch has a configured upstream, fetch it and verify the fetch
  **succeeded** -- a non-zero exit, a timeout, or an unreachable remote is
  the failure to block on. A branch with no upstream configured is not
  treated as local just because nothing named a tracking branch: when the
  project has a remote, look for one unambiguous same-named branch there
  and fetch that instead of trusting an unverified local tip; block if
  none or more than one remote has a match. A fetch that succeeds and moves
  nothing is still a verified fresh base: freshness means the state was
  checked just now, not that it changed. Never read a cached, unfetched
  remote-tracking ref as current, and never resolve the fetched value from
  a shared location another fetch could overwrite in between -- a failed
  fetch reports and blocks rather than falling back to either.
- Compare the local branch against the freshly fetched upstream tip. Equal or
  strictly behind: the upstream tip is the fresh base. Ahead of upstream, or
  diverged from it: **report and block** rather than mixing unmerged local
  commits into a task baseline -- reconciling that is the project owner's
  call, not this gate's.
- Detect an uncommitted change to a tracked file, or an unresolved
  merge/rebase/cherry-pick, in the project's own checkout, and **report and
  block** instead of merging, rebasing, resetting, forcing, or discarding
  anything to make it look clean -- none of those are this gate's decision
  to make on someone else's work. An untracked file (an uncommitted
  `.helm/project.json`, a build artifact) does not block: it changes
  nothing about what the base branch resolves to, and blocking on it would
  make this gate fight the project's own ordinary layout.
- A genuinely local-only project (no remote at all) uses its local base tip
  as the base and explicitly records that no remote exists, rather than
  silently treating "nothing to fetch" as freshness.
- Record the exact verified base **commit SHA**, plus enough evidence (was a
  remote actually fetched, when, and against what upstream) that a later
  review can reconstruct the decision, and create the task worktree/branch
  from that immutable commit -- a branch name is a moving pointer; the SHA is
  what makes the worktree reproducible.

## Approved learning: fresh configured base
- Fact: Before a new worktree-backed task is created, resolve the project's *configured* default/base branch (never a hardcoded or inferred name; a repository default is only inferred once, at registration, and only falls back to the checked-out branch when the project has no remote at all -- a remote with an ambiguous or undiscoverable default is reported, never guessed past). When that branch has an upstream -- configured, or the one unambiguous same-named branch on a remote when none was configured -- fetch it and verify the fetch *succeeded*, resolving the fetched value from a location nothing else can overwrite rather than a shared one; a successful fetch that moves nothing is still a fresh, verified base, and only a failed fetch (unreachable remote, non-zero exit) blocks. After a successful fetch, a local branch that is ahead of or diverged from its upstream also blocks, so unmerged local commits are never mixed into a task baseline; equal-or-behind uses the fetched upstream tip. Never switch, reset, rebase, merge, or force-update the project's own checkout to get there -- resolve refs directly, and block on an uncommitted change to a tracked file or an unresolved merge/rebase/cherry-pick there too (an untracked file does not block). A genuinely local-only project (no remote at all) uses its local tip and records explicitly that no remote exists. Record the exact verified base commit SHA, whether a fetch actually ran, and against what upstream, then cut the task branch/worktree from that immutable commit -- never from the project's current HEAD, which can move between a task's creation and its allocation.
- Rationale: A hardcoded or inferred branch name breaks on any project not named `main`; falling back to the checkout once a remote exists silently blesses whatever feature branch happens to be checked out as the project's base; treating "the fetch didn't change anything" as failure would block on the single most common outcome of asking a remote for its current state; and silently repairing a dirty, ahead, or diverged checkout risks discarding or misattributing work nobody asked to touch. Pinning to a verified commit, resolved before allocation and immune to the project's checkout moving afterward, is what makes a task worktree provably isolated and reproducible instead of merely convenient.
