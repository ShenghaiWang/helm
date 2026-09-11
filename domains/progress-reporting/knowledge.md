---
id: progress-reporting
applies_to: Reporting progress as work happens, and triaging failures.
use_when:
  - long-running work nobody is watching
  - a failure needs classifying before reacting
selectable: false
---
# Progress reporting and failure triage

Reporting as you go, and classifying a failure before reacting to it.
Small by design: compose it with `{"extends": ["progress-reporting"]}`.

## Report progress rather than expecting anyone to watch

Unattended work is invisible unless it reports. Post a short update at each
milestone — plan ready, each stage transition, done, and **every escalation
immediately**. One line each; the point is the timeline, not the prose.

**Going ~20 minutes with no update while work is supposedly in progress is
itself a signal.** Treat it as a prompt to check whether something is stuck, not
as evidence that things are fine.

## Promote follow-ups to action items

A progress line can say what happened, but a required decision or follow-up
must also be recorded as an action item. Use `helm project action <project>
"..."`, or include an `action_item` / `follow_up` payload on a summary worker
message. A phrase like "follow-up needed" is also promoted automatically, but
explicit payloads are clearer and survive wording changes.

## Diagnose failures by class

A failed verification is either a **coding bug** — fix, re-review, re-verify —
or a **plan gap**, in which case fix the plan first and re-issue it. Never let
implementation silently deviate from a plan that turned out wrong; correct the
plan so the record stays true. Treat a CI failure with the same priority as a
failed verification.

## Where a long report goes

A report too long for a protocol message goes in a **file** — a message that
gets truncated mid-finding is worse than no message, and it has happened: a
reviewer's report dissolved into its own terminal spinner frames partway
through the finding that mattered.

**Write it to your own worker directory, not to the task worktree.** Helm gives
you `state/workers/<your-worker-id>/`, and that is the place:

```
state/workers/<your-worker-id>/ROUND_3_FINDINGS.md
```

Not the worktree. A file left in the worktree is either committed — putting a
working note into the project's history, where it does not belong — or left
untracked, and an untracked file makes the task **unapprovable**: Helm refuses
approval on an unclean workspace, which is the right rule, because it cannot
tell your report from work you forgot to commit. Three rounds in one evening
stalled that way, each needing the file moved out by hand before the merge
could proceed.

The worker directory has neither problem. It survives the session, it is not in
the repository, and it does not dirty the tree. Name the file in your protocol
message with its full path so a reader can find it:

> Full report: `state/workers/<your-worker-id>/ROUND_3_FINDINGS.md`

Keep the message itself to the verdict and the headline — enough that a reader
who never opens the file still knows what happened and whether it needs them.
