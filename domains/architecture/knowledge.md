---
id: architecture
applies_to: Choosing a structure before writing code, and improving it in verified steps.
use_when:
  - a new component, module or service is being started
  - a file or class has grown past what one person can hold in their head
  - an extraction or restructuring is being planned
selectable: false
---
# Architecture, chosen early and improved in steps

Small by design: compose it with `{"extends": ["architecture"]}`.

### Decide the structure before the first line, not after the ten-thousandth

A structure is cheapest to choose when nothing depends on it yet. Reaching for
"the simplest thing that works" and deferring the shape is not neutral — it
picks the shape by accident, and the accident is usually one file that grows
until every change to any concern opens the same file. So before writing, say
in one sentence what each part is for and which way the dependencies point.
A structure that is merely *reasonable* at the start beats a perfect one
arrived at after a rewrite.

That is not a licence to design ahead of the requirement. Name the pieces the
work actually has, give each a home and a direction, and leave the rest for
when it exists.

### Dependencies point one way, and that is the invariant worth enforcing

Layer the parts so that lower ones never import higher ones. This is the whole
difference between an extraction and a rename: a module that imports its parent
back still carries the coupling the split was meant to break, only now spread
across more files, which is worse than leaving it in one.

Because it is an invariant rather than a preference, assert it in the test
suite instead of remembering it. A rule kept by hand is broken by hand.

### Split on responsibility, never on line count

The test is a sentence: *can this module's responsibility be described without
using the word "and"?* "Provides state-backed primitives shared by the
components" is cohesive. "Handles status and gates and drivers and processes"
is four modules wearing one name.

Choosing the largest block instead is how a split reproduces the problem one
level down: the new file is smaller and just as incoherent. Size is a symptom
that invites the question; responsibility answers it.

### Improve it in steps that can each be proved

Restructure in moves small enough to verify one at a time, and verify each
before starting the next:

- Move definitions **verbatim**. A step that also rewrites cannot be checked by
  identity, and identity is the cheapest proof there is: every line removed
  must appear unchanged somewhere else, and every line added must be an import,
  a docstring or a declaration.
- Re-export moved names from the old home so call sites are untouched. A step
  that also edits its callers has two ways to be wrong.
- Run the whole suite each step and hold the result to an exact count agreed
  before starting. One extra failure means the step changed behaviour, and the
  step does not land.
- **Compute what a moved body needs; do not write the import list from
  memory.** Free names are derivable from the code, and a hand-written list is
  wrong in exactly the cases nothing exercises.

Identity and the suite catch different things. Identity cannot see a name that
now resolves in a different namespace; the suite cannot see a branch no test
walks. Keep both, and expect each to catch what the other misses.

### Leave what you cannot move cleanly, and say so

When a piece will not move without a semantic change — a function that names
its own class, a caller that reaches through a re-export — stop and put the
design question to the person who owns it, rather than smuggling a behaviour
change inside a move. Naming the obstacle is progress; hiding it is not.

### A bug fix's job is the bug; a structural idea found there is reported, not taken

Fixing a defect puts you in exactly the code where a better structure is most
visible, and acting on it there is a trap for three reasons: the diff stops
being reviewable as a fix, a regression can no longer be told apart from a
restructuring, and the person who asked for the fix did not agree to the
restructuring. So write the observation down — what you saw, why it matters,
what it would cost — hand it to whoever owns the decision, and land the fix on
its own.

The same boundary applies while building a feature, scaled to the size of the
change. A structural choice *inside* the feature's own new code is part of
doing the work: name it in the plan and get on with it. A change that reaches
outside it — a new module boundary, moving code that already exists, reversing
a dependency, altering a shared interface, or introducing a pattern the
codebase does not already use — is a decision about the whole, not about this
task, and is confirmed before it is built, not shown afterwards.

The test is not how many lines it touches. It is whether anyone other than you
would have to change how they work. If yes, ask first.
