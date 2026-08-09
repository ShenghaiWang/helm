# `helm doctor` — a read-only preflight of a Helm root

## Problem

A Helm root is a small pile of preconditions: a layout, a state directory only
this root may own, an ignored preferences file, a domain pack, a set of agent
runtimes that must actually exist on `PATH`, and — under Herdr — a presentation
surface. Every one of them is checked somewhere already, but only at the moment
it is needed: a missing runtime surfaces when a worker fails to launch, a
malformed preferences file surfaces when the next command loads it, a project
that is not a committed Git repository surfaces when discovery refuses it.

That is the wrong time to find out. The operator wants one command that answers
"is this root sound, and is this project delegable" *before* work is delegated,
without changing anything to find out.

## Purpose and non-goals

`helm doctor` is a **read-only preflight**. It inspects evidence Helm can
already read safely, reports findings with stable identifiers and severities,
and exits with a status automation can branch on.

It does **not**:

- initialize, repair, migrate, register, clean, or discover anything. Nothing on
  disk changes, and no project is added to Helm's state. A root that is broken
  is reported broken; `helm init` remains an explicit, separate, human-run
  initialization operation.
- fetch, push, prune, or otherwise touch a remote. All Git evidence is local.
- run a provider's auth, status, or model command. Doctor reports whether a
  named runtime's **executable exists**, and says plainly that executable
  presence is not credential or catalogue readiness. Dynamic runtime and model
  readiness is deliberately a separate, later capability.
- read, print, summarize, or even stat the *contents* of any credential store,
  `.env`, auth file, token cache, or environment **value**. Doctor may report
  that an environment variable is *set* where that is the configuration fact
  under test (`HERDR_ENV`, `HELM_AGENT`); it never prints what it holds.
- decide anything. It reports; the human acts.

## Invocation

```
helm doctor [--project <id>] [--json]
```

- with no `--project`, only root-scoped checks run.
- `--project <id>` **adds** project-scoped checks for one managed project. It
  never replaces the root checks: a project finding is rarely interpretable
  without the root's.
- `--json` emits the machine-readable document below instead of text. The
  findings are identical; only the rendering differs.

Doctor is read-only, so it is available to every caller — coordinator, foreman,
and worker alike. It authorizes nothing.

## Findings

One check produces exactly one finding. A finding is:

| field | meaning |
| --- | --- |
| `id` | stable dotted check id, e.g. `root.preferences`. Never renamed or reused for a different meaning. |
| `scope` | `root` or `project` |
| `severity` | `ok`, `warning`, or `error` |
| `message` | one line, concise, no secrets, no host-specific noise beyond the paths Helm already prints |
| `remediation` | one line naming the concrete next step. Required when severity is not `ok`; the empty string when it is `ok`. |

**Severity means one thing only:**

- `error` — a configured requirement is broken. Work delegated against this root
  or project would fail, or would be unsafe. Doctor exits non-zero.
- `warning` — something is degraded, absent-but-optional-and-worth-knowing, or
  needs a human decision. Work can still proceed. Doctor still exits 0.
- `ok` — checked, and sound. Informational statements ("Herdr is not in use, so
  workers run through the process launcher") are `ok`, not `warning`.

The distinction that matters most: **an optional capability being absent is not
an error; a *named* requirement being broken is.** A runtime nobody configured
that is not installed is `ok`/`warning`. A runtime this root names — in
`agent.default`, in a `model.runtimes.<family>` restriction, in `HELM_AGENT`, in
a configured profile, or in a project's `agent` pin — whose executable is not on
`PATH` is an `error`, because something already decided to depend on it.

## Ordering and determinism

Findings are emitted in a fixed declared order: all root checks in the order
listed below, then all project checks. Within a check that reports over a set
(domains, profiles, projects), the set is sorted by id. Two runs against an
unchanged root produce byte-identical output. Nothing is ordered by dict
iteration, filesystem order, or wall-clock time.

## Root checks

| id | error when | warning when |
| --- | --- | --- |
| `root.configured` | no Helm root can be resolved at all | — |
| `root.layout` | `projects/` or `state/` is missing | `domains/` or `agents/` is missing |
| `root.symlinks` | a Helm-owned root directory (`projects`, `domains`, `agents`, `state`) is a symlink | — |
| `root.state` | state cannot be opened, parsed, or is version-unsupported, or its directory identity does not match the root | state directory or state file is readable beyond its owner |
| `root.boundaries` | local state or preferences is **tracked** in the root's Git repository | the root is not a Git repository, so the boundary cannot be verified |
| `root.preferences` | the file is a symlink, unparseable, oversized, or carries an unknown key or invalid value | — |
| `root.domains` | a domain manifest or `extends` chain is invalid | a domain directory has no readable `knowledge.md` |
| `root.profiles` | a configured agent profile is malformed, or its launch executable is missing | — |
| `root.runtimes` | a runtime this root *names* has no executable on `PATH` | no built-in runtime at all is launchable |
| `root.herdr` | `HERDR_ENV=1` but the `herdr` executable is not on `PATH` | — |
| `root.authority` | — | — (always `ok`; reports whether a capability is configured) |

`root.configured` failing makes the rest unanswerable, so doctor reports that
one finding and stops.

## Project checks

Run only with `--project`. The project is read from the root's `projects/<id>`
and from Helm's own state; **doctor never registers it**.

| id | error when | warning when |
| --- | --- | --- |
| `project.location` | `projects/<id>` exists but is a symlink or is not a directory | — |
| `project.git` | not its own committed Git repository root (no repo, nested repo, or no commit) | the checkout is dirty or mid-operation |
| `project.isolation` | its root overlaps another registered project's root | — |
| `project.config` | `.helm/project.json` is unreadable, is not an object, or fails validation | — |
| `project.base_branch` | a configured `base_branch` does not resolve locally | no base branch can be determined **locally**; name one explicitly |
| `project.domains` | a declared default domain does not exist under `<root>/domains/` | a declared domain is missing `knowledge.md` or `guardrails.md` |
| `project.skills` | a **pinned** skill has no readable `SKILL.md` | some discoverable skill manifest could not be read |
| `project.retained` | — | tasks still hold a worktree, branch, or worker directory, or carry an open approval hold |

A `--project` id that names **nothing at all** under `projects/` is a mistyped
invocation, not a finding about the root: doctor exits `2` with the same
"unknown project" refusal every other command gives, rather than reporting a
fault in a root that may be perfectly sound.

`project.base_branch` resolves **locally only** — recorded base branch, project
setting, or a local ref. It never runs `ls-remote`. Where a local answer is not
available the finding says so rather than reaching the network.

## JSON output

`--json` prints one JSON object and nothing else:

```json
{
  "version": 1,
  "root": "/abs/path/to/root",
  "project": "example",
  "status": "warning",
  "summary": {"ok": 9, "warning": 2, "error": 0},
  "findings": [
    {
      "id": "root.layout",
      "scope": "root",
      "severity": "ok",
      "message": "projects/, domains/, agents/ and state/ are present",
      "remediation": ""
    }
  ]
}
```

- `version` is `1` and is bumped only when an existing consumer would misread
  the document.
- `root` is the absolute root path, or `null` when none could be resolved.
- `project` is the requested project id, or `null`.
- `status` is the highest severity present: `error` > `warning` > `ok`.
- every key above is always present; `remediation` is `""` for an `ok` finding.
- `findings` is in the declared order above.

## Text output

Compact, one line per finding, plus an indented remediation line for anything
not `ok`, and a one-line summary:

```
helm doctor: root /abs/path/to/root
  ok       root.layout        projects/, domains/, agents/ and state/ are present
  warning  root.boundaries    the Helm root is not a Git repository, so tracked/ignored boundaries cannot be verified
           -> initialize Git at the root, or ignore this if the root is deliberately untracked
project example
  error    project.git        projects/example has no commit
           -> create an initial commit in projects/example
1 error, 1 warning, 1 ok
```

## Exit status

| code | meaning |
| --- | --- |
| `0` | doctor ran and found no `error` finding. Warnings do not change this. |
| `1` | doctor ran and found at least one `error` finding. |
| `2` | doctor could not run: invalid invocation, unknown project, or a root that could not be opened at all. |

`2` is the code the rest of the Helm CLI already uses for a refused or failed
command, so automation branching on doctor branches the same way it does on
every other command.

## Secret safety

Doctor inherits the hard rule in `CORE_SAFETY_RULES`: no credential ever reaches
output, not even redacted. Concretely, doctor:

- never opens `auth.json`, `.env`, a keychain, or a token cache, and never
  enumerates a credential directory;
- never prints an environment variable's **value** — only whether the one
  variable a check is *about* is set;
- reports runtime readiness as executable presence only, and says so, rather
  than invoking a provider command that would print account state;
- rebuilds every preferences line from validated fields (the same rule
  `helm prefs show` follows), so a value Helm did not understand can never be
  read back out of the file.

## Implementation pointers

The checks live in `helm/doctor.py` and reuse the existing loaders and
validators rather than restating policy: `preferences.load`, `StateStore`'s open
validation, `Coordinator._discovery_settings`, `Coordinator.domain_meta` and
`_domain_extends`, `Coordinator.discover_skills`, `Coordinator.list_agent_profiles`
and `builtin_runtime_availability`, `Coordinator.task_retained_resources`, and
`runtimes.herdr_integration_status`. A check that needed a rule of its own would
be a rule that had drifted from where it is enforced.
