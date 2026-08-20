---
id: software-delivery
applies_to: Building or changing reusable software behavior, or reviewing such a change.
use_when:
  - a feature, bug fix, refactor, or migration is being implemented
  - code written by an agent needs reviewing before it lands
  - a change needs sizing, branching, and a definition of done
not_for:
  - research, writing, or media production
  - video scripting or channel work, even when it mentions scripts or builds
  - a straight-through operational task -- upload, publish, schedule, run a
    job -- even when carrying it out edits a script, config, or tracker file;
    classify by what the task delivers, not by whether a file changed. See
    `driving-delegated-work` for the classification and its limits.
selectable: true
extends:
  - work-decomposition
  - agent-autonomy
  - model-selection
  - progress-reporting
  - definition-of-done
  - branch-isolation
  - change-sizing
  - spec-driven-development
  - pull-request-lifecycle
  - code-review
  - verification
  - agent-messaging
  - implementation-notes
---
# Software delivery domain

Taking a tracked unit of work from requirements to a reviewed, verified change.

For coding work delivered through a pull request, the reviewed branch is not
the finish line. Done means the PR has been opened, monitored, all actionable
comments and failing checks have been addressed through further coding/review
rounds, reviewers have approved, and Helm has recorded the PR as merged. Until
then the work stays active under the project's single foreman. The merge action
itself still requires the authorized human/tooling; an agent does not merge on
its own authority.

**This domain is a composition, not a document.** It holds no knowledge of its
own — it names the small domains that together make up software delivery, and
Helm loads the whole chain, bases first. Resolve `software-delivery` and a task
receives all of them; extend any single one when only that piece applies.

| Domain | Covers |
|---|---|
| `work-decomposition` | phasing, understanding before planning, sizing, sequential vs concurrent |
| `agent-autonomy` | where an agent decides alone; durable state outside the conversation |
| `progress-reporting` | reporting as you go; classifying a failure before reacting |
| `definition-of-done` | what finished means |
| `branch-isolation` | keeping concurrent work from contaminating itself |
| `change-sizing` | shaping a diff a reviewer can actually review |
| `spec-driven-development` | when behaviour is agreed in writing before it is coded, and working against it |
| `code-review` | the reviewer is a different agent from the author; the bounded loop |
| `verification` | whether observed behaviour is required, and proving it |
| `agent-messaging` | delivering a message between agents so it arrives |
| `implementation-notes` | the author's assumptions, written down so a reviewer can check them |

Splitting these used to be rejected on the grounds that a task would then
receive half the material. Domain composition removed that objection: the chain
loads in full, and each piece stays reusable on its own.

**Provenance.** Distilled from a set of role prompts for a
planner, coder, reviewer and verifier, plus their messaging protocol. Not
executed or independently verified against Helm's behaviour; treat it as
guidance rather than as a description of what Helm does.

Its origin was written for one team's stack — pull requests, an issue tracker,
an end-to-end test runner, mobile simulators. Anything of that shape here is an
**example filling a role**, not a requirement: read "review surface",
"tracker", "verification tool", and "runtime environment", and bind each to
whatever the project actually uses.
## Approved learning: branch ticket metadata
- Fact: Put the tracker ticket ID in the BRANCH NAME, not in code comments. A branch name is routing metadata a human reads once; a code comment is a permanent artifact whose reader may have no access to that tracker. Write comments so they stand alone - name the file, symbol or atom involved rather than a ticket number.
- Rationale: Tracker IDs in source comments age poorly and often require access to a private system. A branch can carry the ticket for routing while comments carry the enduring code-level reason.
## Approved learning: real UI verification
- Fact: Verify a UI fix in the real running app, not in a harness that mocks the surrounding surface. A harness that renders the changed element over a mocked version of its neighbour encodes the assumption under test and will confirm any fix.
- Rationale: Mocking the surface adjacent to the changed component can bake the desired result into the test and hide the real integration defect.
## Approved learning: stale bug reports
- Fact: Before implementing a bug report, check its filing date against the history of the code it names. A report older than the last rework of that code may already be fixed, and the fix may have landed under an unrelated ticket that never linked back.
- Rationale: Old tickets often survive after nearby refactors have removed or changed the reported behavior. Checking history first avoids fixing a bug that no longer exists.
## Approved learning: suggested remedies
- Fact: Treat a reporter suggested remedy as evidence of intent, not as the requirement. Build to the observed symptom; if the suggested mechanism would not produce the desired result, say so and record why.
- Rationale: A user may describe the mechanism they expect rather than the invariant they need. Verify the mechanism against the actual code before implementing it literally.
## Approved learning: comment restraint
- Fact: Keep code comments sparse, and each one super clear and concise — one short sentence where possible. Write a comment only for what the code cannot say itself — a non-obvious constraint, an external contract, a deliberate deviation. Do not narrate what a line does, restate the diff, argue the change is correct, explain history, or leave review-round commentary in the source; match the file's existing comment density rather than raising it. A multi-line block explaining a one-line decision is excessive: compress it to the single load-bearing fact.
- Rationale: Commander direction (2026-08-20) after generated changes carried excessive comments. Comments addressed to a reviewer are noise the moment the change merges, and every stale comment is a future contradiction the next reader must resolve against the code.
