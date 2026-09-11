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

## A code comment explains the code, not the ticket that prompted it

Do not put tracker ids in source comments. `// (PROJ-1234)` tells a reader
where a conversation happened, not what the code does or why it is shaped that
way — and the reader is usually someone with no access to that tracker, or
reading years after the ticket was archived, or an agent with no tracker tools
at all. The comment survives; the context it points at usually does not.

Write the reason instead. If the ticket held a fact the code depends on, the
fact belongs in the comment: not `// PROJ-1234`, but `// the vendor returns
milliseconds here despite the field name`. If the ticket held nothing worth
restating, the reference was never carrying information.

The tracker id belongs where it is addressed to a human doing tracker work —
the branch name, the commit message, the pull request, and a spec or design
document whose whole purpose is to record a decision and its provenance. Those
are read alongside the tracker. Source is not.

Watch for the version of this that looks like documentation: a doc comment
saying "added for PROJ-1234" reads as an explanation and contains none. If
deleting the id would lose nothing, it was noise; if deleting it would lose
something, that something should have been written down instead.

## Comment the surprise, not the code

Excessive commenting is not thoroughness. A comment that restates what the line
below it already says adds a second thing to keep true, and the two drift: the
code gets changed and the prose does not, so the comment becomes a confident
lie in a place readers trust. Every comment is a maintenance liability, and it
has to earn that.

What earns it: a reason that is not visible in the code — why this order, why
this bound, why the obvious approach was rejected, what a vendor does that its
field name denies. A decision someone would otherwise undo. A trap the next
reader will step in.

What does not: restating the signature, naming the types, describing what a
well-named function plainly does, narrating the happy path, or a paragraph of
motivation that belongs in the design document. If the file already has a spec
or a contract document, the motivation lives there and the code links to it
once rather than repeating it.

The test is deletion. Remove the comment and ask what a competent reader loses.
If the answer is nothing, it was noise; if the answer is a fact they could not
have recovered from the code, keep it and make it one sentence.

A useful smell: comment lines approaching or exceeding code lines in a new
file. That density is almost never justified outside a genuinely subtle
algorithm, and it usually means the motivation, the contract and the reasoning
have all been poured into the header because nobody decided where they belong.
