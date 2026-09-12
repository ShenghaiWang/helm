# The optional Helm CLI

The CLI is a tool for initialization, inspection, automation and testing. It
is not the conversational entry point: a repository-native request never
asks the commander to run any of these commands. The full command index is
in the [README](../README.md#command-index); this page covers setup, the
preflight, the tests, and the automation overrides.

```sh
python3 -m pip install --editable .   # or run every command as python3 -m helm
helm --help
helm init <helm-root>                 # only when creating a separate custom root
helm --root <helm-root> status        # or set HELM_ROOT once
helm project list
helm inspect <task-id>
```

Use `helm init` only when explicitly initializing a root; it creates missing
Helm directories without overwriting existing projects. A normal checkout is
already a root and needs no initialization.

## Adopting a repository: `helm adopt`

```sh
helm adopt ~/code/widgets --delivery pr --domain software-delivery [--id widgets] [--label "Widgets"]
helm adopt projects/widgets --no-review        # already under projects/: adopted in place
```

Clones the repository into `projects/<id>` from its own `origin` when it has
one (otherwise from the path; the original is never moved or touched), writes
`.helm/project.json` with the label, delivery policy, domains and any
`--base-branch`, `--no-foreman`, `--no-review`, `--agent`, `--model` or
`--effort` you passed — only when there is none; an existing file is the
owner's and is read instead — registers the project, then runs `helm doctor
--project <id>` and prints it. The exit code is that preflight's verdict for
the project: `1` when something would stop its first task, such as a domain
it declares that this root does not carry.

## Preflight: `helm doctor`

`helm doctor` inspects a root without changing it — layout and root identity,
the tracked/ignored boundary around local state and preferences, preference
and domain validity, state safety, executable availability for the runtimes
this root *names*, and Herdr readiness. `--project <id>` adds one managed
project's checks: direct-child registration and isolation, a committed Git
repository, its config, its base branch resolved locally, its declared
domains and pinned skills, and any task still holding a worktree, branch, or
worker directory.

```sh
helm doctor
helm doctor --project <project-id> --json
helm doctor --probe-runtimes          # each installed agent's --help, for a renamed flag
```

It is read-only: it never initializes, registers, repairs, cleans, or
fetches, and it opens a read-only store, so it creates no lock file and
changes no permissions. It exits `0` when nothing is an error (warnings do
not change that), `1` when an error was found, and `2` when it could not
run. Runtime readiness there is executable presence — doctor runs no
provider auth, status, or model command, and reads no credential store. The
full contract, including every check id and the JSON schema, is in
[doctor.md](doctor.md).

The idempotent [`scripts/setup.sh`](../scripts/setup.sh) checks Python and
prints manual steps by default; `--install` and `--init --root PATH` are
explicit opt-ins. A native agent does not need to run it.

## Running the tests

The suite lives in `tests/`, split by subsystem — task lifecycle, runtime
selection, worker protocol, the worker lifecycle state machine, approvals,
delivery, review, domains and skills, Herdr, the evaluation harness, CLI
surfaces, and the repository contract — over the shared fixtures in
[`tests/support.py`](../tests/support.py):

```sh
NO_COLOR=1 python3 -m unittest discover -s tests -p 'test_*.py'   # the canonical full run
NO_COLOR=1 python3 -m unittest tests.test_approvals                # one subsystem
```

`NO_COLOR=1` keeps assertions about rendered output independent of the
terminal. Every module passes on its own and does not depend on the order
discovery happens to pick. Check the test count, not just the colour: a
green run with fewer tests than the last one is a file that lost a class.

## Worker automation overrides

`helm run` and `helm worker launch` are automation interfaces for an external
worker process. A CLI caller may provide a shell-free `--command`, a
configured profile, or the advanced `HELM_WORKER_COMMAND` override. The CLI
cannot infer an arbitrary external executable, so these remain advanced
overrides, and none of them is required for the repository-native workflow:

```sh
helm run <project-id> "Prepare the next artifact" --command 'agent-binary'
helm agent list          # configured profiles, without allocating a task
helm agent check         # profile commands and live availability
```

Profile files are validated against actual executables and capacity; a
profile alone does not make a runtime available. The root `agents.json`,
`.helm/agents.json`, and `agents/<id>/profile.json` layouts are optional
advanced inputs. Read [agent-adapters.md](agent-adapters.md) before adding a
provider adapter.

## Reporting surfaces

```sh
helm pending [--changes]      # only what waits on a human; --changes prints what is new since the last call
helm ack <project>            # mark a project's owed reports as relayed
helm ask record --reason authorization|ambiguity|escalation --text "..." [project]   # a question put to the commander, on the record
helm ask show                 # what has been asked
helm watchdog install [--interval SECONDS] [--notify-command CMD] [--remind-after MINUTES]   # the scheduled backstop; run|restart|uninstall
helm board [--open]           # one page showing what every agent produced
helm tail <worker-id>         # a worker's decoded terminal output
helm reflect [--hours N]      # recent evidence for a reflection on how Helm is working
helm task cost <task-id>      # what a task's sessions consumed, from the runtimes' own transcripts
helm ledger [--days N] [--project P] [--json]   # every worker task in the window: time to result, review rounds and catches, asks, tokens, cost
helm state stats              # size of the live state document, counts, what could be archived
helm state archive [--dry-run] [--reconcile] [TASK_ID ...]   # move settled records into state/archive/
helm state tidy [--project P] [--dry-run]   # close the decisions and follow-ups nothing can act on any more
```

`helm pending --changes` in a twenty-second loop is what an agent harness
arms at session start: a quiet root generates no events, and the first gate,
approval request, blocker or terminal report wakes the coordinator within
seconds. The coordinator only exists inside a turn, so nothing reaches the
commander while they are away; `helm watchdog` is the scheduled check that
delivers outside the conversation. It posts a desktop notification when the
pending list changes, says it again after `--remind-after` minutes (60 by
default) while the same list still stands, and runs `--notify-command` on
each — a shell command of the commander's own, with `HELM_TITLE` and
`HELM_MESSAGE` in its environment and the whole list on stdin, which is how a
notification reaches a chat channel or a phone rather than a banner that is
gone in seconds. Once a day it runs `helm learning mine` so recurring
findings become proposals on their own. It also heals: a worker that reads as provably dead on two
checks a minute apart — its process gone with no exit record, or the pane
gone and the worker silent past the threshold, never a worker still in its
startup grace — is stopped so its task can be reopened or retried, a dead
foreman is replaced by one that reads the project record and carries on,
and a project with running workers and no driver gets a foreman appointed.
Stalled or erroring workers are reported and never touched; `--no-heal`
turns the healing off. `helm watchdog restart` makes a running watchdog
pick up new code.
