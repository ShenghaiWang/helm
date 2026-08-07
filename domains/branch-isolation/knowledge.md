---
id: branch-isolation
applies_to: Keeping concurrent pieces of work from contaminating each other.
use_when:
  - several changes are in flight at once
not_for:
  - a single change on a single branch
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
