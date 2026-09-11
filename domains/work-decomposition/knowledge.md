---
id: work-decomposition
applies_to: Breaking work into phases and sizing it before building.
use_when:
  - a task is too large to start directly
not_for:
  - work already scoped to one clear change
selectable: false
---
# Work decomposition

Sizing and phasing a piece of work before anyone starts building it.
Small by design: compose it with `{"extends": ["work-decomposition"]}`.

## Work in phases, and do not merge them

Understand → plan → implement → review → verify → deliver. The phases force
decisions into the open before code is written; skipping one hides the decision
rather than removing it.

## Understand before planning

Extract both halves of the requirements, because the second half is what gets
dropped:

- **Functional** — behaviour, edge cases, acceptance criteria.
- **Non-functional** — performance, security, accessibility, offline behaviour,
  analytics/telemetry events, feature flags, platform constraints.

Cross-check the ticket against the project's own guidance (solution docs,
`CLAUDE.md`, `AGENTS.md`) and **name conflicts and gaps explicitly** rather than
silently preferring one source. Write a requirements summary and publish it to
the tracker so other teams reuse it instead of re-deriving it.

## Size the work before decomposing it

**Estimate the total diff first.** Under roughly 500 changed lines, do not break
the work down at all: one unit, one review, one change. Decomposition is not the
default sophisticated choice — it is overhead that pays only when size or
coupling warrants it.

When it does, each piece should be independently implementable and reviewable,
have a testable "done" condition, and be decoupled where possible, with any
unavoidable dependency stated.

**Traceability is a coverage check, not paperwork.** Map every requirement —
functional, non-functional, and each tech-guidance decision — to at least one
piece of work. A requirement with nothing mapped to it means the breakdown is
wrong and must be fixed before proceeding.

Tag every traceability item with how it will be verified:

- **unit** — no user-facing behaviour change; test results suffice.
- **e2e** — touches behaviour a user interacts with; needs a real run.
- **When unsure, default to e2e.** It is the safer error, and the verifying role
  can hand it back if it proves unnecessary.

## Sequential or concurrent execution

Choose from the actual dependency graph, never by default.

**Sequential** — one piece fully through implement → review → verify → change
before the next starts. Choose it when pieces are tightly coupled or touch
overlapping files, when concurrency would cause real resource contention (two
simultaneous builds on one machine), or when there are only one or two pieces
and pipelining is not worth the complexity.

**Concurrent** — roles work different pieces at once, each trailing the one
before it. Choose it when pieces are independent or only loosely dependent, and
when there are three or more so the throughput gain is real. **Concurrency
requires worktree isolation, and that is not optional**: each piece in flight
gets its own worktree so two roles never share a checkout.

A mixed graph is fine — an independent group and a dependent chain can run under
different modes, with the reasoning stated per group.
