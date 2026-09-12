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
  - architecture
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
