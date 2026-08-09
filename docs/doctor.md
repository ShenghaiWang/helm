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
- follow a path it was pointed at. See "Structural path allowlist" below.
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

## Structural path allowlist

Doctor opens a configuration file because the **layout** puts it there, never
because something asked it to. Three rules, and they are structural rather than
name-based on purpose — a denylist of credential-looking filenames fails on the
file nobody thought of:

- **A symlinked configuration path is refused, not resolved.** `preferences.json`,
  `agents.json`, `agents/*.json`, `agents/<id>/profile.json`, a domain's
  `domain.json`/`knowledge.md`/`guardrails.md`, and a project's `.helm/` and
  `.helm/project.json` are each read only when the path itself is a real file.
  Resolution is not enough: a link from `.helm/project.json` to a credential file
  elsewhere *inside the same project* resolves cleanly and would be read.
- **A symlinked Helm-owned directory stops every check that would traverse it.**
  Reporting `root.symlinks` is not sufficient. A linked `projects/` means the
  project checks are refused outright, not run against a tree outside the root;
  a linked `domains/` or `agents/` means no domain or profile is read.
- **An environment variable that redirects configuration is reported, not
  followed.** `HELM_PREFERENCES_FILE` and `HELM_AGENTS_FILE` each turn their
  check into a `warning` naming the variable, and doctor reads only the root's
  own file. That is a deliberate divergence from the rest of Helm, which honours
  both: doctor's promise is about what it opens.

## Read-only Git probing

Every Git query runs with hooks, `core.fsmonitor`, credential helpers, pagers,
and remote protocols forced off, and with `--no-optional-locks` plus
`GIT_OPTIONAL_LOCKS=0` so no probe refreshes or rewrites the index. Without
that, inspecting a repository would run scripts that repository configured —
a preflight handing control to the thing it was asked to inspect — and a status
probe would be a write.

A probe that **does not answer** (a corrupt index, a timeout, a missing `git`)
is never read as a clean answer. That distinction is the difference between
"this checkout is fine" and "I could not tell", and collapsing it is how a
broken repository reports healthy.

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

A check whose precondition failed is emitted as a `warning` whose message begins
`not checked:` — never omitted. A missing id reads as "nothing to say", which is
not what happened, and an automation comparing id sets would see a shorter list
rather than a stated gap. So the id set in a report is always complete.

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
| `root.state` | state cannot be opened, parsed, is version-unsupported, has an unwalkable shape, or its directory identity does not match the root | state directory or state file **was** readable beyond its owner when Helm opened it |
| `root.boundaries` | any path under `state/`, `agents/`, `projects/`, or `preferences.json` is **tracked** in the root's Git repository, other than the exact shipped `.gitkeep` placeholders | the root is not a Git repository, or Git could not answer, so the boundary cannot be verified |
| `root.preferences` | the file is a symlink, unparseable, oversized, or carries an unknown key or invalid value | `HELM_PREFERENCES_FILE` redirects preferences away from the root |
| `root.domains` | `domains/` is a symlink, or a domain manifest, `extends` chain, or manifest link is invalid | a domain directory has no readable `knowledge.md` |
| `root.profiles` | `agents/` or an agent configuration file is a symlink, a profile is malformed, or a profile's launch **or availability-check** executable is missing, or `HELM_WORKER_COMMAND` names one that is not available | `HELM_AGENTS_FILE` redirects profiles away from the root |
| `root.runtimes` | a runtime this root *names* has no executable on `PATH`, is excluded by this root, or is paired with a model whose family this root restricts to other runtimes | no built-in runtime at all is launchable |
| `root.herdr` | `HERDR_ENV=1` but the `herdr` executable is not on `PATH` | — |
| `root.authority` | — | — (always `ok`; reports whether a capability is configured) |

`root.configured` failing makes the rest unanswerable, so doctor reports that
one finding and stops.

## Project checks

Run only with `--project`. The project is read from the root's `projects/<id>`
and from Helm's own state; **doctor never registers it**.

| id | error when | warning when |
| --- | --- | --- |
| `project.location` | `projects/` or `projects/<id>` is a symlink, or `projects/<id>` is not a directory or resolves outside `projects/` | — |
| `project.git` | not its own committed Git repository root (no repo, nested repo, or no commit) | the checkout is dirty, mid-operation, or Git could not report its state |
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

`project.base_branch` resolves **locally only** — the project setting, Helm's
recorded base branch, a locally recorded `refs/remotes/<remote>/HEAD` that every
remote agrees on, or the checked-out branch of a repository with no remote at
all. It never runs `ls-remote`. Where no local answer exists the finding says so
rather than reaching the network.

The runtimes a project *pins* (`"agent"`, `"model"` in its `.helm/project.json`)
are read before the root checks run, so they are part of what `root.runtimes`
treats as named. A pinned runtime that is not installed is a broken requirement
whether it was named by the root or by one project.

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
| `2` | doctor could not run: invalid invocation, or an unknown project id. |

A Helm root whose **state file** cannot be opened is not a `2`: that is the
condition doctor exists to name. Every command opens the store before dispatch,
so without special handling the one case most needing a report would produce
none. Instead the store-independent checks still run, `root.state` is an
`error`, the remaining checks are `not checked:` warnings, and doctor exits `1`
with a complete document on stdout.

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
  read back out of the file;
- prints a runtime or model id only when it is a word from a fixed known
  vocabulary (a built-in runtime id, or a configured profile's id) or a value
  from a file `helm prefs show` already prints. An id that came from
  `HELM_AGENT`, `HELM_MODEL`, or `HELM_WORKER_COMMAND` is described by its
  source and never quoted — "it looks like an ordinary runtime id" is exactly
  the judgement a leak survives.

## Implementation pointers

The checks live in `helm/doctor.py` and reuse the existing loaders and
validators rather than restating policy: `preferences.load`, `StateStore`'s open
validation, `Coordinator._discovery_settings`, `Coordinator.domain_meta` and
`_domain_extends`, `Coordinator.discover_skills`, `Coordinator.list_agent_profiles`
and `builtin_runtime_availability`, `Coordinator.task_retained_resources`, and
`runtimes.herdr_integration_status`. A check that needed a rule of its own would
be a rule that had drifted from where it is enforced.
