# Runtimes, models, effort, and root-local preferences

A worker is an agent CLI. This is how Helm decides which one runs a task,
which model it runs, and how hard it thinks — and where one installation's
own answers to those questions live.

## Built-in runtimes

Helm ships launch definitions for `claude` (Claude Code), `codex` (Codex
CLI), `pi`, `opencode`, `cursor` (Cursor CLI, whose executable is
`cursor-agent`), and `omp` (Oh My Pi); each contributes only an executable,
the argv that hands it one prompt, and the credential variables that runtime
reads. See [`helm/runtimes.py`](../helm/runtimes.py) for the table. A runtime
is available when its executable is actually on `PATH` — a name alone never
makes one available, and a known runtime whose executable is missing is
reported unavailable rather than quietly replaced.

Herdr integrations are useful signal, but they are not launch definitions.
`herdr integration status` tells Helm which agents Herdr can recognize and
control once they are running; `helm agent check` includes that inventory
when it can query Herdr safely. A new Herdr integration becomes selectable
for delegated work only when Helm also has a built-in entry or an
`agents.json` profile with a real launch command, environment passthrough,
and any availability check it needs.

A worker is started in its interactive form inside a Herdr pane, where it has
a real terminal, and in its non-interactive print form on the process
fallback, where a full-screen TUI would only write escape noise into the log.

```sh
helm run api "Fix the failing import" --agent pi
helm agent check      # which runtimes this machine can start
helm agent models     # each launchable runtime's own model catalogue, verbatim
```

## Choosing the runtime

The runtime for a task resolves most-specific-first, and anything stated
outranks anything inferred:

1. the task's own choice — "use Codex for this one", or `--agent codex`;
2. the project's pin in `projects/<id>/.helm/project.json`;
3. `HELM_AGENT`, or a configured profile when one exists — an environment
   variable is a deliberate override for this run;
4. `agent.default` in the root-local `preferences.json`;
5. otherwise the runtime this Helm session is itself running under, detected
   from the environment.

```jsonc
// projects/api/.helm/project.json — every worker for this project runs Codex
{"label": "API", "domains": ["backend"], "agent": "codex"}
```

Choosing the agent is the coordinator's call, made per task on fit; detection
is the fallback for when nothing distinguishes the candidates, not a default
that skips the decision. Fitness is mostly about what the repository assumes
its agent can read: a project whose skills live only under one agent's own
directory (`.claude/skills/`) has wired them for that agent, and any other
agent starts blind unless the brief names the exact `SKILL.md` paths. After
that it comes down to model breadth (a reviewer that must not share the
author's model wants a gateway-backed agent), harness shape, and cost. An
agent the root **excludes** is excluded whatever its fit, because that is the
commander's cost decision made in advance. Set `HELM_AGENT=none` to require
an explicitly named agent.

Configured profiles compose with the built-ins: a profile may name a runtime
instead of spelling out a command. A profile's `capacity` is a deliberate
throttle; a built-in runtime has no limit of its own.

```jsonc
// agents.json
{"agents": [
  {"id": "shorts", "runtime": "codex", "domains": ["publishing"], "capacity": 2},
  {"id": "pi", "domains": ["research"]}
]}
```

## Choosing the model, not just the runtime

Naming a runtime does not name the model it runs. "This project runs on
Codex" and "this task is mechanical, run it cheap" are different statements,
and either can be made without the other. The model resolves the same way —
`--model` on the task, then the project's `"model"` pin, then `HELM_MODEL`,
then the root's `model.default` preference — with one difference: there is
deliberately **no detection step**. A wrong runtime guess fails loudly on a
missing executable; a wrong model guess runs, bills, and answers. When
nothing is stated, Helm passes no model and the runtime keeps its own
default.

So decide the model rather than inheriting it, and say the choice and the
reason when relaying, because a model is a cost the commander is paying. A
model is never invented: check the live catalogue near dispatch time (`helm
agent models`, `pi --list-models`, `opencode models`, or `/model` inside an
interactive pane) before naming one, because a plausible-looking model that
does not exist is a failed launch.

```jsonc
// projects/tickets/.helm/project.json — cheap model, because the work is mechanical
{"label": "Tickets", "agent": "claude", "model": "<cheapest-tier-model-id>"}
```

Only a built-in runtime publishes the flag that selects its model. A profile
that supplies its own command does not, so a model aimed at one is **refused
rather than dropped** — silently ignoring it would leave the coordinator
believing it had instructed a model it never sent. Which model suits which
task is knowledge, and lives in the `model-selection` domain, which is
composed into `software-delivery` so a worker gets it automatically.

**An independent review means a different model, not merely a different
process.** A reviewer running the author's model shares the blind spots that
produced the bug. `pi` and `opencode` both reach several vendors behind one
`--model`, which is what makes them the useful reviewers. A runtime the root
excludes takes its whole catalogue with it, whatever the review needed it
for; never route around an exclusion by naming the runtime explicitly.

## Effort is a third choice, owed the same way

Reasoning effort is a cost the commander pays and a quality difference nobody
can see afterwards, so Helm states it or leaves it alone — it never invents
one. It resolves most-specific-first: `--effort` on the task, then the floor
the task's shape implies (`small` low, `critical` high, `standard` none), the
project's `"effort"` pin, `HELM_EFFORT`, then the root's `effort.default`
preference; unset means the runtime's own default.

Effort is not a property runtimes share, so Helm records a mechanism per
runtime rather than assuming a flag: Claude Code takes `--effort` (low,
medium, high, xhigh, max); Codex takes a `-c model_reasoning_effort=`
override with OpenAI's own vocabulary; pi expresses "think harder" by
swapping models; opencode carries it inside the model id. A runtime with no
effort setting of its own can be taught to realise one **by model**:
`effort.runtimes.pi = model::high=<id>,low=<id>` says "high effort on pi means
this model". Where a task states both a model and an effort that would swap
models, Helm refuses — overriding a stated model to satisfy an effort is
exactly the silent substitution this mechanism exists to prevent.

A runtime that cannot express a level and has no map **refuses a stated
level** rather than dropping it silently — the task's, the project's, or
`HELM_EFFORT`. A root `effort.default` is a floor for runtimes that take one,
not a demand: on such a runtime it is dropped and the task records that it
launched at the runtime's own default. `helm doctor --probe-runtimes` runs
each installed agent's `--help` to catch a flag renamed upstream.

Match the level to the work: high for authoring or reviewing
correctness-critical code — auth, tokens, money, anything security-shaped —
medium for ordinary feature work, low for mechanical rounds like rebases,
evidence runs and doc edits. Say which level you chose and why.

## Turns or a session

`execution.turns` (`on`/`off`, default off) runs every worker this root
starts as non-interactive turns that share one agent session, so nothing is
typed into a pane; a project pins its own with `"execution": "turns"` or
`"session"` in `.helm/project.json`, which outranks the preference either
way. Claude Code, Codex and Cursor resume their sessions across turns; pi
and opencode start a later turn fresh with a catch-up. See
[worker-protocol.md](worker-protocol.md#turn-based-execution).

## Restricting a model family to certain runtimes

Helm ships a **classifier**, not a policy. Given a model identifier it can
say which vendor family it belongs to, recognizing plain ids,
provider-qualified and gateway spellings (`vendor/model`,
`gateway/vendor/model`, `region.vendor.model:0`), and the bare family aliases
agent CLIs accept. On its own the classifier refuses nothing: a fresh clone
with no preferences file pairs every model with every runtime. A root turns a
family into a restriction itself:

```sh
helm prefs set model.runtimes.<family> <runtime> [<runtime>...]
helm prefs keys      # which families the classifier knows
```

With one set, Helm checks the pairing at launch wherever the model came from
— the task's `--model`, a project pin, `HELM_MODEL`, the root's
`model.default`, or a review's `--reviewer-model` — for ordinary workers and
reviewers alike, and for a profile that inherits a runtime the restriction
excludes. A launch command that selects such a model itself (`--model <id>`
or `--model=<id>`) is refused on a runtime the family is not allowed on.
Refusal is **never substitution**: Helm swaps neither the runtime nor the
model, and the message says which runtime is required, that the restriction
is local to this root, and the one command that removes it.

The limit of the check is exact: **Helm reads argv, and nothing else.** An
opaque wrapper — a shell script, an alias, a launcher that picks its model
from its own config file — can still reach a model Helm never sees, because
Helm cannot execute or introspect it. Configuring a runtime with its own
command is a deliberate act by whoever set up the root, and the pairing
inside that command stays theirs to keep.

## Root-local operator preferences

Helm ships no operator choices. Which agent this machine defaults to, which
model, which runtimes it will not pay to start, and which model families may
only run on which runtimes are answers about **one installation**, so they
live in `preferences.json` at the Helm root, which `.gitignore` excludes. The
repository carries the schema, the mechanism, the CLI, the docs and the
tests; it never carries the answers.

```sh
helm prefs keys                          # the supported keys; nothing else is accepted
helm prefs show                          # effective values, env overrides, legacy state
helm prefs path                          # where the file is
helm prefs set agent.default claude
helm prefs set agent.exclude codex omp
helm prefs set model.runtimes.claude claude
helm prefs set model.free prefer         # cost evidence rides into the worker's context
helm prefs set effort.default medium
helm prefs set evidence.standard require   # approve standard tasks only on a recorded, counted suite run
helm prefs set model.prices.some-model-5 in=1,out=5,cache_read=0.1,cache_write=1.25   # USD per million tokens
helm prefs set model.prices.* in=1,out=5,cache_read=0.1,cache_write=1.25              # the flat rate for every other model
helm prefs unset agent.exclude
helm prefs migrate                       # legacy state.config exclusions into the file
```

**Not a credential store.** Every key is enumerated, every value passes the
same narrow validator a runtime or model id passes everywhere else, and there
is no free-text field and no environment block. An unknown key is refused at
load rather than stored, so nothing Helm did not understand can be printed
back out by `prefs show`. Credentials stay with the tool that owns them.

**Versioned and atomic.** The document states a `version`, and a version this
build does not know is refused rather than half-understood. Every write lands
with one `os.replace`, so a concurrent reader sees the old file or the new
one — never a truncated one that would read as "no exclusions".

**Precedence.** A task's own `--agent`, `--model` and `--effort`, then the
project's pin, then an environment variable as a session override, then the
preference file, then (for the runtime only) detection. A project may pin its
own agent or model — more specific — and can neither add nor remove an
exclusion or a family restriction, because the file is read from the root and
nowhere else. Restrictions are the exception to the ladder: exclusions from
the preference file and from any legacy `state.config` entry are **unioned**,
since merging restrictions is the only combination that cannot accidentally
widen one.

**Writes are the commander's.** `prefs set`, `unset` and `migrate` are
root-only, alongside `approval grant` and for the same reason: an agent that
could write a preference could lift the exclusion stopping it from starting
an expensive runtime. `path`, `show` and `keys` stay open, because an agent
that cannot read the policy cannot report it either. Inspect before
asserting: do not tell a commander a restriction is in force without having
looked.

**Backward compatible.** `HELM_AGENT`, `HELM_MODEL`, `HELM_EXCLUDE_AGENTS`,
`HELM_REVIEW_EXCLUDE_AGENTS` and `state.config.excluded_agents` all keep
working. `helm prefs migrate` copies legacy exclusions into the file and
leaves the old entry in place, so an older Helm reading the same root behaves
identically.
