# Herdr worker spaces

Herdr is the default place a delegated worker runs: a real terminal in a
pane the commander can open. It is never required for delegation itself —
without it the worker is still spawned through Helm's core process launcher
into the same isolated worktree — and Helm treats its resources as things it
owns only when it created them. Adapter conventions are in
[agent-adapters.md](agent-adapters.md).

## One workspace per project

Inside a verified Herdr-managed environment (`HERDR_ENV=1` plus a `herdr`
executable) the adapter presents one workspace per project, holding one tab
per ticket plus the overview pane that project's routed messages print into.
Every agent working on a ticket — its lead, author, reviewer, scout — runs in
its own pane of that ticket's tab, split in unfocused. Work on a different
ticket gets its own tab, and work with no ticket is grouped with nothing: it
keeps a tab of its own. Helm finds a ticket's tab through its own layout
record, never by its title, and a reviewer belongs to the ticket of the work
it checks. Closing an agent closes its pane; the ticket's tab closes with its
last pane. A lead appointed before its ticket was known has a tab of its own
until it takes the ticket on, and then that tab becomes the ticket's -- before
`helm run` launches the ticket's task, so that task opens beside it. Placing a
pane, adopting a tab and closing a tab's last pane each happen under one
layout lock (`state/herdr-layout.lock`), so a close never takes a pane that a
launch elsewhere has just split in.
There is no separate coordinator workspace; a legacy one recorded by an older
version can be closed with `helm herdr cleanup-coordinator`.

Helm persists opaque IDs, reuses a recorded space instead of creating a
second one, verifies the space still exists before reusing it, and closes
only resources it created. It never adopts, retitles, or lifecycle-manages a
user resource, and never starts, stops, focuses, restarts, or deletes one.

**Spawning is silent.** Spaces are created unfocused and Helm never issues a
focus call, so starting a worker never switches what the commander is
looking at.

## Labels and panes

Labels are display only: Helm identifies every resource by opaque ID and
never looks one up by label. A Herdr panel shows only the first few
characters, so labels are short and front-loaded — a workspace is the
project's glyph and ID, a ticket's tab is the ticket, and each pane in it is
the agent's role (`lead`, `author 7ded`, `reviewer 3fa1`). A tab with no
ticket is named for its work and role plus four characters of the worker ID.
`helm herdr relabel` applies the scheme to spaces, tabs and panes that
already exist.

## The agents view

Herdr's agents sidebar lists every pane holding an agent. Its own detection
finds an interactive agent by its process, but a turns-mode worker runs its
agent only during a turn, so between turns its pane holds Helm's runner and
nothing more. The runner therefore reports the pane itself, under the source
`helm`: `idle` between turns, `working` during one, and a release when it
exits. It does not report state for an interactive agent, which Herdr already
detects — a second authority saying `idle` would hide the `blocked` the
runtime's own integration reports.

In both modes the runner reports display metadata: the tokens `$ticket` and
`$role`, and a display name of `<runtime> · <role>`. The metadata carries a
six-hour TTL and is refreshed hourly, so a long-lived worker keeps its labels
and a dead one loses them. Each refresh reads the ticket from the task record
as it is then, and a lead that takes a ticket after launch has it reported the
moment its tab becomes the ticket's. Every report is best effort; a Herdr that
cannot be reached never fails a launch or a turn.

The default sidebar rows already show the tab (the ticket) and the agent
(its display name, with the role). To show the tokens explicitly, set the
rows in your own Herdr config, for example:

```toml
[ui.sidebar.agents]
rows = [["state_icon", "workspace", "$ticket"], ["agent", "$role"]]
```

Herdr's own "grouped" ordering (`ui.agent_panel_sort = "spaces"`) groups by
workspace, which is the project; the grouping by ticket comes from the tab.

Lines routed into a project's pane are plain text carrying the project's name
and ID; escape codes do not survive `pane run`, so per-project colour is
delivered in the Helm session's own output rather than in panes. In a pane
the runner gives the worker a real terminal and mirrors it to both the pane
and Helm's log, so an interactive agent renders its session and stays usable
instead of showing a blank pane. Each tab and pane is started with automatic
shell updates disabled, because an update prompt in a fresh shell ate the launch
keystrokes and left a worker that never started.

## When a space closes

A project's space is closed automatically once its work is finished and
reported — nothing running, and every task either delivered (`merged` or
`pr-merged`) or cleaned up. `helm watch` also sweeps recorded project spaces
that have no remaining worker tabs, and `helm worker stop` checks the same
release gate after closing a pane.

What keeps a space open, and why:

- a **completed but undelivered** task, whether or not it still has a worker
  tab — releasing the tab is the first thing a clean result does, so a
  missing pane says nothing about whether the change was delivered;
- a **failed or blocked** task, while its pane holds the diagnosis;
- an **approval-needed** task, because a human still has to look.

A task lead's own task never holds a space open: it produces no branch, so it
has nothing to deliver. `HELM_KEEP_SPACES=1` keeps every space.

## A closed space closes the report

The commander closing a project's space is an explicit signal: that project's
day-to-day is out of their attention, and its lines leave every report and
status summary. Two things still surface — an item that needs the
commander's own decision (an approval, a destructive gate) and a genuine
emergency. Everything else about a closed-space project is answered only when
asked.
