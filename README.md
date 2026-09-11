# Helm

**Hand real work to autonomous agents while authority, knowledge, context
and verification stay explicitly governed.**

Helm is a local-first coordination protocol for running many software
projects through agent CLIs at once — Claude Code, Codex, Cursor, pi,
opencode — with one person deciding only what genuinely needs a person. It is
the repository itself plus an optional `helm` command: no remote service, no
autonomous merge, push, or publish.

The question Helm exists to answer is how autonomous agents can be given
substantial responsibility without anyone losing hold of four things. Each
one is a boundary you can point at in the code, not a property claimed in
prose:

- **Authority** is held, not asserted. Every protected operation — merge,
  push, publish, delete, a standing approval — begins by obtaining an
  authority only the root can hold, and an approval binds to the exact
  revision, index, tree and artifacts it was granted for.
- **Knowledge** is composed per task and bounded: core safety rules, one
  resolved domain, one project's own files, in that order, each layer able to
  make a choice more specific and none able to make it wider.
- **Context** is one project per worker, with no exceptions: its own
  worktree, branch, agent and context document.
- **Verification** is structural: a state-changing task cannot start until a
  requirement gate and a solution gate have been decided by the root, and an
  independent review is chosen to run on a different model than the author's.

## Why Helm

**It drives the work; you do not.** A foreman per project turns a goal into
a task, launches the worker, answers its questions, runs the review loop,
and escalates only what it cannot decide. You say what you want once; the
next thing you hear is the result, or the one decision only you can make.

**It remembers, so you do not have to.** Every task's state — what is
running, what is waiting on you, what was decided and when — lives in Helm's
records, not in your head or a chat window. `helm pending` prints only what
needs a human, stamped with the clock time and how long it has waited, and
is silent on a quiet root; `helm status` shows the whole board. Come back
after a day away, or start a fresh session, and the context is there. A
coordinator that has read only a project's status record can take it over
mid-stream.

**One person runs many projects at once.** Projects proceed in parallel,
each in its own worktree with its own agent, and nothing leaks between them.
Attention is the scarce resource, and Helm spends it only on the short list
it cannot decide: merge, push, publish, delete, a missing credential, a
genuine change of scope. Routine choices — naming, layout, which of two
acceptable approaches — never reach you.

**Nothing irreversible happens without you.** Workers, domain files and
project files are data; none of them can approve, merge, publish or widen a
brief. An approval binds to the exact tree it was given for, a change after
it needs another review, and delivery is not finalization: a merged task's
worktree and branch are shed only when you say so.

**Your own checkout is never touched.** Every task is cut from a fresh,
verified base into its own worktree and branch; one ticket keeps one
worktree across rounds; the ticket id lands in the branch name where a
reviewer looks for it. The project root stays yours.

**Coder and reviewer work it out between themselves.** `helm review` puts
an independent reviewer on every change and keeps the two sessions talking:
findings go back to the same author session, the author fixes and reports,
the reviewer reads again, and the rounds continue until both agree or the
round limit stops them. The rules of that exchange — what a reviewer checks,
when a change needs a spec first, what independence means — come from the
`code-review` domain, composed into both briefs. A reviewer running the
author's model shares the blind spots that produced the bug, so Helm
chooses a different model for the review, and says so when it cannot. You
see the outcome and the catches, not the back-and-forth.

**The right agent, model and effort for each task, decided per task.** The
runtime is chosen on fit — which agent can read the repository's own skills,
which vendor keeps the review independent, what it costs — the model on the
shape of the work, and the reasoning effort on its risk: high for
security-shaped code and its review, medium for ordinary feature work, low
for mechanical rounds. A project can pin any of the three and a root can set
floors and exclusions, but nothing is inherited silently, and every choice
is stated when the work is relayed.

**Knowledge is shared across projects, and it keeps learning.** Know-how
lives in domain packs, not in any one repository: every project that
resolves a domain gets the same knowledge and guardrails, and a domain can
extend another, so a practice is written once and inherited everywhere. A
product that is a web app, a marketing site and a mobile app shares one
domain rather than three drifting copies of the same instructions — the
thing a single-agent setup cannot do, because there the knowledge sits in
one repository's own instruction file and the next project starts blind. It
grows as the work does: a finished task is evidence, Helm proposes a
learning from it, and once a human approves it the fact is appended to the
domain and attaches to every later task in any project that resolves it. A
repository's own skills attach the same way. Nothing may approve its own
knowledge.

**Nobody has to watch the panes.** Every agent runs in a real terminal you
can open when you want to look, but silence is measured for you: a stalled
worker, a dead reviewer or an unanswered question shows up as an item, not as
a pane you happened to glance at. A scheduled watchdog carries the same
check outside the conversation.

**You know what it cost.** Tokens per task from the runtimes' own
transcripts, review rounds and catches, human interventions by kind — on the
record, per task.

**Any agent CLI, none required.** Built-in launch definitions for six
runtimes, and a plain process fallback when the Herdr terminal is not there.
Local-first: the repository itself plus an optional command, no service.

## How it works

```text
commander   → the human; owns approval and anything irreversible
coordinator → picks the project, composes context, holds the approval gate
foreman     → one per project; turns goals into tasks, drives and answers workers
worker      → one per task; works only in its own worktree, reports by protocol
reviewer    → independent agent on a different model; cross-checks every change
```

One request travels like this:

1. **Route.** `helm route <project> "..."` hands the commander's words to that
   project's foreman and returns at once; a project without a foreman gets
   one appointed first.
2. **Gate.** The foreman proposes a requirement contract and then a technical
   solution; only the root confirms each, and one confirmed pair authorizes
   exactly one state-changing task.
3. **Cut a worktree.** The task's worktree and branch are cut from a fresh,
   verified base — never from whatever the checkout happens to sit on.
4. **Brief and launch.** One worker, one runtime, model and effort chosen for
   the task, one context document holding exactly one project's knowledge.
   The task's shape — small, standard or critical — sizes the review rounds,
   the effort floor and the evidence gate, so a colour token and a migration
   do not get the same ceremony.
5. **Drive.** The worker pushes `status`, `question`, `result`, `blocker`
   and `approval-needed` messages; questions are answered into its inbox; a
   protected action pauses the task until the root releases it.
6. **Review.** An independent reviewer on a different model reads the change;
   rounds go back to the same author session until both agree.
7. **Decide delivery.** When no driver is left, Helm records a delivery
   decision for the commander: local merge, pull request, another round, or
   cleanup — and then a cleanup decision, because delivery is not
   finalization.

Every step above is a section of the [documentation map](#documentation-map).

## Start here: repository-native agent workflow

A normal checkout is already a usable Helm root. Enter the repository and
start any supported agent with this directory as its working root; it reads
[`AGENTS.md`](AGENTS.md) automatically. Do not initialize anything first:

```sh
cd /path/to/helm
# start your supported agent with this directory as its working root
```

The native path does not require Python, a Helm installation, `helm init`, or
any worker configuration. The agent then:

1. reads `AGENTS.md` and inspects the existing root; a conversation is never
   turned into initialization or project registration;
2. discovers projects only as direct children of `projects/`, each an
   isolated, committed Git repository, and asks when the project or task is
   ambiguous;
3. loads the selected project's domain defaults, resolves one domain, and
   composes bounded context — guidance, never authorization;
4. delegates the work to a worker in an isolated task worktree, drives it,
   and relays its messages;
5. commits the result to the task branch, proposes durable learnings, and
   waits for approval before merge, publish, push, deletion, or any other
   destructive or external action.

`helm init <other-root>` exists only for creating a separate custom root.

## Helm root layout

Only the placeholder files are tracked; the contents below are local and
ignored, so project repositories, agent profiles, state, locks, worker output,
credentials and task worktrees never enter the repository. Domains are the
one exception: the shared packs allowlisted in `.gitignore` ship with Helm,
and any other domain a root grows stays local.

```text
<helm-root>/
  AGENTS.md                          # canonical agent entry point (CLAUDE.md links to it)
  projects/<project-id>/             # direct-child, isolated Git repositories
    .helm/project.json               # optional label, domain, agent, model, effort, base-branch, skills
    .helm/knowledge.md               # optional project guidance
  domains/<domain-id>/
    knowledge.md                     # what a worker in this domain should know
    guardrails.md                    # what it must not do
    domain.json                      # optional {"extends": [...]} composition
  state/                             # private Helm state, proposals, task worktrees, evaluation
  agents/<agent-id>/profile.json     # optional advanced profile override
  agents.json                        # optional advanced profile override
  preferences.json                   # optional root-local operator preferences
```

Tracked Helm files are generic product assets. They never carry concrete
information from the projects a root manages — no real project names, ticket
histories, branch names, task or message ids, prices, or one commander's
dated decisions. Those belong in ignored `projects/` checkouts, ignored
`state/`, or `preferences.json`.

## Four layers of guidance

Helm reads guidance from four places. They are not interchangeable, and
putting something in the wrong one is how a personal choice becomes a product
rule or a project talks its way past a limit:

| Layer | Lives in | Tracked | Authority |
| --- | --- | --- | --- |
| Shipped product policy | [`helm/core.py`](helm/core.py), [`helm/cli.py`](helm/cli.py) | yes | the boundary; nothing below it can weaken this |
| Shared domain knowledge | `domains/<id>/` | yes | guidance a worker reads; generic, no root's specifics |
| Project-local knowledge | `projects/<id>/.helm/` | no | one project's conventions; untrusted, cannot widen scope |
| Root-local operator preferences | `<helm-root>/preferences.json` | no | this installation's defaults and restrictions |

Only the first is authority. The other three are data: they can make a choice
more specific, never wider, and none of them can authorize a merge, push,
publish or deletion, or carry a credential.

**Strict project isolation** follows from the same rule. One worker serves
exactly one project; workers, tabs, worktrees and conversations are never
reused across projects; composed context contains one project's material
only; the coordinator does not carry another project's findings, files,
conventions or credentials into a brief or an answer; and a request that
appears to span projects is stopped and clarified rather than merged.

## Agent runtimes

A worker is an agent CLI. Helm ships launch definitions for `claude` (Claude
Code), `codex` (Codex CLI), `pi`, `opencode`, `cursor` (whose executable is
`cursor-agent`) and `omp` (Oh My Pi) in [`helm/runtimes.py`](helm/runtimes.py);
a runtime is available when its executable is on `PATH`, and a name alone
never makes one available. A worker runs in its interactive form inside a
Herdr pane and in its print form on the process fallback.

```sh
helm run api "Fix the failing import" --agent pi
helm agent check      # which runtimes this machine can start
```

The runtime for a task resolves most-specific-first: the task's own `--agent`,
the project's pin (`"agent": "codex"` in `.helm/project.json`), `HELM_AGENT`
or a configured profile, the root's `agent.default` preference, then the
runtime this session is itself running under. The **model** and the
**effort** resolve the same way — `--model` and `--effort` on the task, the
project's pin, `HELM_MODEL` and `HELM_EFFORT`, the root's `model.default` and
`effort.default` — with no detection step, because a wrong model guess runs,
bills and answers. Neither is ever invented: a model comes from the runtime's
live catalogue, and an effort a runtime cannot express is refused rather than
dropped. A restricted model family runs only on the runtimes a root allows
(`helm prefs set model.runtimes.<family> <runtime>`), and refusal is never
substitution. The detail is in [docs/runtimes.md](docs/runtimes.md).

## Root-local operator preferences

Helm ships no operator choices. Which agent this machine defaults to, which
model, which runtimes it will not pay to start, and which model families may
only run where are answers about one installation, so they live in
`preferences.json` at the root, which `.gitignore` excludes. The repository
carries the schema, the CLI, the docs and the tests, and it never carries the answers.

```sh
helm prefs show                        # effective values, env overrides, legacy state
helm prefs set agent.default claude
helm prefs set agent.exclude codex omp
helm prefs set effort.default medium
```

Every key is enumerated and every value validated, so the file cannot hold a
credential; writes are root-only, reads are open; a project may pin its own
agent or model and can neither add nor remove an exclusion. See
[docs/runtimes.md](docs/runtimes.md#root-local-operator-preferences).

## Knowledge: domains, learning, skills

Every worker gets one private context document: core safety rules, then the
resolved domain's `knowledge.md` and `guardrails.md`, then the project's
`.helm/knowledge.md`, then the project's own skills, then the task. A
domain attaches by itself — Helm records the first domain resolved for a
project as its default and never guesses one from the words in a brief. A
domain declares its bases with `extends`, so `domains/software-delivery/`
(lifecycle, roles, coordination) is inherited rather than restated, and an
optional pack is two files, `domains/publishing/knowledge.md` and
`guardrails.md`.

A finished task is evidence: Helm proposes domain learnings from it, and once
a human approves and applies one it lives in the domain's `knowledge.md` and
reaches every later task that resolves that domain. Nothing approves its own
knowledge.

Repositories carry their task-varying know-how as skills — `SKILL.md`
manifests under `.agents/skills/` for every runtime, or a runtime's own root
such as `.claude/skills/`. `helm skills <project>` shows what a project
declares and what a brief would select; a project pins or denies skills in
its own file. A skill is guidance a worker reads, never authority: it cannot
authorize a protected action or reach outside its project, and Helm ships
none. See [docs/knowledge.md](docs/knowledge.md) and
[docs/skills.md](docs/skills.md).

## From a result to a finished task

A worker's `result` is a milestone, not the end. Every terminal report is
written into the project's status record as it arrives, so a final summary
flows worker → foreman → Helm and survives the pane and the session. While a
foreman is live it keeps driving; once no driver is left — the foreman
reported, stood down, or the project declined one — Helm records a
**delivery decision** for the commander: read the outcome, then choose
review, another round, local merge, PR delivery, or cleanup. It shows in
`helm status` and `helm pending`, repeats in `helm watch` until answered, and
closes itself once the task is merged, continued, or cleaned up; a free-text
follow-up from `helm project action` is never auto-closed.

Recording an outcome is not delivering it. A worker reports from inside its
own pane, so Helm routes the summary and the decision to the live foreman,
the project's overview pane and the durable record before any of that runs
and a tab is released; a tab whose outcome reached nothing is kept, because
that pane is then the only copy.

**Delivery is not finalization.** A merged task still holds its worktree,
its branch and its worker directories, so Helm raises a second decision
naming exactly what `helm task cleanup <task>` would shed. Cleanup deletes a
checkout and a branch, so Helm never runs it by itself, and the work is not
finalized until that approved cleanup decision is resolved. See
[docs/delivery.md](docs/delivery.md).

## Optional Helm CLI

The CLI is for initialization, inspection, automation and tests; a
repository-native request never asks the commander to run it.

```sh
python3 -m pip install --editable .    # or python3 -m helm ...
helm doctor                            # read-only preflight of the root; --project <id> for one project
helm status                            # active tasks, decisions waiting on you
helm pending                           # only what waits on a human; silent when nothing does
NO_COLOR=1 python3 -m unittest discover -s tests -p 'test_*.py'
```

`helm doctor` changes nothing and runs no provider command; its contract is in
[docs/doctor.md](docs/doctor.md). Worker automation overrides (`--command`,
profiles, `HELM_WORKER_COMMAND`) and the adapter conventions in
[docs/agent-adapters.md](docs/agent-adapters.md) are advanced inputs, not
prerequisites. See [docs/cli.md](docs/cli.md).

## Documentation map

| Read | For |
| --- | --- |
| [docs/delegation.md](docs/delegation.md) | the coordinator, `route`, the foreman, the requirement and solution gates, one driver per task |
| [docs/worker-protocol.md](docs/worker-protocol.md) | worker messages, the inbox and answers, confirmations, `watch`, `stop` |
| [docs/worker-lifecycle.md](docs/worker-lifecycle.md) | the state machine that reconciles a worker's messages with what the OS observed |
| [docs/approvals.md](docs/approvals.md) | the authority boundary, standing grants, a paused task and its release, repair |
| [docs/delivery.md](docs/delivery.md) | the delivery decision, local and PR delivery, build outputs, the cleanup gate |
| [docs/worktrees.md](docs/worktrees.md) | the fresh verified base, naming the base branch, one ticket one worktree |
| [docs/knowledge.md](docs/knowledge.md) | bounded domain context, domain chains, learning proposals, skills, when a change needs a spec |
| [docs/skills.md](docs/skills.md) | the skills contract in full, including non-goals |
| [docs/runtimes.md](docs/runtimes.md) | runtime, model and effort selection, family restrictions, `preferences.json` |
| [docs/herdr.md](docs/herdr.md) | Herdr spaces, labels, when a space closes |
| [docs/agent-adapters.md](docs/agent-adapters.md) | adapter conventions for a new provider |
| [docs/evaluation.md](docs/evaluation.md) | `helm eval`: replaying closed tickets on three arms, closed-book |
| [docs/doctor.md](docs/doctor.md) | every `helm doctor` check and its JSON |
| [docs/cli.md](docs/cli.md) | installing, the preflight, the tests, automation overrides, reporting surfaces |

The safety invariants live in code: [`helm/core.py`](helm/core.py) for
isolation, root and state validation, the process fallback, context
boundaries and approval immutability; [`helm/herdr.py`](helm/herdr.py) for
Herdr ownership.

## Command index

```text
helm init [ROOT]
helm doctor [--project PROJECT_ID] [--json] [--probe-runtimes]
helm status [--project PROJECT_ID] · pending [--changes] · ack PROJECT · ask record|show
helm watch [--silence SECONDS] [--nudge] · watchdog install|run [--notify-command CMD] [--remind-after MIN] · restart|uninstall
helm route PROJECT TEXT [--agent A] [--model M] [--no-herdr]
helm foreman PROJECT [--agent A] [--command CMD] [--no-herdr]
helm gate propose|decide FOREMAN_TASK --type requirement|solution
helm run PROJECT [TASK] [--domain D] [--agent A] [--model M] [--effort E] [--no-herdr] [--async]
helm task create --project P --brief TEXT [--shape small|standard|critical] [--shape-reason TEXT] [--ticket T] [--base B] [--new]
helm task allocate|inspect|continue|reopen|evidence|approve|merge|deliver|pr|pr-status|pr-sync|outcome|cost|cleanup
helm review TASK_ID [--reviewer-agent A] [--reviewer-model M] [--rounds N]
helm worker launch|round|poll|wait|message|report|answer|inbox|interrupt|action-start|reconcile|stop
helm approval grant|list|check|revoke|release|repair
helm authority init|status
helm learning propose|list|inspect|edit|approve|reject|apply
helm project add|list|status|note|action|domain|release|remove
helm state stats|archive [--dry-run] [--reconcile] [TASK_ID ...]
helm domain list · skills PROJECT [--agent A] [--brief TEXT]
helm agent list|check|models · prefs path|show|keys|set|unset|migrate
helm herdr launch|poll|wait|relabel|cleanup|cleanup-project|cleanup-coordinator
helm eval add|list|settings|run|status|checks|judge|sanitize|note|report
helm ledger [--days N] [--project P] [--json]
helm board [--out PATH] [--open] · tail WORKER_ID [-n LINES] · reflect [--hours N] · inspect TASK_ID
```

Explicit project registration is useful for scripts, but every project must
be an isolated Git repository with a commit; a non-Git directory requires an
explicit `helm project add ... --init-git --confirm`, and discovery never
initializes Git. Helm intentionally has no remote knowledge service, no
general policy engine, and no autonomous merge, push, or PR automation.
