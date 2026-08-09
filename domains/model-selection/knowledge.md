---
id: model-selection
applies_to: Choosing which model, which reasoning effort, and -- given the fitness evidence below -- which runtime a piece of work runs on.
use_when:
  - an agent is deciding what to run its own work on
  - a reviewer must not be the same model as the author
  - code being written has to name a model for an API call
  - work is stalling on capability, or costing more than it is worth
  - a driver is picking among runtimes by what a project's own repository assumes (skills, conventions) before delegating
not_for:
  - the mechanical resolution order that turns a chosen runtime into a launch
    command -- that lives in the root's runtime rules, not here
selectable: false
---
# Model selection

Picking the model and the effort a piece of work runs at.
Small by design: compose it with `{"extends": ["model-selection"]}`.

## Three choices wear the same words

**The runtime** decides which models exist at all, and it is chosen for the task
the same way the model is — by fit, not by inheritance. An excluded runtime
takes its whole catalogue with it, so no reasoning about a model brings back one
that cannot be launched. Pick the agent first; it bounds everything below.

**The model the agent runs on** is scoped to the task. It changes nothing once
the task ends, so an agent may settle it alone.

**The model the agent's code calls** ships in a repository and outlives the
task. Changing a model string in someone's product changes their cost, latency,
and output quality for every user, none of which the task asked about. Propose
it with the trade-off stated; never swap it while doing something else.

## Choosing the agent: what the repository already assumes

Agents differ less in raw capability than in **what they can read without being
told**, and that is a property of the repository, not of the agent.

**Check where a project's skills live before choosing.** A project that keeps
them in `.agents/skills/` has made them portable — any agent can be pointed at
`SKILL.md` directly. A project that keeps them only in `.claude/skills/` has
wired them for Claude Code's path-scoped auto-loading, and every other agent
starts blind to conventions the repository considers mandatory. Running such a
project on a different agent silently drops that guidance; the work still
compiles and still violates the house rules.

Discovery happens **inside the selected project only**, before delegation, and
what it finds stays there: read the project's own skill manifests to see what
exists and what each one is for, never copy a skill's contents into Helm's own
tracked files, and never install or enable a skill automatically just because
it was found. Select from what a manifest's own metadata/description says —
only the skills that plausibly bear on the task at hand, not the whole
directory by default. Record which ones were selected (or explicitly "none",
with the reason), their project-local paths, how the chosen runtime will load
them (auto-loaded by convention, or named explicitly in the brief), and why —
in the brief and the project record, so a driver picking this up later does
not have to re-derive the decision.

A skill is guidance a worker reads, never authority it can invoke. It cannot
expand the task's scope, authorize a merge, publish, push, deletion, or other
protected action, override a core safety rule, or grant a credential or
capability the runtime does not already have. Treat a task's required skill
that is missing, or unreadable by the runtime that was chosen for other
reasons, as a capability blocker to report — not as license to improvise past
it or invent equivalent guidance.

So the first question is not "which agent is best" but "what does this
repository already assume its agent can see":

| The repository | Reach for |
| --- | --- |
| Keeps skills only under `.claude/skills/` | `claude`, or another agent given the `SKILL.md` paths explicitly in its brief |
| Keeps skills under `.agents/skills/` too | Any agent — the conventions are portable by design |
| Has no skills, just `AGENTS.md` | Any agent; fitness comes down to the model and the harness |

After that, the differences that actually decide a task:

- **Model breadth.** A gateway-backed agent reaches vendors the others cannot,
  which matters when the task needs a specific model — most often a reviewer
  that must not be the author's.
- **Harness shape.** Subagents, skills, hooks, and MCP are the agent's, not the
  model's. Work that fans out across many files benefits from an agent that can
  delegate; a single well-specified edit does not.
- **What it costs to run.** An agent excluded by the root is excluded whatever
  its fit — that is a policy decision made in advance, and fitness reasoning
  does not reopen it.

Enumerate rather than assume here too: an agent is available only when its
executable is on `PATH`, and the presentation layer integrating an agent does
not mean Helm can launch it.

## Inventory the machine before choosing

Treat every machine as different. Before dispatching meaningful work on a fresh
or changed machine, enumerate the launchable agents and model catalogues that
exist **there**, then choose from that live set rather than from memory.

Runtime inventory is a two-column question:

- `helm agent check` answers what Helm can actually launch: built-in runtime or
  profile, executable on `PATH`, capacity, detection, and Herdr recognition
  when available.
- `herdr integration status` answers what Herdr can recognize or control once
  an agent is already running. That is useful for diagnosis and fit, but it is
  not enough to launch work unless Helm also has a runtime/profile for it.

Model inventory is runtime-specific and should be checked near the time of
dispatch, especially for reviewers and expensive tasks:

- `pi --list-models [search]` for pi's reachable providers and exact model ids.
- `opencode models [provider]` for opencode's gateway catalogue.
- For interactive agents whose catalogue is exposed only inside the agent UI,
  ask the agent with `/model` or `/models` in its Herdr pane and use the exact
  IDs it reports. Do this as discovery, not as a worker task, and do not keep
  stale IDs in the brief.

The dispatch decision is then: repository assumptions and skills first, task
shape second, live model availability third, cost fourth. If the best-fit model
is not reachable from the machine doing the work, choose the nearest available
tier and report that substitution in the task status.

## Effort is the first lever

Every runtime exposes a reasoning-effort knob, and it moves quality and cost
further than a tier change does — same model, nothing to migrate. Raise it as
scope, uncertainty, or the cost of being wrong goes up; lower it when a cheaper
level demonstrably holds on real work.

**The ladders are not the same ladder.** They share names without sharing
meaning, and neither the rung count nor the default matches:

| Runtime | Levels | Default |
| --- | --- | --- |
| `claude` | `low` `medium` `high` `xhigh` `max` | `high`; `xhigh` is Claude Code's own main-loop setting |
| `codex` | `low` `medium` `high` `extra high` `max` `ultra` | `medium` |
| `pi` | `off` `minimal` `low` `medium` `high` `xhigh` `max` | per model |
| `opencode` | `--variant`, whatever the chosen provider offers | per model |

Codex's `ultra` is not simply "more than max" — it splits divisible work across
parallel subagents, so it is a shape of work, not a dial position. pi adds `off`
and `minimal` below the others, which is the cheap end nothing else offers.
opencode has no ladder of its own at all: `--variant` passes the level through
to whichever provider the model came from, so the valid values change with the
model rather than with the runtime.

A level does not transfer even within one vendor: vendors say plainly that no
exact mapping exists between the effort levels of successive model generations.
Re-tune when the model changes rather than carrying the old rung across.

At the top of any ladder, give the run a large output budget. Reasoning and
answer share one ceiling, so a budget sized for the answer alone truncates
mid-thought, and the retry costs more than the headroom would have.

## Enumerate the catalogue, never recall it

Model line-ups turn over in months, and a confidently stated stale name is a 404
at best. Read the machine:

- `pi --list-models [search]` prints every model pi can reach, with context
  window, output cap, and thinking and image support. It is the fastest
  catalogue on the box even when the work will not run under pi.
- `opencode models [provider]` prints its own, which is far wider — a gateway
  credential reaches many vendors at once, so this is where to look for a model
  no other runtime here can start.
- Claude capabilities are queryable from the Models API; the `claude-api` skill
  is authoritative for that surface and outranks anything recalled.
- The catalogue reflects **credentials actually present**, not what a vendor
  sells. A provider with no key does not appear — pi's own default provider can
  be one of them, so "the default" is not proof of availability.

A model is named, never invented. IDs are exact and complete as written; a date
suffix or a plausible-looking variant fails. An unfamiliar ID means it shipped
after training, not that it is wrong.

## Start from the task, not from the model

Read down the left column until a row describes the work, then take the tier on
that row. The tiers are what matter; the names in them change.

| The task looks like | Tier |
| --- | --- |
| Classification, extraction, formatting, a short lookup, a mechanical rename | cheapest |
| Routine implementation, a well-specified fix, high-volume coding, a first-pass summary | middle |
| Multi-file change, a refactor, debugging something that is not reproducing, work that must run unattended for a while | top |
| A problem that has already defeated the tier below, or the longest autonomous runs | frontier |
| Reviewing a change | at least the tier that wrote it, on a **different model** |

Deliberately no "today that means" column. A committed list of model names goes
stale in months, and a stale name asserted confidently is a failed launch. Map
the tier onto a real id at dispatch time from the live catalogue, using the
commands above.

Two rows deserve their exceptions stated. **Difficulty you cannot yet judge is
not cheap work** — an underspecified brief belongs a tier above where its word
count suggests, because the cheap tiers are the ones that fail by confidently
doing the wrong thing. And **review never goes below the work it reviews**: a
cheap reviewer on a hard change produces agreement, not scrutiny.

When a task's shape is genuinely unclear, prefer the middle tier and raise on
evidence. Starting high and never re-checking is how a fleet quietly costs
several times what it should; starting low and never raising is how it produces
work nobody can use.

## What a tier costs is read, not recalled

Vendor rates change, and they are not the bill anyway. Two things have to be
checked at dispatch time rather than remembered:

- **The rate.** Read it from the vendor's own current pricing, or from the
  catalogue command for the runtime that will run the work. A price written
  into a committed file is true on one day and misleading after it.
- **How the runtime is authenticated.** API-key billing and a consumer
  subscription behave completely differently: per-worker cost under a
  subscription has little to do with a per-token rate, and a fleet of workers
  can cost far more or far less than a per-token rate implies. Check which is in
  play before reasoning about cost at all, and treat an unexplained cost as a
  fault to investigate rather than a rate to divide.

Within one vendor's line-up the shape is usually stable even when the names are
not: a small fast model, an everyday workhorse, a top coding/agentic model, and
sometimes a frontier tier above it, each roughly a multiple of the one below.
Reason in those tiers, then resolve the tier to an id from the live catalogue.

Two properties worth checking rather than assuming, because they change what
the tier is good for: whether the model exposes a reasoning-effort parameter at
all — the cheapest tier often does not, which makes it the wrong choice for
anything whose difficulty is uncertain — and how large its context window is.

**Pinning an older model is a deliberate act with a reason** — a workload tuned
against it, a reproduction being chased. Drifting onto one because a config was
never revisited is not, and vendors retire models on announced dates, so a pin
stops working rather than degrading. Check the pin against the live catalogue
before the date somebody else discovers it as an outage.

## A root may restrict a model family to certain runtimes

Some installations require that a given vendor's models only ever run under
that vendor's own agent CLI — for cost, for account, or for support reasons.
That is a **root-local operator preference**, not a property of the model and
not a rule Helm ships: a root sets it with `helm prefs set
model.runtimes.<family> <runtime>...`, and Helm then refuses the pairing at
launch rather than substituting either half.

So before reasoning about which runtime may run a model, check `helm prefs
show`. A restriction that is in force narrows the reviewer field too, and a
restriction that is absent means nothing is refused.

## pi and opencode are the cross-provider ones — which is why they review

**A reviewer that is the same model as the author is close to self-review.** It
shares the author's blind spots and tends to ratify the reasoning that produced
the bug. Independence is the whole point of a review, so the reviewer wants a
different *model*, not merely a different process.

Both of these reach several vendors behind one flag, which is what makes them
the reviewers rather than a second copy of the author:

- **pi** takes `--model provider/id`, a fuzzy pattern, and a `:level` thinking
  shorthand — `--model <id>:high` and `--model <provider>/<id>` are both valid.
  Its catalogue is whatever its credentials reach.
- **opencode** takes `--model provider/model` and reaches a much wider
  catalogue through a gateway provider — many models spanning several vendors
  on one credential. `opencode models` lists them.

Reach for the model flag before reaching for a different runtime: switching
model is what buys independence, and switching runtime is only the crude way to
get it. Where a root restricts a model family (above), the runtime does have to
move, because only the allowed runtimes may run that family at all.

## Cost and capability are the commander's trade-off

Downgrading to save money is a decision about what the work is worth, and that
belongs to whoever is paying. Run the tier the task was given, say plainly when
a cheaper one would have held, and let the answer come back.

Upgrading has the same shape. Work that is failing on capability should say so
and name the tier it wants, rather than burning attempts at a level that cannot
reach the answer.

## What this domain does not claim

It gives a method for choosing a tier and for reading the live catalogue and
the live rate. It does **not** name models, quote prices, or rank vendors
against each other:
that comparison moves faster than a committed file can track, and a stale
ranking asserted confidently is worse than none. When a task genuinely turns on
a cross-vendor comparison, measure it on that task and report what was measured.
