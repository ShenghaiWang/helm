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
4. Delegate the work to a worker agent spawned in a dedicated Helm-owned Herdr
   space. The coordinator does not do the work itself.
5. The worker works only in the assigned task worktree. If the agent harness
   did not provide one, create a unique task worktree under Helm `state/`
   before editing; never use a project root or another task's worktree as a
   shortcut.
6. After useful work completes, suggest concise durable domain learnings with
   evidence from the task result, artifacts, messages, and review outcome. A
   suggestion is only a persisted proposal; inspect/edit/reject it or wait for
   explicit user/coordinator approval before promotion.
7. Commit the result to the task branch, report the changes and any blockers,
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
    .helm/project.json               # optional label, policy, domain, and agent defaults
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
`extends` `code-review`, so the coder/reviewer independence rules arrive
composed rather than restated — and because it lives in `domains/`, it is
versioned, reviewable, and reusable by anything else that drives agents.

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

### Delivery lifecycle

A worker `result` is a milestone, not the end of the task. The outcome is kept
in durable project surfaces -- the task worktree, branch, PR, delivered
artifacts, project status, messages and logs -- rather than by keeping an agent
session alive as storage.

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
worker stop` checks the same release gate after closing a worker pane. Failed
and blocked tasks keep their space while the pane holds evidence,
approval-needed tasks keep theirs because a human still has to look, and
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
helm learning propose|list|inspect|edit|approve|reject|apply
helm worker launch|poll|wait|message|answer|stop
helm herdr launch|poll|wait|relabel|cleanup|cleanup-project|cleanup-coordinator
helm foreman PROJECT [--agent PROFILE] [--command CMD] [--no-herdr]
helm review TASK_ID [--reviewer-agent A] [--reviewer-model M] [--rounds N]
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
