---
id: implementation-notes
applies_to: Recording the assumptions behind a code change so a reviewer can check them.
use_when:
  - a non-trivial code change is being authored
  - a reviewer needs to check assumptions against a diff
not_for:
  - changes with no assumptions worth stating
  - non-code work
selectable: false
---
# Implementation notes

Making an author's assumptions reviewable.
Small by design: compose it with `{"extends": ["implementation-notes"]}`.

## Every non-trivial change ships an implementation-notes file

The author writes `implementation-notes.html` alongside the change, listing
**every assumption it made**: what it took as given about inputs, callers,
data shape, environment, versions, concurrency, failure modes, and scope. One
row per assumption, each saying what was assumed, why, and what breaks if it
is wrong.

An assumption is invisible in a diff. The code shows what was written, never
what the author believed while writing it, so a reviewer reading only the diff
can check whether the code is self-consistent but not whether it is *founded*.
Unstated assumptions are where the surviving bugs live.

HTML rather than prose in a commit message because it is read outside the
terminal, in a browser, by a human or another agent, and because a table of
assumptions is a structure, not a paragraph.

## The reviewer reads it second, never first

Order matters, and it is the opposite of convenience:

1. Read the diff and the claim the change makes about itself. Form a view.
2. *Then* open the implementation notes.
3. Check each assumption against the code: is it actually what the code does,
   is it true of the real system, and is anything in the diff resting on an
   assumption the author did not list?

Reading the notes first is how a reviewer adopts the author's frame and stops
being independent — the failure the `code-review` domain exists to prevent.
Reading them second turns them into a checklist the author cannot mark off for
themselves.

An assumption the reviewer cannot verify is a finding, not a footnote. An
assumption that turns out to be false is a finding even when the code passes
its tests, because the tests were written under the same assumption.

## Missing notes are a review blocker

A non-trivial change without implementation notes is returned unreviewed. The
reviewer does not reconstruct the author's assumptions on the author's behalf:
doing so invents a frame and then checks the code against the invention.
