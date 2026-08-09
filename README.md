# Helm v1

Helm exists so that one person can run many projects at once.

That person is the **commander**. The scarce resource is not compute and not
agents — it is their attention, and Helm's whole design is about spending it
only where it is actually needed. A commander says “work on the next Publishing
artifact” or “add a refresh button to the session list”, and Helm turns that into
a task, a worktree, a briefed agent, and a review, then comes back with the one
or two things that genuinely need a human.

Three ideas carry that.

**Parallelism through isolation.** Several projects run at the same time, each
with its own worktree, branch, agent, and context. Nothing leaks between them:
a worker serves exactly one project, its context document contains exactly one
project, and no project's findings can answer another project's question. That
strictness is what makes running five projects at once safe rather than
frantic — see [Strict project isolation](#strict-project-isolation).

**Delegation, all the way down.** The coordinator does not do the work. It
chooses the project, composes the context, and appoints a **foreman** for each
project; the foreman turns goals into tasks, spawns **workers**, answers their
questions, and runs an independent **reviewer** against what they produce.
Every layer decides what it can and escalates only what it must, so routine
choices — naming, file layout, which of two acceptable approaches — never
reach the commander at all. What does reach them is the short list Helm cannot
decide: merge, push, publish, delete, missing credentials, a genuine change of
scope.

**Knowledge that compounds.** A finished task is evidence. Helm turns that
evidence into proposed **domain learnings**, and once approved they are appended
to the domain's knowledge and attach automatically to every future task that
resolves it — including tasks in other projects that share the domain. Work
therefore gets cheaper over time instead of repeating the same discoveries:
this is the self-learning loop, and it is deliberately two-step, because
nothing may approve its own knowledge. See
[Learning proposals](#learning-proposals) and
[Automatic bounded domain context](#automatic-bounded-domain-context).

Helm is local-first: a coordination protocol plus an optional CLI, with no
remote service and no autonomous merge, push, or publish automation. The primary
interface is the repository itself — start any supported agent with this
directory as its working root and it should read [`AGENTS.md`](AGENTS.md)
automatically, without running Helm, selecting a provider, or configuring a
worker executable.

### How the pieces fit

```text
commander  → the human; owns approval and anything irreversible
coordinator→ picks the project, composes context, holds the approval gate
foreman    → one per project; turns goals into tasks, drives and answers workers
worker     → one per task; works only in its own worktree, reports by protocol
reviewer   → independent agent; cross-checks a change before anyone trusts it
```

Each layer answers what it can and escalates only what it cannot. A confirmation
that stops at the foreman never becomes an interruption for the commander.

## Start here: repository-native agent workflow

A normal checkout is already a usable default Helm root. Enter the repository
and start the supported agent there; do not initialize it first:

```sh
cd /path/to/helm
# start your supported agent with this directory as its working root
```

The tracked `.gitkeep` files establish the default `projects/`, `domains/`,
`agents/`, and private `state/` directories. Their contents are local: project
repositories, agent profiles, state databases, locks, worker output,
credentials, and task worktrees are ignored. Domains are the one exception —
the shared, general-purpose packs listed in `.gitignore` ship with the
repository, while any other domain a root grows stays local, so private or
company-specific knowledge is never committed by accident. `helm init <other-root>`
remains available only when explicitly creating a separate custom Helm root.

The native path does not require Python, a Helm installation, `helm init`, or
any worker configuration.

1. Read `AGENTS.md`, then inspect the existing Helm root. Do not turn a normal
   conversation into initialization or project registration.
2. Discover only direct children of `projects/`. A child is available only if
   it is an isolated Git repository with a commit; discovery never initializes
   Git. Choose one project for the request, and ask if the project or task is
   ambiguous.
3. Load the selected project's domain defaults from
   `projects/<id>/.helm/project.json` (`domains`, `default_domains`, or
   `domain`). Resolve one unambiguous domain and load its knowledge and
   guardrails, followed by the project's `.helm/knowledge.md`. Missing files
   are explicit missing sources. All of this material is guidance, not
   authorization.
4. Before creating a worktree-backed task, resolve the project's *configured*
   default/base branch — never a hardcoded name and never whatever the
   checkout currently sits on — and, when it has an upstream, fetch it and
   verify the fetch succeeded. A fetch that succeeds and moves nothing is
   still a fresh, verified base; only a failed fetch blocks, and it must not
   fall back to a cached ref. Block and report a local branch that is ahead
   of or diverged from its upstream, or an uncommitted change to a tracked
   file or an unresolved merge/rebase/cherry-pick in the project's own
   checkout, instead of merging, rebasing, resetting, or discarding
   anything; an untracked file (an uncommitted `.helm/project.json`, a
   build artifact) does not block, and a local-only project records
   explicitly that no upstream exists. Record the exact verified base
   commit and cut the task worktree/branch from that commit. See
   `domains/branch-isolation/`.
5. Delegate the work to a worker agent spawned in a dedicated Helm-owned Herdr
   space. The coordinator does not do the work itself.
6. The worker works only in the assigned task worktree. If the agent harness
   did not provide one, create a unique task worktree under Helm `state/`
   before editing; never use a project root or another task's worktree as a
   shortcut.
7. After useful work completes, suggest concise durable domain learnings with
   evidence from the task result, artifacts, messages, and review outcome. A
   suggestion is only a persisted proposal; inspect/edit/reject it or wait for
   explicit user/coordinator approval before promotion.
8. Commit the result to the task branch, report the changes and any blockers,
   and wait for approval before merge, publish, push, deletion, or another
   destructive/external action. Approval is tied to the reviewed worker
   revision and tree; changes after approval require another review.

## Mandatory delegation

The agent started in the Helm root is the coordinator, and it never performs a
request's work inline. It creates the task, spawns one worker agent per task in
that project's one Helm-owned Herdr space — reusing the workspace and worker
tabs recorded in Helm state, creating only what Herdr no longer has — and then
drives that worker and relays its protocol messages.

Herdr is detected only in a managed session (`HERDR_ENV=1` plus an available
`herdr` executable). When it is unavailable, delegation still applies: the
worker is spawned through Helm's core process launcher into the same isolated
task worktree, and only the presentation surface is lost. To spawn a worker the
coordinator discovers runtime/tool capabilities from the active environment or
harness rather than assuming a provider-specific command; if no runtime exists
at all it asks for one and explains the limitation instead of doing the work
itself. See [`AGENTS.md`](AGENTS.md) for the complete boundary rules and
[`docs/agent-adapters.md`](docs/agent-adapters.md) for optional adapter
conventions.

## Helm root layout

The root may be this checkout or another directory explicitly initialized as a
Helm root. In this checkout, only the placeholder files are tracked; the
contents below are local and ignored:

```text
<helm-root>/
  AGENTS.md                         # canonical agent entry point
  projects/.gitkeep                  # tracked default; project contents ignored
  projects/<project-id>/             # direct-child, isolated Git repositories
    .helm/project.json               # optional label, policy, domain, agent, and base-branch defaults
    .helm/knowledge.md               # optional project guidance
  domains/.gitkeep                  # tracked default
  domains/software-delivery/        # shared base pack: lifecycle, roles, coordination
  domains/<domain-id>/domain.json   # optional {"extends": [...]} composition
  domains/<domain-id>/
    knowledge.md
    guardrails.md
  state/.gitkeep                     # tracked default; private state ignored
  state/                             # private Helm state, proposals, and task worktrees
  agents/.gitkeep                    # tracked default; profiles are local
  agents.json                        # optional advanced profile override
  agents/<agent-id>/profile.json    # optional advanced profile override
```

An agent must not modify another project, `state/` records, firstmate files, or
user-owned worktrees. The core implementation is the authority for isolation,
root/state validation, process fallback, context boundaries, approval
immutability, and Herdr ownership: see [`helm/core.py`](helm/core.py) and
[`helm/herdr.py`](helm/herdr.py).

Tracked Helm files are generic product assets. They must not contain concrete
information from projects managed by a local Helm root: no real project names,
ticket histories, analytics, artifacts, branch names, task/message IDs, or
task-specific learnings. Keep those in ignored `projects/` checkouts, ignored
`state/`, or a private local domain pack. Public docs, tests, shared domain
packs, and examples use sanitized fixtures only.

## Strict project isolation

Isolation covers knowledge and context, not only write access. One worker
serves exactly one project; workers, tabs, worktrees, and conversations are
never reused across projects. Composed context contains one project's material
only, and the coordinator does not carry another project's findings, files,
conventions, or credentials into a brief or an answer. Learning proposals stay
in the task's resolved domain. A request that appears to span projects is
stopped and clarified rather than merged.

## Agent runtimes

A worker is an agent CLI, and Helm knows how to start several without any
configuration. The built-in runtimes are `claude` (Claude Code), `codex`
(Codex CLI), `pi`, `opencode`, and `omp` (Oh My Pi); each contributes only an executable, the
argv that hands it one prompt, and the credential variables that runtime reads.
A runtime is available when its executable is actually on `PATH` — a name alone
never makes one available.

Herdr integrations are useful signal, but they are not Helm launch definitions.
`herdr integration status` tells Helm which agents Herdr can recognize and
control once they are running; `helm agent check` includes that live inventory
when it can query Herdr safely. Helm may use it when judging fit or diagnosing a
pane. A new Herdr integration becomes selectable for delegated work only when
Helm also has a built-in runtime entry or an `agents.json` profile with a real
launch command, environment passthrough, and any availability check it needs.

`pi` and `opencode` both reach several vendors' models behind one flag, which
makes them the useful reviewers: an independent review wants a different model
from the one that wrote the change, and those two put one a `--model` away.

The runtime for a task is resolved most-specific-first:

1. the task's own choice — “use Codex for this one”, or `--agent codex`;
2. the project's pin in `projects/<id>/.helm/project.json`;
3. `HELM_AGENT`, or a configured profile when one exists;
4. otherwise the runtime this Helm session is itself running under, detected
   from the environment.

So the default is “workers use the same agent I do”, one project can be pinned
to a different agent than the rest, and any single task can override both:

```jsonc
// projects/api/.helm/project.json — every worker for this project runs Codex
{"label": "API", "domains": ["backend"], "agent": "codex"}
```

### Choosing the model, not just the runtime

Naming a runtime does not name the model it runs, and the two are separate
decisions: “this project runs on Codex” and “this task is mechanical, run it
cheap” are different statements, and either can be made without the other. The
model resolves the same way, most-specific-first — `--model` on the task, then
the project's `"model"` pin, then `HELM_MODEL`.

There is deliberately **no detection step**. A wrong runtime guess fails loudly
on a missing executable; a wrong model guess runs, bills, and answers. So when
nothing is stated, Helm passes no model at all and the runtime keeps its own
default. The `model-selection` domain tells coordinators to check the live
catalogue near dispatch time — for example `pi --list-models`, `opencode
models`, or an interactive agent's `/model` or `/models` command in Herdr —
before naming a model for important work.

```jsonc
// projects/tickets/.helm/project.json — cheap model, because the work is mechanical
{"label": "Tickets", "agent": "claude", "model": "claude-haiku-4-5"}
```

Only a built-in runtime publishes the flag that selects its model. A profile
that supplies its own command does not, so a model aimed at one is **refused
rather than dropped** — silently ignoring it would leave the coordinator
believing it had instructed a model it never sent, and the bill is the only
place that difference would show up. Which model suits which task is knowledge,
not policy, and lives in the `model-selection` domain.

```sh
helm --root <helm-root> run api "Fix the failing import" --agent pi
helm --root <helm-root> agent check      # which runtimes this machine can start
```

An unknown name is rejected with the list of known agents, and a known runtime
whose executable is missing is reported unavailable rather than quietly
replaced. Detection is last precisely because it is a guess; when nothing is
pinned, configured, or detectable, Helm asks for a runtime instead of inventing
a command. Set `HELM_AGENT=none` to require an explicitly named agent.

A worker is started in its interactive form inside a Herdr pane, where it has a
real terminal, and in its non-interactive print form on the process fallback,
where a full-screen TUI would only write escape noise into the log.

Worker environments stay scrubbed. A runtime declares the few variables it
reads — `ANTHROPIC_API_KEY` for Claude Code, `OPENAI_API_KEY` for Codex, and so
on — and only those are forwarded, only for a worker actually launched with
that runtime. Nothing else in the coordinator's environment reaches a worker.

Configured profiles still work and now compose with the built-ins: a profile
may name a runtime instead of spelling out a command, so pointing a domain at a
different agent is a one-line change. See
[`helm/runtimes.py`](helm/runtimes.py) for the table itself.

```jsonc
// agents.json
{"agents": [
  {"id": "shorts", "runtime": "codex", "domains": ["publishing"], "capacity": 2},
  {"id": "pi", "domains": ["research"]}
]}
```

A profile's `capacity` remains a deliberate throttle. A built-in runtime has no
limit of its own, so several workers can run under the same runtime at once.

## Learning proposals

This is how Helm gets better at a domain instead of rediscovering it. A
finished task leaves evidence — its result, artifacts, messages, and review
outcome — and that evidence becomes a candidate fact for the domain the task
resolved. Once approved and applied, the fact lives in
`domains/<domain-id>/knowledge.md`, which means it is loaded automatically into
every future task that resolves that domain, in any project. The loop is:

```text
task completes → propose (with evidence) → approve → apply → attaches to
every later task in that domain, automatically
```

Learning is deliberately a two-step promotion flow. When a worker reports a
`result`, Helm attempts to create inert candidate proposals from the completed
task's evidence; a coordinator can also propose one explicitly. Worker output,
a domain file, or a proposal can never approve itself — knowledge that could
approve itself would let a single confused worker teach every future one:

```sh
# Extract result/artifact candidates, or provide one concise fact explicitly.
helm --root <helm-root> learning propose <task-id> --fact "Use captions for artifacts"
helm --root <helm-root> learning list --status proposed
helm --root <helm-root> learning inspect <proposal-id>
helm --root <helm-root> learning edit <proposal-id> --fact "Use captions on artifacts"
helm --root <helm-root> learning approve <proposal-id> --note "reviewed evidence"
helm --root <helm-root> learning apply <proposal-id>
# Rejection is also explicit and leaves the proposal as provenance.
helm --root <helm-root> learning reject <proposal-id> --note "not reusable"
```

Helm infers a proposal's domain from the completed task when that mapping is
unambiguous. If it is ambiguous, pass `--domain`; a task's selected domain
cannot be replaced with an unrelated domain. Proposals retain source task,
artifact, message, review, confidence, and timestamps. Duplicate facts are
reused rather than duplicated, while contradictory facts are surfaced for
inspection/editing instead of silently replacing knowledge. Applying is an
explicit append to `domains/<domain-id>/knowledge.md`; only that knowledge file
is changed, never `guardrails.md`, and core Helm safety rules remain higher
priority than learned material.

## Optional Helm CLI

The CLI is an optional tool for initialization, inspection, and automation. It
is not the conversational entry point. Use `helm init` only when explicitly
initializing a root; it creates missing Helm directories without overwriting
existing projects. For a repository-native request, do not ask the user to run
any of these commands.

```sh
# Optional: install/use the CLI while developing or automating Helm.
python3 -m pip install --editable .
helm --help

# Optional: initialize a new root, only as an initialization operation.
helm init <helm-root>

# Optional: inspect state or trigger automation after a root exists.
helm --root <helm-root> status
helm --root <helm-root> project list
helm --root <helm-root> inspect <task-id>
```

The idempotent [`scripts/setup.sh`](scripts/setup.sh) checks Python and prints
manual steps by default. `--install` and `--init --root PATH` are explicit
opt-ins; setup never installs software or changes projects by default. A native
agent does not need to run this script.

### Running the tests

The suite lives in `tests/`, split by subsystem — task lifecycle, runtime
selection, worker protocol, the worker lifecycle state machine, approvals,
delivery, review, domains and skills, Herdr, CLI surfaces, and the repository
contract — over the shared fixtures in
[`tests/support.py`](tests/support.py). Run the whole thing with discovery, and
a single subsystem by naming its module:

```sh
# The canonical full run.
NO_COLOR=1 python3 -m unittest discover -s tests -p 'test_*.py'

# One subsystem while working on it.
NO_COLOR=1 python3 -m unittest tests.test_approvals
```

`NO_COLOR=1` keeps assertions about rendered output independent of the
terminal. Every module is self-contained: it passes on its own and does not
depend on the order discovery happens to pick.

### Optional CLI worker automation

`helm run` and `helm worker launch` are automation interfaces for an external
worker process. A CLI caller may provide a shell-free `--command`, a configured
profile, or the advanced `HELM_WORKER_COMMAND` override. The CLI cannot infer
an arbitrary external executable, so these settings remain documented only as
advanced overrides:

```sh
helm --root <helm-root> run <project-id> "Prepare the next artifact" --command 'agent-binary'
# Or inspect optional configured profiles without allocating a task:
helm --root <helm-root> agent list
helm --root <helm-root> agent check
# With HELM_ROOT already set, the short forms are also available:
helm agent check
```

Neither `agents.json` nor `HELM_WORKER_COMMAND` is required for the primary
repository-native workflow. Profile files are validated against actual
executables and capacity; a profile file alone does not make a runtime
available. The root `agents.json`, `.helm/agents.json`, and
`agents/<id>/profile.json` layouts are optional advanced inputs. See
[`helm/cli.py`](helm/cli.py) for the complete command surface and
[`docs/agent-adapters.md`](docs/agent-adapters.md) before adding a provider
adapter.

## Automatic bounded domain context

When the CLI launches a worker, Helm composes one private context document in
this strict order:

1. immutable Helm core safety rules;
2. `domains/<domain-id>/knowledge.md`;
3. `domains/<domain-id>/guardrails.md`;
4. `projects/<project-id>/.helm/knowledge.md`;
5. the current task and assigned worktree.

The coordinator composes the same ordered context for every spawned worker, and
that document carries exactly one project. Each source has a path, an authority
boundary, and an `exists` marker.

**Domain knowledge attaches by itself.** The commander never names a domain,
and after the first task on a project neither does anything else: Helm records
the first domain actually resolved for a project as that project's default, and
every later task inherits it automatically.

```sh
helm project domain <project-id> software-delivery   # set or change it
helm project domain <project-id>                     # clear it
```

The default lives in Helm's own state, which outranks the project's
`.helm/project.json`, so it never writes to the project's repository. An
explicit `--domain` on a task that differs is a one-off — it does not rewrite
the project's default.

What Helm will **not** do is guess. It never infers a domain from the words in
a brief: “script” once routed a video script to the software domain, and one
Hot Story video resolved to five software domains at once. It does not infer
from the shape of the repository either, which would do the same to a video
project that happens to contain a Python file. The evidence for a default is a
judgement already made on that project by something that read the task and the
domain catalogue (`helm domain list`, where each domain declares `applies_to`,
`use_when`, and `not_for`). One prior decision, reused — not a match on
coincidence.

The remaining escape hatch, `--no-domain`, ships a worker with core safety
rules only — no code review, verification, or definition of done — and it
teaches the project nothing.

### Reusing knowledge across projects

A task resolves exactly one domain, so shared practice would otherwise have to
be restated in every domain that needs it. Instead a domain declares its bases,
and Helm loads the whole chain automatically — bases first, the selected domain
last, so the most specific guidance is read last:

```sh
mkdir -p domains/backend
cat > domains/backend/domain.json <<'EOF'
{"extends": ["software-delivery"]}
EOF
```

Any task resolving `backend` now inherits `software-delivery`'s knowledge and
guardrails without the project restating them. Composition is depth-limited,
rejects a cycle, and rejects a base that does not exist, so a broken chain fails
loudly instead of silently dropping guidance. The composed context reports the
resolved order as `domain_chain`.

`domains/software-delivery/` ships with the repository as a general base:
lifecycle (requirements, sizing, traceability, circuit breakers, definition of
done), the author/reviewer/verifier roles, and multi-agent coordination. Domain and project files
cannot authorize merges, publishing, credentials, destructive actions, or
scope expansion. Core rules always outrank them. An ambiguous domain mapping
must be resolved explicitly rather than guessed. After an approved learning is
applied, its `Approved learning` block in `knowledge.md` includes proposal,
task, artifact/message, confidence, and approval provenance; guardrails are
never written by the learning workflow.

For an optional domain pack:

```sh
mkdir -p domains/publishing
cat > domains/publishing/knowledge.md <<'EOF'
# Publishing domain
Keep recommendations accurate, audience-safe, and suitable for the requested format.
EOF
cat > domains/publishing/guardrails.md <<'EOF'
Do not invent analytics, claim unpublished facts, or publish anything without approval.
EOF
```

The native agent may read and apply existing domain files; creating or changing
policy/domain files is itself a scoped change requiring the same review
boundary.

### Task-varying skills

Domain knowledge is durable and shared: it attaches to every task that
resolves a domain. A great deal of what a worker needs is not like that — how
this repository runs its screenshot harness, the shape of its migrations, its
release checklist. That material varies per task, belongs to the repository it
describes, and would be wrong to promote into a shared domain pack.

Repositories already carry it as **skills**: a directory of `SKILL.md`
manifests, each declaring a name and a description of when it applies. Helm
reads them from the project it is working on — never from Helm itself, and
never from another project:

| Root | Read for |
| --- | --- |
| `.agents/skills/<id>/SKILL.md` | every runtime |
| `.claude/skills/<id>/SKILL.md` | the runtime that owns it |

```sh
helm skills <project-id>                      # what this project declares
helm skills <project-id> --agent claude       # including that runtime's own root
helm skills <project-id> --brief "add a migration"   # what a brief would select, and why
```

Selection is deliberately dull: a skill earns its place when its own declared
description overlaps the brief, and a driver that wants an exact set pins it
in the project's own file. A denylist outranks a pin, because it is the
standing decision that a skill must not be used here.

```jsonc
// projects/api/.helm/project.json
{"skills": {"pin": ["house-style"], "deny": ["legacy-deploy"]}}
```

What was selected, what was skipped, and anything that could not be read are
recorded on the task, so `helm inspect` answers "what was this worker actually
given" without re-deriving it. A missing, malformed, symlinked, or
description-less skill is **reported, never guessed at** — a worker is told to
raise it rather than improvise an equivalent.

Content is bounded, and the runtime's own loading is respected: a skill the
runtime already loads from its own directory is named rather than pasted in,
because two copies of one instruction in a context window is waste until the
moment they disagree. Anything the runtime cannot see is provided in full,
trimmed to a limit that is stated rather than applied silently.

A skill is guidance a worker reads, never authority it can invoke. In the
composed context it sits after project knowledge and before the task, and it
cannot authorize a protected action, widen the brief, override core safety, or
reach outside its project. Helm ships no skills of its own and never installs,
enables, or writes one. See [`docs/skills.md`](docs/skills.md).

### Deciding when a change needs a spec first

Some changes should have their behavior agreed in writing before anyone codes
them, and most should not. That judgement is knowledge, so it ships as the
`spec-driven-development` domain rather than as a Helm feature: **Helm has no
spec command, no spec state, and no spec gate.** Nothing in a task's lifecycle
changes, and no task waits on a human because of it.

The domain is composed into the three places the decision is acted on, so it
arrives without any project asking for it:

| Composed into | Reaches | What it does there |
| --- | --- | --- |
| `driving-delegated-work` | a project's foreman | decide at brief time, before a coder starts |
| `software-delivery` | the author of a change | write the document, implement against it |
| `code-review` | the independent reviewer | read the behavior against the contract |

The rubric asks for a spec when the behavior is ambiguous, when the change
alters a contract other components depend on, on auth and security boundaries,
where data loss is possible, for billing or publishing, for user-facing
workflows, when review keeps relitigating the same tradeoff, or when the work
already needs multiple rounds. It says to skip it for narrow, well-understood,
low-risk mechanical changes — spec-gating a typo trains everyone to skim the
spec that mattered.

**No behavior change outranks every trigger above.** The rubric asks what the
change does, not which directory it lands in: a typo in publishing copy or a
mechanical rename in billing code is not specced because the area matched a
keyword. The one reversal is doubt — a "rename" that moves a serialized name,
a public symbol, or a config key is a contract change, and it gets the spec.

The foreman decides and **writes the verdict, its one-line reason, and any
convention or path the coder needs into the task brief**, as well as recording
it in progress reporting. The brief is the part that matters for the handoff: a
worker's context is its brief plus composed domain and project knowledge, and
the project's progress record is not in it, so a decision kept only there never
reaches the coder. It is a coordination call either way: not a commander
approval, and not a new task status.

**The spec follows the managed project's conventions, never Helm's.** The
worker reads the repository's own files first, and a repository that already
has a spec convention keeps it — OpenSpec, Spec Kit, and BMAD are named in the
domain purely as examples a driver should recognize, and Helm depends on none
of them. Where no convention exists, the worker writes a short plain document
in the repository's existing documentation location, covering problem, desired
behavior, non-goals, acceptance criteria, verification, open questions and
action items, and follow-ups created.

Installing, initializing, or scaffolding a framework is a scope decision, so
the guardrails rule it out **as a step in doing something else**. It stays
possible when adopting one is itself the brief — explicitly scoped as its own
task, with whatever human authority its protected parts need already obtained.
Never as a side effect.

A repository with nowhere obvious to put the document does not get one invented
for it. The worker infers a home from the repository's own naming and
contributing norms where it can; where it cannot, it writes a clearly
task-local file in the task worktree, reports it with `--type artifact --path`,
and says the location is temporary. Proposing a permanent convention is a
recorded follow-up, not a side effect of the change that noticed the gap.

A temporary file has an end, and it falls before approval. Keep it through
review — it is what the findings refer to — then capture its decisions, closed
questions, and follow-ups in the task result and the project record, and delete
it so the worktree is clean. A task worktree must be clean to be approved,
untracked files included, and that check is not the thing to loosen. If the
document turns out to be worth keeping, it was never temporary: commit it into
the repository as part of the change instead.

The document lives on the task branch in the task worktree, and is part of the
change where the repository keeps that kind of document. A spec change, a
blocking open question, or an unmeetable acceptance criterion is reported as an
intermediate outcome to the foreman and to Helm rather than saved for the final
result. Resolution is always stated: nothing reads a document's prose to
conclude that its open questions have been settled.

**The reviewer is told what the author produced, structurally.** A reviewer
reads a diff, so anything the author did not commit is invisible to it — which
is exactly the case for a spec written where the repository keeps no such file.
Relying on the foreman to mention the path works until it forgets, so the
reviewer brief Helm generates now lists the author task's recorded artifact
paths and descriptions. This is generic to artifacts rather than special-cased
for specs: every artifact message already validated its path against the
author's workspace and stored it workspace-relative, so the handoff adds no new
state and no new trust, and a task that reported none gets no paragraph.

### A fresh, verified base before every new task worktree

A task worktree inherits whatever the base was at the moment it was cut, so
that moment has to happen before the task exists, not be checked afterward.
`domains/branch-isolation/` carries the procedure — resolve the project's
*configured* base branch (never a hardcoded or inferred name), and when it
has an upstream, fetch it and verify the fetch **succeeded**. A fetch that
succeeds and moves nothing is still a fresh, verified base; only a failed
fetch blocks, and it must never fall back to a cached ref. A branch with no
upstream configured but a remote that has a same-named branch is not treated
as local: Helm still fetches that one unambiguous match rather than trusting
an unverified local tip, and blocks if none or more than one remote has a
matching name. A local branch that is ahead of or diverged from its freshly
fetched upstream also blocks, so a task is never built on unmerged local
commits mixed in without review; equal-or-behind uses the fetched upstream
tip. Block the same way on an uncommitted change to a tracked file, or an
unresolved merge/rebase/cherry-pick, in the project's own checkout — an
untracked file (an uncommitted `.helm/project.json`, a build artifact) does
not block, since it changes nothing about what the base branch resolves to.
Record the exact verified commit the task worktree/branch is cut from.

| Composed into | Reaches | What it does there |
| --- | --- | --- |
| `branch-isolation` | every worktree-backed task | the procedure itself |
| `driving-delegated-work` | a project's foreman | before `helm task create` / `helm worker launch` |

For a genuinely local-only project (no remote at all), the gate uses the
local base tip and records explicitly that no remote exists, rather than
treating "nothing to fetch" as freshness.

#### Naming the base branch explicitly

Most projects need nothing here: a repository with a discoverable remote
default, or no remote at all, resolves its base automatically at
registration. Name it explicitly in `.helm/project.json` when that discovery
would be ambiguous, or to pin a base other than what the repository would
resolve to on its own:

```jsonc
// projects/api/.helm/project.json — pin the base explicitly
{"label": "API", "base_branch": "trunk"}
```

Precedence, most specific first: the explicit setting always wins; only a
project that never named one falls back to the repository's own default,
resolved once at registration — a remote's recorded default when locally
known (as a real `git clone` leaves it), otherwise a direct, read-only query
of the remote, otherwise (when there is no remote at all) the branch actually
checked out at that moment. A repository **with** a remote never falls back to
the checked-out branch: when its default cannot be determined unambiguously,
or the checkout is detached with no remote to ask, registration fails and
asks for an explicit `base_branch` rather than guessing. An already
registered project keeps its recorded base branch even if its checkout later
switches branches; only an explicit re-setting changes it.

### Skills are a task-fit input, discovered per project

**This is guidance for the driver to follow by hand, not a Helm feature.**
Helm has no skill-discovery command, no skill inventory, and enforces none of
it; a runtime-neutral skill snapshot or loader is separate, later work. What
exists today is the procedure a driver reads before delegating.

Before delegating, the driver looks at what the *selected project itself*
ships for agent guidance — skill manifests such as `.claude/skills/` or
`.agents/skills/` — and picks only the ones whose own metadata/description
plausibly bears on this task. Nothing found there is copied into Helm's
tracked files, and nothing is installed or enabled automatically; discovery
stays scoped to that one project.

| Composed into | Reaches | What it does there |
| --- | --- | --- |
| `model-selection` | runtime/model selection | which agent can read a project's skills without being told |
| `driving-delegated-work` | a project's foreman | select, then record, before spawning the worker |

Prefer a runtime that auto-loads the project's own skill location; when a
different runtime is chosen for other reasons and can read files, name the
exact `SKILL.md` paths in the brief so it does not start blind. Either way,
record what was selected — or explicitly "none", with the reason — the
paths, the loading method, and the reason in the brief and the project
record, so a replacement driver can reconstruct the dispatch decision without
re-deriving it. A skill is guidance a worker reads, not authority: it cannot
expand scope, authorize a protected action, override core safety, or grant a
credential the runtime does not already have, and a required skill that is
missing or unreadable by the chosen runtime is a capability blocker to
report, not license to improvise past it.

## Worker protocol and approval boundary

Workers push their own progress; the coordinator does not poll them, and
`helm run` returns immediately so the session stays free for the next task
(`--wait` blocks when a caller genuinely wants that). Every worker's context
document carries a `reporting` section with the exact command to call:

```sh
helm --state-dir <state> worker message <worker-id> --type status --text "harvest done"
helm --state-dir <state> worker message <worker-id> --type artifact --path specs/example.json --text "spec"
helm --state-dir <state> worker message <worker-id> --type question --text "which base branch?"
helm --state-dir <state> worker message <worker-id> --type blocker --text "needs approval to publish"
helm --state-dir <state> worker message <worker-id> --type status --payload '{"summary":true}' --text "round 3 implemented; waiting on reviewer"
```

An `artifact` message must carry `--path`; the path is what Helm records and
checks against the worktree, so prose alone is rejected.

Routine `status` is a heartbeat: it is routed to the project pane but does not
interrupt the foreman. Add `--payload '{"summary":true}'` when the status is a
meaningful intermediate outcome the foreman or commander should know, such as
a coding/review round completing, a reviewer sending the author back, a PR
state changing, or a delivery gate opening. A foreman's own summary status is
recorded into `helm project status` as a commander-facing progress line; a
worker summary is recorded there too and also wakes the foreman.

A worker **asks instead of guessing or stopping**. Helm answers from the task
goal on the user's behalf and sends the reply into the worker's own session:

```sh
helm --state-dir <state> worker answer <worker-id> --text "branch off main"
```

Delivery is Escape, then the text, then `Enter`, with a pause between each.
`send-text` alone only fills the input buffer, an `Enter` sent immediately
races the paste and submits a fragment of it, and a paste that arrives while
the agent is mid-execution is treated as an interruption -- after which the
next text lands in a buffer that never submits, so the answer looks delivered
and the worker waits forever. `blocker` stays reserved for what
genuinely needs a human: approval, credentials, a decision outside the brief, or
a contradiction no source resolves.

**Confirmations go to Helm, and Helm decides.** Agent CLIs habitually pause to
ask whether to proceed, which option to take, or whether a change is
acceptable. In a Helm pane nobody is reading that prompt, so waiting on it is a
silent stall rather than a safe pause. Every worker is told to push those as
`question` messages, state what it will do if the answer is yes, and carry on
with whatever the answer does not block. Protected actions are the exception:
merge, publish, push, deletion, other destructive or external actions, and
missing credentials still reach a human, and Helm cannot grant them.

Each push is recorded and routed to the project's Herdr pane as it happens.
Stdout only reaches Helm when the worker exits, so a long task that reports
nothing until then is indistinguishable from one that died.

### Nobody watches the panes

Delegation is only real if a human does not have to check each agent's UI, so
Helm measures silence itself:

```sh
helm watch            # every running worker's health; exit 1 if any need attention
helm watch --nudge    # also ask each silent worker, once, for a status push
```

A worker is `healthy` while it reports, `reported` once it has delivered a
terminal message and merely left its session open, `stalled` when it has
produced neither a protocol message nor any terminal output for the silence
threshold, and `finished` when its process exited without Helm noticing --
which `watch` settles automatically so a task cannot sit in `running` forever
because nobody looked. `helm status` prints the same attention list.

Repair stops at the unambiguous. A finished worker is settled; a stalled one is
reported and nudged once, never silently failed, because its pane is the
evidence needed to diagnose it.

How a worker's own `result`, `blocker` or `failure` combines with what the
operating system observed — which wins, in which order, and what is kept as
evidence when they disagree — is one state machine with a normative
specification: [`docs/worker-lifecycle.md`](docs/worker-lifecycle.md). Read it
before changing `poll_worker`, `_ingest_worker_event` or `_apply_process_exit`.

Abandoning a task is a decision, so it has a command:

```sh
helm worker stop WORKER_ID --reason "..."
```

It signals a process worker, closes a Herdr worker's pane, and settles the
record either way — including when the provider cannot be reached, because a
stop nobody can record is the state this exists to make impossible. Without it
the only exits were a worker finishing on its own or a human closing a pane by
hand, which leaves the record saying `running` forever with nothing able to
correct it; anything keyed on a live worker is then wrong permanently. The log
and worktree are kept as evidence — `helm task cleanup` removes those
deliberately, afterwards.

### A foreman drives one project's loops

```sh
helm foreman PROJECT   # one project, one foreman; a second is refused
```

`watch` tells you a worker is stalled; somebody still has to answer it. That
somebody does not have to be the coordinator. A foreman is an agent started
with the project's status record as its brief, and it owns the loops inside
that project: turning a goal into a delegated task, launching the worker that
does it, answering that worker's questions, running `helm review` so an
independent agent cross-checks the change, and reporting the outcome upward.

**Every project gets one automatically.** Any command that starts work —
`helm run`, `helm worker launch`, `helm herdr launch` — appoints the project's
foreman first if it has none, so this does not depend on a coordinator
remembering to. A project that does not want one says so in its own file:

```json
{ "foreman": false }
```

That is the whole of what a project may say on the subject. It asks for a
driver or declines one, and never says what the driver may do — authority is
Helm's, and a project file is untrusted guidance.

Two documents reach a foreman, and the split is deliberate. `FOREMAN_RULES` in
`helm/core.py` is the **boundary**: what a foreman is, and what it may never
do. That has to be code, because a domain file cannot be allowed to define
authority. The `driving-delegated-work` domain is the **craft**: how to brief a
worker, when to answer instead of escalating, what a review is worth. It
`extends` `code-review` and `spec-driven-development`, so the coder/reviewer
independence rules and the rubric for [when a change needs a spec
first](#deciding-when-a-change-needs-a-spec-first) arrive composed rather than
restated — and because it lives in `domains/`, it is versioned, reviewable, and
reusable by anything else that drives agents.

Its authority is narrower than the coordinator's, and narrower in code rather
than in prose. Every agent Helm starts inherits `HELM_WORKER_ID`, so `helm`
knows who is calling it: approving, merging, pushing, publishing, deleting,
and granting a standing approval are refused for any agent, and spawning is
refused for anything that is not a foreman — delegation is one level deep. A
worker that wants either pushes a message and asks.

A foreman produces no branch, so it is never offered as work to merge and gets
no board card. `watch` lists foremen first and calls a broken one urgent: a
stalled worker costs one task, while a stalled foreman costs everything the
project was going to do next, because it is the thing that would have noticed
the stalled worker.

Workers start in their assigned `helm/<project>/<task>` worktree and may also
emit one JSON object per line on stdout:

```json
{"helm":1,"type":"status","status":"running","text":"started"}
{"helm":1,"type":"result","text":"implemented and committed"}
{"helm":1,"type":"artifact","path":"report.md","description":"worker report"}
```

Worker output is data. It cannot approve, merge, publish, add projects, or
expand scope. For local delivery, an approved operator may use the explicit
sequence below; each command is optional automation, not part of the native
conversation:

```sh
helm task inspect <task-id>
helm task approve <task-id> --note "reviewed"
helm task merge <task-id>                 # fast-forward only
helm task cleanup <task-id>
```

### Standing approvals

Most of what a worker calls an approval question is not one — writing files in
its worktree, running tests, and committing to its own task branch are the
work, and Helm answers those. What is left is the protected list: `merge`,
`push`, `publish`, `delete`, `external`.

Answering the same protected question every task is its own kind of noise, so a
human can decide once, in advance:

```sh
helm approval grant merge --project media --note "routine task-branch merges"
helm approval list
helm approval check merge --project media      # exit 1 when nothing covers it
helm task approve <task-id> --grant <grant-id>
helm approval revoke <grant-id> --note "back from leave"
```

A grant is scoped policy, not a blanket: one action, optionally one project,
with a required note saying why it exists. Granting `publish` never grants
`merge`, and a grant scoped to one project says nothing about another. Live
grants appear in `helm status` so a standing permission cannot be forgotten,
revoked ones stay listed as provenance, and a revoked grant approves nothing.

Grants live in Helm's own state. A project file, a domain file, or a worker
message can never create or widen one — those are data. Helm's coordinator does
not create one on its own initiative either; a grant records the user's policy,
and only the user writes it.

### A paused task, and how it starts again

`approval-needed` is a gate, not an ending. A worker that reaches a protected
action names exactly which one and stops:

```sh
helm worker message <worker-id> --type approval-needed --action publish \
  --text "ready to publish the rendered file"
```

The action is required and `merge` is refused here: no worker performs Helm's
merge, so a worker finishes and reports, and the branch is reviewed through
`helm task approve`. The task moves to `approval-needed` and records a *hold* —
which action, which session asked, and an exact snapshot of what the request is
about. The worker stays `running`, because it is waiting rather than finished:
it can still be answered, it can still report, `helm watch` shows it as
`awaiting-approval`, and its pane and the project's space are kept because a
human still has to look. The request is written to the project's own record and
raised as a commander action item, on both intake paths.

What the snapshot binds, by content: the revision and tree, the index, the whole
tracked diff against HEAD (staged and unstaged, binary included), every
untracked path by digest, every artifact the task declared by id/path/digest,
and everything under the project's declared delivery directories — which is
where ignored build outputs live. Workspace identity is verified first, and
anything unreadable is a refusal, not an empty binding.

Then the decision, and the two steps that follow it:

```sh
helm approval release <task-id> --action publish --confirm      # or --grant <id>
helm worker action-start <worker-id>                            # the worker runs this
```

`release` records the authorization against the snapshot the request was made
for. It does not resume anything: if the work moved between the request and the
decision, it refuses and nothing is authorized. If it moved after, `action-start`
refuses. The task stays paused at `approval-needed` until the worker itself runs
`action-start`, which is both the acknowledgement that the go-ahead arrived in
that live session and the one-use gate immediately before the side effect. Only
then does the task go to `running`. The worker acts, reports its `result` with
any receipt in `--payload`, and those receipts are recorded as outcome data —
never compared against the pre-action snapshot, so a publish that writes its own
receipt does not invalidate the approval it just used. The outcome reaches the
project's record and the commander's action item is closed.

Delivery is a separate fact from the decision. If the authorization cannot be
delivered — no Herdr, a pane the provider says is gone — `helm approval release`
reports `NOT delivered` and exits non-zero, the hold stays
`authorized-pending-delivery`, the task stays paused, and the escalation stays
open. Running the same command again is a delivery retry, not a second
authorization: the same ticket, no new decision recorded.

**Same-session resume needs an interactive session.** A worker started by the
plain process launcher runs in print mode with no input channel, so nothing can
hand it a go-ahead; `helm approval release` refuses such a worker outright
rather than spending an authorization nobody can receive. That is a limitation
of that mode, not a presentation difference. A task stranded that way — and a
task carrying an approval request from an older Helm, whose worker was recorded
as failed — is recovered with:

```sh
helm approval repair <task-id>
```

Repair is evidence-led. It reconstructs a waiting hold and revives that same
worker only when the provider says its session is live and the recorded request
names an unambiguous action; it asks a live worker to restate an unusable
request rather than inventing one; and when the session is gone it abandons the
hold and marks the task `failed`, so it can be cleaned up or retried instead of
sitting in permanent `approval-needed` residue.

Authorization is enforced in core, not in command dispatch. `helm approval
release`, `repair`, `task approve`, `task merge`, `task pr`, `approval
grant/revoke` and the learning approvals all require an authority object that
only the root can obtain, so an agent gains nothing by importing `Coordinator`
directly. The caller is identified from evidence it does not own: the marker
every agent inherits, plus process lineage — clearing `HELM_WORKER_ID` no longer
makes a worker's own command look like the root. Optionally, bind those commands
to a capability this root holds:

```sh
helm authority init      # writes the secret 0600 and never prints it
helm authority status
```

With one configured, a protected command also requires `HELM_AUTHORITY` to match
it. No agent Helm starts can inherit it: the worker environment is an allowlist,
and the value is never written into a context document, prompt, or log. Without
one, the session-role boundary is all there is, and each approval record says
which of the two actually verified the human (`authority.mode`).

### Delivery lifecycle

A worker `result` is a milestone, not the end of the task. The outcome is kept
in durable project surfaces -- the task worktree, branch, PR, delivered
artifacts, project status, messages and logs -- rather than by keeping an agent
session alive as storage.

Every terminal report is written into the project's status record as it
arrives, so a final summary flows worker → foreman → Helm and survives the
pane, the session and the conversation. What happens next depends on whether
anything is still driving the work. While the project's foreman is live, the
result is pushed to it and nobody is asked to merge anything. Once no driver
is left -- the foreman reported, stood down, or the project declined one --
Helm records a **delivery decision** as a commander-visible action item on that
project: read the outcome, then choose review, another round, local merge, PR
delivery, or cleanup.

```sh
helm status                 # "Waiting on you": every open decision, project-labelled
helm project status <id>    # the same items marked [decision], with the task they name
helm watch                  # repeats DECISION REQUIRED until somebody answers it
```

Recording the outcome is not the same as delivering it. A worker reports by
running a Helm command *inside its own pane*, so the confirmation is printed
onto the surface that releasing the tab is about to remove. Helm therefore
routes the final summary and the decision before any of that runs — to the
project's live foreman, to the project's own overview pane, and to the durable
status record — and records which of those accepted it. There is no
live-foreman precondition: a project with no driver is the case that most needs
telling. A finished tab whose outcome reached nothing at all is not released,
because that pane is then the only copy.

It is deduplicated, so the several paths that can raise it -- a worker result,
a foreman's final report, a foreman standing down -- produce one item. It names
the single unresolved task when there is one and stays project-scoped when
there are several. And it closes itself once the decision has been taken: the
task reaching `merged` or `pr-merged`, being continued with `helm task
continue`, or being cleaned up. A free-text follow-up recorded with `helm
project action` is never auto-closed -- Helm knows when a delivery decision was
taken and cannot know whether somebody's caveat was dealt with.

**Delivery is not finalization.** A task that reaches `merged` or `pr-merged`
still owns its task worktree, its `helm/<project>/<task>` branch, and its
worker directories, and the delivery decision closes the moment the merge
lands -- so the commander saw nothing outstanding while the disk still held
everything. Helm therefore raises a second gate, also shown as `[decision]`
and repeated by `helm watch`, naming exactly what that task still holds and
the safe command that sheds it:

```sh
helm task cleanup <task>                   # worktree, worker directories, merged branch
helm task cleanup <task> --delete-branch   # also discard a branch with unmerged commits
helm project release <id>                  # the same, task by task, reporting what it kept
```

What it names is read from Helm's own record of what this task owns, not from
a live look at the disk: `helm status` and `helm watch` run no git or
filesystem probe for it, and a project root that has moved or gone unreadable
cannot be mistaken for "the branch is already gone". A resource is held until
Helm records letting go of it, and `helm task cleanup` is the only thing that
does -- including for resources removed outside Helm, which it reconciles as
removed on the way past.

Like the delivery decision it is derived rather than flagged, so it is raised
once however many times it is recomputed, it is never raised for a task that
holds nothing, and it resolves itself as soon as the record says the residue
is gone. It resolves only for what cleanup actually shed: a branch kept because it holds
unmerged commits leaves the item open, now naming just the branch. Cleanup
stays explicit -- Helm never runs it for you -- and its refusals are unchanged:
a dirty workspace, a live session, work still awaiting approval, and an
unmerged branch are all preserved and reported. The work is not finalized until
that approved cleanup decision is resolved.

For local delivery, the final delivery state is `merged`: Helm records approval,
fast-forwards the task branch into the project's base branch, copies declared
local artifacts into the main worktree when needed, and can then release the
worker session.

For PR delivery, the final delivery state is `pr-merged`. Helm records the
branch push as an intermediate delivery event, records the PR URL as `pr-open`
when a PR is created or supplied, and keeps that task visible until monitoring
records the PR as merged:

```sh
helm task pr <task-id> --confirm       # push and create a PR when gh is available
helm task pr-status <task-id> --state open --url https://example/pull/1
helm task pr-sync <task-id>            # read comments/checks/state with gh
helm task pr-status <task-id> --state merged --url https://example/pull/1
```

`pr-open` is deliberately not final: checks, review comments and human replies
can still arrive, so the project's single foreman stays responsible for
monitoring it. `pr-merged` is final for PR delivery, just as `merged` is final
for local delivery. Cleanup of the task worktree is a separate explicit
operation after that final state has been recorded.

### Delivering build outputs

A merge moves tracked files only, so a rendered video -- often the actual
product -- stays in the task worktree and dies with it. Delivery copies a
task's outputs into the project:

```sh
helm task deliver <task-id>            # runs automatically after a merge too
helm task deliver <task-id> --force    # replace a differing project copy
```

It copies what the worker declared with `--type artifact --path`, plus
anything under the directories a project names in `.helm/project.json`:

```jsonc
{"label": "YT", "deliver": ["renders", "clips"]}
```

That second list matters because a worker that forgets to report a render
would otherwise lose it. Copying never escapes the worktree or the project
root, an identical file is a no-op, and a differing file is reported and
skipped rather than replaced -- the copy already in the project may be the
human's own cut.

Approval records the terminal worker, branch tip, and tree hash — identically
whether a person approved in the moment or a standing grant did, with the
grant's id recorded as the authority. Any later
mutation invalidates approval and requires review. Cleanup refuses live
workers and dirty or unresolved workspaces. PR delivery still requires the
explicit protected push/PR command; monitoring records observations, it does
not approve or merge on its own.

## Herdr worker spaces

Herdr is the default place a delegated worker runs, but it is never required
for delegation itself or for the safe process fallback. Inside a verified
Herdr-managed environment the adapter presents **one workspace per project**,
holding one tab per worker plus the overview pane that project's routed
messages print into. There is no separate coordinator workspace; a legacy one
recorded by an older version can be closed with `helm herdr cleanup-coordinator`.
Helm persists opaque IDs, reuses a recorded space instead of creating a second
one, verifies the space still exists before reusing it, and closes only
resources it created; it never adopts or lifecycle-manages user resources.
Spaces are created unfocused and Helm never issues a focus call, so starting a
worker never switches what you are looking at. Labels are display only -- Helm
identifies every resource by opaque ID and never looks one up by label -- and a
Herdr panel shows only the first few characters, so they are deliberately
short and front-loaded: a workspace is the project's glyph and ID, and a tab is
a slug of its task plus four characters of the worker ID. `helm herdr relabel`
applies the scheme to spaces and tabs that already exist. Lines routed into a
project's pane are plain text carrying the project's name and ID: escape codes
do not survive `pane run`, so per-project colour is delivered in the Helm
session's own output rather than in panes. A project's space is closed
automatically once its work is finished and reported — nothing running and
every task either delivered (`merged` or `pr-merged`) or cleaned up. `helm
watch` also sweeps recorded project spaces that have no remaining worker tabs,
so a space left open after an earlier cleanup does not linger forever. `helm
worker stop` checks the same release gate after closing a worker pane. A
completed task keeps its project's space whether or not it still has a worker
tab: releasing that tab is the first thing a clean result does, so a missing
pane says nothing about whether the change was delivered. Failed and blocked
tasks keep their space while the pane holds the diagnosis, approval-needed
tasks keep theirs because a human still has to look, a foreman's own task
never holds one -- it produces no branch, so it has nothing to deliver -- and
`HELM_KEEP_SPACES=1` keeps all of them. In a pane the runner gives the
worker a real terminal and mirrors it to both the tab and Helm's log, so an
interactive agent renders its session and stays usable instead of showing a
blank pane. If Herdr is
unavailable the worker is still spawned, using the same core process launcher
and isolation checks. Details belong in the optional
[`docs/agent-adapters.md`](docs/agent-adapters.md) and
[`helm/herdr.py`](helm/herdr.py).

## Full optional command index

```text
helm init [ROOT]
helm run PROJECT [TASK] [--domain DOMAIN] [--agent PROFILE] [--no-herdr] [--async]
helm project add|list|status|note|action|domain
helm agent list|check
helm task create|allocate|inspect|approve|merge|deliver|pr|pr-status|pr-sync|outcome
helm task cleanup TASK_ID [--delete-branch]
helm approval grant|list|check|revoke
helm approval release TASK_ID --action ACTION (--confirm | --grant GRANT_ID) [--note N] [--text T]
helm learning propose|list|inspect|edit|approve|reject|apply
helm worker launch|poll|wait|message|answer|stop
helm herdr launch|poll|wait|relabel|cleanup|cleanup-project|cleanup-coordinator
helm foreman PROJECT [--agent PROFILE] [--command CMD] [--no-herdr]
helm review TASK_ID [--reviewer-agent A] [--reviewer-model M] [--rounds N]
helm skills PROJECT [--agent A] [--brief TEXT]
helm board [--out PATH] [--open]
helm tail WORKER_ID [-n LINES]
helm reflect [--hours N]
helm watch [--silence SECONDS] [--nudge]
helm status [--project PROJECT_ID]
helm inspect TASK_ID
```

Explicit project registration is useful for scripts, but every project must be
an isolated Git repository with a commit. A non-Git directory requires an
explicit `helm project add ... --init-git --confirm`; automatic discovery never
initializes Git. Helm intentionally has no remote knowledge service, general
policy engine, autonomous merge, push, or PR automation.
