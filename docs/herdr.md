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
per worker plus the overview pane that project's routed messages print into.
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
project's glyph and ID, a tab is a slug of its task plus four characters of
the worker ID. `helm herdr relabel` applies the scheme to spaces and tabs
that already exist.

Lines routed into a project's pane are plain text carrying the project's name
and ID; escape codes do not survive `pane run`, so per-project colour is
delivered in the Helm session's own output rather than in panes. In a pane
the runner gives the worker a real terminal and mirrors it to both the tab
and Helm's log, so an interactive agent renders its session and stays usable
instead of showing a blank pane. Each tab is started with automatic shell
updates disabled, because an update prompt in a fresh shell ate the launch
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

A foreman's own task never holds a space open: it produces no branch, so it
has nothing to deliver. `HELM_KEEP_SPACES=1` keeps every space.

## A closed space closes the report

The commander closing a project's space is an explicit signal: that project's
day-to-day is out of their attention, and its lines leave every report and
status summary. Two things still surface — an item that needs the
commander's own decision (an approval, a destructive gate) and a genuine
emergency. Everything else about a closed-space project is answered only when
asked.
