# Optional agent and presentation adapters

The repository-native workflow is adapter-free, but it is not delegation-free:
the agent running in the Helm root is the coordinator, and the work is always
done by a worker it spawns. This document describes optional discovery and
presentation only; it is not required setup and does not define a provider
command for normal conversation.

## Runtime discovery

The main agent should use the capabilities and runtime metadata exposed by its
active harness first. It may discover available runtimes/tools from that
environment and choose one whose capabilities match the assignment. It must
not infer availability from a profile file, a provider name, or an invented
launch command.

Delegation is mandatory, so use this order to find a worker runtime:

1. inspect the active environment or harness tool inventory for an advertised
   runtime that can be spawned as a worker, without requiring the user to
   configure `HELM_WORKER_COMMAND` or `agents.json`;
2. otherwise use a built-in runtime from [`helm/runtimes.py`](../helm/runtimes.py),
   resolved most-specific-first: the task's named agent, the project's
   `.helm/project.json` pin, `HELM_AGENT` or a configured profile, the root's
   own `agent.default` preference (`helm prefs show`), and finally
   the runtime this Helm session is itself running under;
3. when inside Herdr (`HERDR_ENV=1` and an available `herdr` executable), run
   that worker in the project's one Helm-owned Herdr workspace, reusing the
   recorded space and creating one only when Herdr no longer has it;
4. otherwise spawn the worker through Helm's core process launcher into the
   same isolated task worktree, and report that Herdr was unavailable;
5. if no runtime is discoverable at all, explain that limitation and ask the
   user for a specific runtime — do not complete the assignment in the
   coordinator instead.

A missing runtime is not permission to do the work inline, edit another
project, broaden the task, or guess a provider-specific executable. Optional CLI profiles and
`HELM_WORKER_COMMAND` are advanced overrides for callers that intentionally
launch an external process; they are not a discovery mechanism for the native
path.

## Adding a built-in runtime

A built-in runtime is a launch definition, not an adapter: an executable, the
interactive and non-interactive argv that hand the agent one prompt, the
credential variables that agent reads, and the environment marker that
identifies it as the session's own runtime. Keep each field verified against
the CLI's real interface rather than assumed, keep the credential list to what
that runtime actually reads — it is the only widening of the worker
environment scrub — and add tests for a named-but-missing executable, the
interactive/non-interactive split, and per-task selection. A runtime never
carries capacity, capability, or authority; those stay with configured
profiles and Helm core.

## Herdr adapter boundary

Herdr is the default worker surface, not Helm's coordinator or approval
authority. The adapter may create and reuse only Helm-owned resources whose
opaque IDs it has recorded. It must preserve project IDs, labels, colors, assigned
worktrees, and routed messages without using focus, labels, or workspace order
to identify a resource. It must fall back to Helm's process path when the
managed session or executable is unavailable.

The adapter must not adopt, close, or otherwise alter a user-owned resource.
It must not turn worker output into approval, merge, publication, credentials,
scope expansion, or destructive authority. The authoritative implementation is
[`helm/herdr.py`](../helm/herdr.py); core task, state, worktree, and approval
rules remain in [`helm/core.py`](../helm/core.py).

Herdr agent integrations are recognition/control hooks, not worker launch
recipes. Installing an integration may let Herdr classify an already-running
agent's state or expose it through `herdr agent`, but Helm still needs either a
built-in runtime in [`helm/runtimes.py`](../helm/runtimes.py) or a configured
profile that names a concrete command before that agent can be selected for
work. `helm agent check` may report `herdr integration status` as live
inventory, especially on a newly configured laptop, but do not route a task to a
Herdr kind merely because it appears in `herdr agent` help.

## Adding an adapter

Keep provider-specific detection, identifiers, transport, and presentation in
an optional adapter module. The core path must continue to work with no
adapter installed and must retain its safe process fallback. Add tests for
unavailable-provider fallback, opaque-ID ownership, partial setup cleanup, and
worker/project isolation. Update `AGENTS.md` and the README only with stable
workflow rules and pointers, not provider-specific launch instructions.
