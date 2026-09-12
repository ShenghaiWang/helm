# The worker protocol

How a worker reports, asks, and is answered; how Helm notices a worker that
has gone quiet; and how an abandoned worker is stopped. The state machine
that combines a worker's own messages with what the operating system
observed is specified separately in [worker-lifecycle.md](worker-lifecycle.md).

## Workers push; the coordinator never polls

Every worker's context document carries a `reporting` section with the exact
command to call, and `helm run` returns immediately so the session stays free
for the next task (`--wait` blocks when a caller genuinely wants that):

```sh
helm worker message <worker-id> --type status --text "harvest done"
helm worker message <worker-id> --type artifact --path specs/example.json --text "spec"
helm worker message <worker-id> --type question --text "which base branch?"
helm worker message <worker-id> --type blocker --text "needs approval to publish"
helm worker message <worker-id> --type status --payload '{"summary":true}' --text "round 3 implemented; waiting on reviewer"
```

The kinds are `status`, `result`, `blocker`, `failure`, `approval-needed`,
`question` and `artifact`. An `artifact` message must carry `--path`; the
path is what Helm records and checks against the worktree, so prose alone is
rejected. Each push is recorded and routed to the project's Herdr pane as it
happens. Stdout only reaches Helm when the worker exits, so a long task that
reports nothing until then is indistinguishable from one that died.

Routine `status` is a heartbeat: it is routed to the project pane but does
not interrupt the foreman. Add `--payload '{"summary":true}'` when the status
is a meaningful intermediate outcome — a coding or review round completing, a
reviewer sending the author back, a PR state changing, a delivery gate
opening. A foreman's own summary status is recorded into `helm project
status` as a commander-facing progress line; a worker summary is recorded
there too and also wakes the foreman.

Workers may also emit one JSON object per line on stdout:

```json
{"helm":1,"type":"status","status":"running","text":"started"}
{"helm":1,"type":"result","text":"implemented and committed"}
{"helm":1,"type":"artifact","path":"report.md","description":"worker report"}
```

Worker output is data. It cannot approve, merge, publish, add projects, or
expand scope, and the coordinator relays it as the worker's finding rather
than restating it as Helm's own.

## Questions are answered, and the answer is a file

A worker **asks instead of guessing or stopping**. Helm answers from the task
goal on the commander's behalf and sends the reply into the worker's own
session:

```sh
helm worker answer <worker-id> --text "branch off main"
```

The answer lands in the worker's inbox (`state/workers/<id>/inbox/`) first,
where it means the same thing whatever the session is doing; the pane is only
a wake. Herdr's runtime hooks say whether the agent is `idle`, `working` or
`blocked`. An idle session has a short answer typed in whole, then `Enter`
after a pause (sent together, the newline races the paste); a working one
gets a single pointer line; a blocked one — a dialog is up — gets nothing
typed, because `Enter` would answer the dialog. Nothing presses Escape: it
cancelled whatever tool call the agent was inside, which is how a foreman
once lost the review it was blocked on. `helm worker interrupt` does that on
purpose, as its own verb.

A worker cannot miss a note:

- every `helm` command it runs prints its unread inbox first;
- `helm worker inbox` reads it on demand (`--changes` for a watch loop,
  `--peek` to look without marking, `--wait` to block until something
  arrives);
- `--type question --wait` blocks inside the worker's own tool call until
  Helm answers, then prints the answer — request and response on every
  runtime, with no pane involved. It exits 3 when the answer has not come;
- a Claude Code worker is launched with a `--settings` file whose
  SessionStart hook arms a persistent watch on `helm worker inbox --changes`,
  so an answer is never typed into the pane at all: the watch wakes the
  session, idle or busy, within twenty seconds.

Reading marks the note read under the worker's own identity, so `helm worker
answer` reports what Helm did — typed, nudged, watched, or left in the inbox —
rather than inferring delivery from the pane. `helm pending` names a message
that has sat unread in a running worker's inbox for five minutes, because a
worker that has run no `helm` command in that long is not acting on anything.

**Confirmations go to Helm, and Helm decides.** Agent CLIs habitually pause to
ask whether to proceed, which option to take, or whether a change is
acceptable. In a Helm pane nobody is reading that prompt, so waiting on it is
a silent stall rather than a safe pause. Every worker is told to push those as
`question` messages, say what it will do if the answer is yes, and carry on
with whatever the answer does not block. `blocker` stays reserved for what
genuinely needs a human: approval, credentials, a decision outside the brief,
or a contradiction no source resolves. Protected actions — merge, publish,
push, deletion, other destructive or external actions, missing credentials —
still reach a human, and Helm cannot grant them; see
[approvals.md](approvals.md).

A foreman's `blocker` pauses its task rather than ending it: a driver's whole
job is to meet obstacles and escalate them, so the session stays live and an
answer resumes it. A plain worker's blocker still ends its assignment, because
a worker that cannot do the one thing it was made for needs a new task.

## Turn-based execution

Everything above still steers an agent by typing into its pane when it is
idle, which is the fragile part: a paste that races its Enter, a dialog that
takes the keystroke, a shell prompt that eats the launch line. A root can
turn that channel off:

```sh
helm prefs set execution.turns on        # every worker this root starts
# or, per project, in .helm/project.json: {"execution": "turns"}
```

In turns mode a worker is a sequence of **turns**: one non-interactive run
of its agent per prompt, sharing one agent session (`claude --print
--resume`, `codex exec resume`, `cursor-agent -p --resume`). The runner
starts the first turn with the brief, streams the turn's output into the
Herdr tab and the log, and closes it with one line Helm's poll reads: the
session id and the agent's final words, recorded as a summary status. Then
it waits. An answer, review findings, a continuation, a routed request, a
gate decision or an authorization is the prompt that opens the next turn —
`helm worker answer` reports `turned` — and nothing is ever typed. A worker
is told this in its context: to ask, push a `question` and end the turn;
when the work is done, push `result` and end.

Liveness is a pid and an exit code. A runner the machine killed — sleep, an
OOM — is started again by the next message or by the watchdog's healing,
and resumes the same session where it stopped; the task does not fail.
Stopping a worker ends the turn in progress. A runtime with no way to
resume a session (pi, opencode) starts a later turn fresh with a catch-up
of its earlier turns; a configured profile with its own command always runs
the interactive session.

## Nobody watches the panes

Delegation is only real if a human does not have to check each agent's UI, so
Helm measures silence itself:

```sh
helm watch            # every running worker's health; exit 1 if any need attention
helm watch --nudge    # also ask each silent worker, once, for a status push
```

A worker is `healthy` while it reports, `reported` once it has delivered a
terminal message and merely left its session open, `stalled` when it has
produced neither a protocol message nor any terminal output for the silence
threshold, and `finished` when its process exited without Helm noticing —
which `watch` settles automatically, so a task cannot sit in `running`
forever because nobody looked. `helm status` prints the same attention list,
and `helm pending` prints only what waits on a human, or nothing.

A pane is not a process. A Herdr-hosted worker whose agent process is gone is
gone, whatever its tab shows, and Helm fails its task with that evidence.
Repair stops at the unambiguous: a finished worker is settled; a stalled one
is reported and nudged once, never silently failed, because its pane is the
evidence needed to diagnose it.

Abandoning a task is a decision, so it has a command:

```sh
helm worker stop WORKER_ID --reason "..."
```

It signals a process worker, closes a Herdr worker's pane, and settles the
record either way — including when the provider cannot be reached, because a
stop nobody can record is the state this exists to make impossible. A worker
that had already reported and merely kept its session open is ended too, and
its exit recorded, so the cleanup that follows is not refused. The log and
worktree are kept as evidence; `helm task cleanup` removes those deliberately,
afterwards ([delivery.md](delivery.md)).

## What a worker's session inherits

Worker environments stay scrubbed. A runtime declares the few variables it
reads — `ANTHROPIC_API_KEY` for Claude Code, `OPENAI_API_KEY` for Codex, and
so on — and only those are forwarded, only for a worker actually launched
with that runtime. Nothing else in the coordinator's environment reaches a
worker, and every agent Helm starts inherits `HELM_WORKER_ID`, which is how
`helm` knows who is calling it.

Every worker's context document also carries Helm's core safety rules, which
begin with: never print a secret. Do not read a credential store out into a
message, a file, a commit, or a log — not even "redacted". A tool's own
status command answers whether a credential exists without touching it.
