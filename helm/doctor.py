"""`helm doctor` -- a read-only preflight of a Helm root and one project.

Every precondition a Helm root has is already checked somewhere: the layout at
`helm init`, the preferences file at load, the runtime executable at launch, the
committed Git repository at discovery. Each of those checks fires at the moment
it is needed, which is the moment it is most expensive to be wrong. Doctor runs
them early, together, and changes nothing.

Four properties are load-bearing, and the tests hold each of them:

**It is read-only.** Nothing here initializes, registers, repairs, cleans,
fetches, or discovers. A broken root is reported broken -- `helm init` stays an
explicit human operation, and a project that is not registered stays that way,
because a preflight that quietly fixed what it found would make "is this sound"
unanswerable. That extends to Helm's own machinery: opening a state store
normally tightens permissions and creates a lock file, so doctor opens a
read-only store that performs no repair and refuses every write. Reporting the
exposure accurately was never a substitute for not causing it.

**It reads only structurally-allowed paths.** A configuration path is followed
because the layout puts it there, never because a file or an environment
variable pointed at it. A symlinked configuration file is refused rather than
resolved, a symlinked Helm-owned directory stops every check that would have
traversed it, and an environment variable that redirects configuration is
*reported* rather than followed. Filename denylisting ("don't open `auth.json`")
would be the wrong shape entirely: the file nobody thought of walks straight
through it.

**Absence and breakage are different findings.** An optional capability that is
not installed is not a fault. A requirement this root *named* -- in
`agent.default`, in a model-family restriction, in `HELM_AGENT`, in a configured
profile, in a project's pin -- that cannot be satisfied is, because something
already decided to depend on it. Collapsing the two makes the report noise.

**It never touches a secret.** Doctor reads no credential store, runs no
provider auth, status, or catalogue command, and prints no environment *value* --
not even one that looks like an ordinary runtime id, because "looks ordinary" is
exactly the judgement a leak survives. An id reaches output only when it is a
word from a fixed known vocabulary or a value from a file `helm prefs show`
already prints. That applies to messages doctor merely *relays* as much as to
ones it writes: the launcher's own refusals quote the argv and model they
objected to, so doctor states the refusal in its own generic terms rather than
passing them through.

The contract -- check ids, severities, output shape, exit codes -- is in
`docs/doctor.md`. Checks reuse the existing loaders and validators rather than
restating policy; a check with a rule of its own would be a rule that had
drifted from where it is enforced.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Sequence

from . import preferences as prefs
from . import runtimes
from .core import (
    CHECKOUT_OPERATION_MARKERS,
    Coordinator,
    HelmError,
    SafetyError,
    canonical,
    inside,
    overlaps,
)

#: Bumped only when an existing consumer would misread the JSON document.
REPORT_VERSION = 1

OK = "ok"
WARNING = "warning"
ERROR = "error"

#: Highest wins. The report's `status` is the maximum severity present.
_RANK = {OK: 0, WARNING: 1, ERROR: 2}

#: Helm-owned root children. The first two are the root; without them there is
#: nothing to coordinate. The second two are conventional and recreatable.
_REQUIRED_LAYOUT = ("projects", "state")
_OPTIONAL_LAYOUT = ("domains", "agents")

#: The only paths under Helm's local directories that may be tracked. An
#: allowlist of exact paths, not a basename match: `state/private/.gitkeep`
#: is a tracked state directory whatever its file is called.
_TRACKABLE_PLACEHOLDERS = frozenset(
    {"projects/.gitkeep", "agents/.gitkeep", "state/.gitkeep", "domains/.gitkeep"}
)

#: Environment variables that redirect a configuration path Helm would
#: otherwise read from the root's own layout. Doctor reports each as a
#: warning and declines to follow it: the whole value of a structural
#: allowlist is that a variable cannot move a read somewhere else.
_PREFERENCES_OVERRIDE = prefs.PREFERENCES_ENV
_AGENTS_OVERRIDE = "HELM_AGENTS_FILE"

#: Every check id doctor can emit, in the order it emits them. Keeping the
#: order here rather than in call order is what makes "a check that could not
#: run" a stated finding instead of a silent gap.
ROOT_CHECKS = (
    "root.configured",
    "root.layout",
    "root.symlinks",
    "root.state",
    "root.boundaries",
    "root.preferences",
    "root.domains",
    "root.profiles",
    "root.runtimes",
    "root.herdr",
    "root.authority",
)
PROJECT_CHECKS = (
    "project.location",
    "project.git",
    "project.isolation",
    "project.config",
    "project.base_branch",
    "project.domains",
    "project.skills",
    "project.retained",
)


@dataclass(frozen=True)
class Finding:
    """One check's verdict. `remediation` is required unless severity is ok."""

    id: str
    scope: str
    severity: str
    message: str
    remediation: str = ""

    def document(self) -> dict[str, str]:
        return {
            "id": self.id,
            "scope": self.scope,
            "severity": self.severity,
            "message": self.message,
            "remediation": self.remediation,
        }


@dataclass
class Report:
    root: Path | None
    project: str | None
    findings: list[Finding] = field(default_factory=list)

    @property
    def status(self) -> str:
        return max((f.severity for f in self.findings), key=lambda s: _RANK[s], default=OK)

    @property
    def summary(self) -> dict[str, int]:
        counts = {OK: 0, WARNING: 0, ERROR: 0}
        for finding in self.findings:
            counts[finding.severity] += 1
        return counts

    def document(self) -> dict[str, Any]:
        return {
            "version": REPORT_VERSION,
            "root": str(self.root) if self.root is not None else None,
            "project": self.project,
            "status": self.status,
            "summary": self.summary,
            "findings": [finding.document() for finding in self.findings],
        }

    @property
    def exit_code(self) -> int:
        return 1 if self.summary[ERROR] else 0


# ---------- read-only Git probing ----------

#: Configuration doctor forces off for every probe, ahead of any system,
#: global, or repository config. `core.fsmonitor` and `core.hooksPath` are the
#: sharp ones: a repository can point either at a script, and a preflight that
#: runs a checkout's own executable has handed control to the thing it was
#: asked to inspect. The credential and pager settings close the same shape of
#: hole for anything that would spawn a helper.
_GIT_SAFE_CONFIG = (
    "-c", "core.fsmonitor=",
    "-c", "core.hooksPath=/dev/null",
    "-c", "core.askPass=",
    "-c", "credential.helper=",
    "-c", "core.pager=cat",
    "-c", "gc.auto=0",
    "-c", "protocol.allow=never",
    "-c", "advice.detachedHead=false",
)

#: `--no-optional-locks` plus `GIT_OPTIONAL_LOCKS=0` keep a status probe from
#: refreshing and rewriting the index, which is a write, however harmless it
#: looks. The three config variables take every configuration file out of the
#: picture, so the `-c` flags above are the whole of the configuration.
_GIT_SAFE_ENV = {
    "GIT_TERMINAL_PROMPT": "0",
    "GIT_OPTIONAL_LOCKS": "0",
    "GIT_CONFIG_NOSYSTEM": "1",
    "GIT_CONFIG_GLOBAL": os.devnull,
    "GIT_CONFIG_SYSTEM": os.devnull,
    "GIT_ASKPASS": "",
    "GIT_ALLOW_PROTOCOL": "",
    "GIT_PAGER": "cat",
}


def _git_lines(root: Path, *args: str) -> list[str] | None:
    """Local, hardened, non-interactive git output, or None when git failed.

    `None` means *the probe did not answer* -- a corrupt index, a missing git,
    a timeout -- and is deliberately distinct from a clean empty answer. A
    caller that treats the two the same reports a repository it could not read
    as a repository with nothing to say, which is the failure mode that makes a
    preflight worse than no preflight.

    Never fetches, never writes: doctor's whole Git surface is local queries
    about a checkout that is already on disk.
    """
    # Every inherited `GIT_*` variable is dropped, not just the ones with
    # known-dangerous names. `GIT_DIR`, `GIT_INDEX_FILE`, `GIT_WORK_TREE`,
    # `GIT_OBJECT_DIRECTORY`, `GIT_CONFIG_GLOBAL` and friends each redirect
    # which files git opens, so an allowlist of overrides layered on top of a
    # full environment leaves the redirect in place -- the `-c` flags stop a
    # repository configuring a helper, and do nothing about the environment
    # pointing git at another file entirely. Dropping the whole namespace is
    # the only version of this that does not need a complete list of git's
    # path variables to be correct.
    env = {name: value for name, value in os.environ.items() if not name.startswith("GIT_")}
    env.update(_GIT_SAFE_ENV)
    try:
        result = subprocess.run(
            ["git", "--no-optional-locks", *_GIT_SAFE_CONFIG, "-C", str(root), *args],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=15,
            env=env,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    return [line for line in result.stdout.splitlines() if line.strip()]


#: A configuration line that pulls in another file. Git reads a repository's
#: own `.git/config` no matter what the command line says, and an `include` or
#: `includeIf` there names a path git will open -- anywhere on the filesystem,
#: including a credential file. There is no flag that turns includes off, so
#: the read is prevented by not running git against such a repository at all.
_GIT_INCLUDE = re.compile(r"\[\s*include(?:if)?\b|\binclude(?:if)?\s*\.\s*path\b", re.I)


def _administrative_git_dir(path: Path) -> bool:
    """Whether a path is somewhere a git administrative directory can live.

    A *linked worktree* keeps its git directory under the main repository's
    `.git/worktrees/<name>`, which is legitimately outside the worktree -- and
    Helm's own task worktrees are exactly that shape, so refusing it would
    break doctor for the workflow this repository is built around. What must
    still be refused is a gitfile pointing somewhere that is not a git
    administrative area at all: `gitdir: ~/.aws` would otherwise have doctor
    read `~/.aws/config`. Requiring a `.git` component is a structural test
    that admits every real worktree and no credential directory.
    """
    return ".git" in canonical(path).parts


def _git_config_files(repo_dir: Path) -> tuple[list[Path], str]:
    """Every config file git would read for this repository, or why not.

    Located without running git, because running git is the thing being gated.
    `.git` is normally a directory; a *gitfile* points at one elsewhere, which
    is followed only into a git administrative area.
    """
    dot = repo_dir / ".git"
    if _is_symlink(dot):
        return [], "its .git is a symlink"
    if dot.is_dir():
        gitdir = dot
    elif dot.is_file():
        try:
            raw = dot.read_text(encoding="utf-8").strip()
        except (OSError, UnicodeDecodeError) as exc:
            return [], f"its .git file could not be read: {exc}"
        if not raw.startswith("gitdir:"):
            return [], "its .git file does not name a git directory"
        pointed = Path(raw[len("gitdir:"):].strip()).expanduser()
        gitdir = pointed if pointed.is_absolute() else repo_dir / pointed
        if not inside(canonical(gitdir), canonical(repo_dir)) and not _administrative_git_dir(gitdir):
            return [], "its .git file points outside any git directory"
    else:
        return [], ""
    files = [gitdir / "config"]
    common = gitdir / "commondir"
    if common.is_file():
        try:
            pointed = Path(common.read_text(encoding="utf-8").strip())
        except (OSError, UnicodeDecodeError) as exc:
            return [], f"its commondir could not be read: {exc}"
        resolved = pointed if pointed.is_absolute() else gitdir / pointed
        if not inside(canonical(resolved), canonical(repo_dir)) and not _administrative_git_dir(resolved):
            return [], "its commondir points outside any git directory"
        files.append(resolved / "config")
    return files, ""


def git_include_problem(repo_dir: Path) -> str:
    """Why git must not be run against this repository, or "".

    Screened *before* any probe. The overrides on the command line stop a
    repository configuring a helper Helm would execute; they do nothing about
    an `include.path` naming a file git will read. Refusing to probe is the
    only version of this that prevents the read rather than merely surviving
    it -- and the refusal is a finding, so the repository is not quietly
    skipped either.
    """
    files, problem = _git_config_files(repo_dir)
    if problem:
        return problem
    for config in files:
        if _is_symlink(config):
            return "its Git config is a symlink"
        if not config.is_file():
            continue
        try:
            raw = config.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            return f"its Git config could not be read: {exc}"
        for line in raw.splitlines():
            stripped = line.strip()
            if not stripped or stripped[0] in "#;":
                continue
            if _GIT_INCLUDE.search(stripped):
                return (
                    "its Git config uses an include directive, which would make "
                    "git read a file outside Helm's control"
                )
    return ""


# ---------- small read-only helpers ----------


def _exposed(mode: int | None) -> bool:
    return mode is not None and bool(mode & 0o077)


def _is_symlink(path: Path) -> bool:
    try:
        return path.is_symlink()
    except OSError:  # pragma: no cover - stat failures are reported elsewhere
        return False


def _named(runtime_id: str, vocabulary: Iterable[str]) -> str:
    """A runtime id, but only when it is a word doctor is allowed to print.

    The vocabulary is the built-in runtime ids plus the ids of profiles this
    root configured -- both fixed, operator-visible words. Anything else came
    from an environment variable, and an environment value never reaches
    output however ordinary it looks.
    """
    return runtime_id if runtime_id in set(vocabulary) else "an unrecognized runtime"


# ---------- the checks ----------


class _Doctor:
    """Accumulates findings in declared order. One method per check id."""

    def __init__(
        self,
        coordinator: Coordinator | None,
        helm_root: Path | None,
        *,
        state_error: str = "",
    ) -> None:
        #: Opt-in: run each installed agent's --help to catch a renamed
        #: effort flag. Off by default because doctor starts nothing.
        self.probe_runtimes = False
        self.coordinator = coordinator
        self.root = canonical(helm_root) if helm_root is not None else None
        self.findings: list[Finding] = []
        self._scope = "root"
        #: Set when the store could not be opened at all; every check that
        #: needs it says so rather than being quietly dropped.
        self._state_error = state_error
        self._state_ok = not state_error
        #: Helm-owned root directories that are symlinks. Reporting one is not
        #: enough -- every check that would have walked through it has to stop,
        #: or the report describes a tree outside the root it claims to be
        #: inspecting.
        self._linked: set[str] = set()
        #: Filled before the root checks run when --project is given, so a
        #: project's pinned runtime and model are part of "what this root
        #: names" rather than a requirement discovered too late to check.
        self._project_root: Path | None = None
        self._project_id: str = ""
        self._project_settings: dict[str, Any] = {}
        self._profiles: list[dict[str, Any]] | None = None
        #: True when a redirect means doctor never read the preferences Helm
        #: would actually use, so every conclusion drawn from them is stated
        #: as unchecked rather than asserted against an empty set.
        self._preferences_unchecked = False
        #: Domains `root.domains` found unreadable -- a linked directory, a
        #: linked manifest, a broken extends chain. A project declaring one is
        #: declaring a source that will not arrive, so `project.domains` reads
        #: this rather than asking the filesystem again: `is_dir()` follows a
        #: link and would call the very domain that check just refused present.
        self._unusable_domains: set[str] = set()
        #: Doctor resolves the preferences file structurally and pins the
        #: result, so no core method called on its behalf can follow the
        #: redirect it declined -- `excluded_agents` and the launch checks both
        #: consult preferences, and each would otherwise reopen the very file
        #: `root.preferences` refused.
        if coordinator is not None:
            self._preferences_unchecked = self._preferences_override()
            try:
                coordinator.use_preferences(self.root_preferences())
            except (prefs.PreferencesError, OSError, ValueError):
                coordinator.use_preferences(prefs.EMPTY)
                self._preferences_unchecked = True

    # -- recording --

    def _add(self, check: str, severity: str, message: str, remediation: str = "") -> None:
        self.findings.append(
            Finding(check, self._scope, severity, message, "" if severity == OK else remediation)
        )

    def ok(self, check: str, message: str) -> None:
        self._add(check, OK, message)

    def warn(self, check: str, message: str, remediation: str) -> None:
        self._add(check, WARNING, message, remediation)

    def error(self, check: str, message: str, remediation: str) -> None:
        self._add(check, ERROR, message, remediation)

    def _unchecked(self, check: str, because: str) -> None:
        """A check that could not run. Stated, never omitted."""
        self.warn(check, f"not checked: {because}", "resolve the finding it depends on, then rerun")

    # ---------- root ----------

    def run_root(self) -> bool:
        """Root checks. Returns False when nothing further is answerable."""
        self._scope = "root"
        if self.root is None:
            self.error(
                "root.configured",
                "no Helm root could be resolved from --root, HELM_ROOT, or this directory",
                "run helm doctor --root <path>, or helm init <path> to create one",
            )
            return False
        self.ok("root.configured", f"Helm root {self.root}")
        self._check_layout()
        self._check_symlinks()
        self._check_state()
        self._check_boundaries()
        self._check_preferences()
        self._check_domains()
        self._check_profiles()
        self._check_runtimes()
        self._check_herdr()
        self._check_watchdog()
        self._check_authority()
        return self._state_ok

    def _check_watchdog(self) -> None:
        """Is the out-of-session backstop installed on this machine?

        The reporting chain's last hop -- reaching a human while no session is
        open -- only exists if the platform scheduler runs `helm watchdog`.
        Nothing machine-specific is tracked in the repository, so a fresh
        clone has no watchdog until someone runs the install; a root without
        one works, but silently loses exactly the notifications it most needs,
        which is why this is a warning and not a note.
        """
        import platform as _platform
        from pathlib import Path as _Path
        if _platform.system() == "Darwin":
            entry = _Path.home() / "Library" / "LaunchAgents" / "com.helm.watchdog.plist"
            present = entry.exists()
        elif _platform.system() == "Linux":
            entry = (
                _Path.home() / ".config" / "systemd" / "user" / "helm-watchdog.timer"
            )
            present = entry.exists()
        else:
            self.ok("root.watchdog", "no scheduler integration for this platform")
            return
        if present:
            self.ok("root.watchdog", f"watchdog scheduler entry present ({entry.name})")
        else:
            self.warning(
                "root.watchdog",
                "no watchdog scheduler entry: nothing reaches a human while no "
                "session is open",
                "run helm watchdog install",
            )

    def _check_layout(self) -> None:
        assert self.root is not None
        missing_required = [name for name in _REQUIRED_LAYOUT if not (self.root / name).is_dir()]
        missing_optional = [name for name in _OPTIONAL_LAYOUT if not (self.root / name).is_dir()]
        if missing_required:
            self.error(
                "root.layout",
                f"the Helm root is missing {', '.join(missing_required)}/",
                f"run helm init {self.root} to create the root layout",
            )
            return
        if missing_optional:
            self.warn(
                "root.layout",
                f"the Helm root has no {', '.join(missing_optional)}/",
                f"run helm init {self.root}; domains/ carries shared knowledge and "
                "agents/ carries optional profiles",
            )
            return
        self.ok(
            "root.layout",
            "projects/, domains/, agents/ and state/ are present",
        )

    def _check_symlinks(self) -> None:
        """Find linked Helm-owned directories *and* stop traversing them.

        A symlinked `projects/` is not a cosmetic finding: every project check
        after it would inspect, and run git against, a tree somebody else
        controls. So the names land in `self._linked` and the dependent checks
        refuse rather than resolve.
        """
        assert self.root is not None
        self._linked = {
            name
            for name in (*_REQUIRED_LAYOUT, *_OPTIONAL_LAYOUT)
            if _is_symlink(self.root / name)
        }
        if self._linked:
            linked = sorted(self._linked)
            self.error(
                "root.symlinks",
                f"Helm-owned root director{'ies' if len(linked) > 1 else 'y'} "
                f"{', '.join(f'{name}/' for name in linked)} "
                f"{'are' if len(linked) > 1 else 'is'} a symlink; "
                "checks that would traverse them are refused",
                "replace the symlink with a real directory; a link can point at "
                "a tree this root does not own",
            )
            return
        self.ok("root.symlinks", "no Helm-owned root directory is a symlink")

    def _check_state(self) -> None:
        """Report the state Helm *found*, including what it repaired on open.

        `StateStore` tightens its own directory and files to 0700/0600 the
        moment it is constructed, which is correct and long-standing -- and it
        happens before doctor runs. Reading the modes back off the disk here
        would therefore report the repair rather than the exposure, which is
        the one thing a permission finding must never do. So the store records
        what it found before touching anything, and doctor reports that.
        """
        if not self._state_ok:
            self.error(
                "root.state",
                f"Helm state cannot be read: {self._state_error}",
                "restore or remove the state file; nothing else here is trustworthy "
                "until it opens",
            )
            return
        assert self.coordinator is not None
        store = self.coordinator.store
        try:
            data = store.load()
        except (HelmError, OSError, ValueError) as exc:
            self._state_ok = False
            self.error(
                "root.state",
                f"Helm state cannot be read: {exc}",
                "restore or remove the state file; nothing else here is trustworthy "
                "until it opens",
            )
            return
        malformed = self._state_shape_problem(data)
        if malformed:
            self._state_ok = False
            self.error(
                "root.state",
                f"Helm state has an unusable shape: {malformed}",
                "restore the state file from a backup; a container Helm cannot "
                "iterate is not a state file it can reason about",
            )
            return
        found = getattr(store, "opened_modes", {}) or {}
        exposed = sorted(path for path, mode in found.items() if _exposed(mode))
        if exposed:
            # No "Helm restricted it on open" here: doctor's store is read-only,
            # so the exposure it found is the exposure that is still there. The
            # remediation therefore has to be an action, not reassurance.
            self.warn(
                "root.state",
                f"Helm state is readable beyond its owner: {', '.join(exposed)}",
                "chmod 700 the state directory and 600 the state file; state holds "
                "task records, worker output and approval grants",
            )
            return
        self.ok(
            "root.state",
            f"state opens at {store.directory} "
            f"({len(data.get('projects', {}))} project(s), {len(data.get('tasks', {}))} task(s))",
        )

    @staticmethod
    def _state_shape_problem(data: Any) -> str:
        """Why this state document cannot be walked, or "".

        `StateStore.load` validates the version and fills in missing keys, but
        a document whose `tasks` is a list still parses and still passes the
        version check -- and every later check that iterates it raises an
        AttributeError, which is a traceback where a finding was promised.
        """
        if not isinstance(data, dict):
            return "the document is not an object"
        for key in ("projects", "tasks", "workers"):
            section = data.get(key, {})
            if not isinstance(section, dict):
                return f"{key} is not an object"
            for entry_id, entry in section.items():
                if not isinstance(entry, dict):
                    return f"{key}[{entry_id!r}] is not an object"
        return ""

    def _check_boundaries(self) -> None:
        """Local state and preferences must never be tracked by the root's Git.

        This is a secret-adjacent boundary, not a tidiness one: `state/` holds
        worker output, task records and approval grants, and `preferences.json`
        holds one operator's cost policy. Tracking either ships it to everyone
        who clones the repository.
        """
        assert self.root is not None
        if not (self.root / ".git").exists():
            self.warn(
                "root.boundaries",
                "the Helm root is not a Git repository, so tracked/ignored "
                "boundaries cannot be verified",
                "no action needed if the root is deliberately untracked; otherwise "
                "check that state/, projects/, agents/ and preferences.json are ignored",
            )
            return
        include = git_include_problem(self.root)
        if include:
            self.error(
                "root.boundaries",
                f"the root repository was not inspected because {include}",
                "remove the include directive from the repository's Git config; "
                "doctor will not run git against a repository that can redirect it "
                "into another file",
            )
            return
        tracked = _git_lines(
            self.root,
            "ls-files",
            "--",
            "state",
            "agents",
            "projects",
            prefs.PREFERENCES_FILENAME,
        )
        if tracked is None:
            self.warn(
                "root.boundaries",
                "git could not report which local paths are tracked",
                "run git status at the Helm root to see why git cannot answer",
            )
            return
        # An exact-path allowlist. Matching on the basename let any tracked
        # file called `.gitkeep` -- `state/private/.gitkeep` -- carry a whole
        # tracked state directory through the check.
        leaked = sorted(entry for entry in tracked if entry not in _TRACKABLE_PLACEHOLDERS)
        if leaked:
            shown = ", ".join(leaked[:5]) + (" ..." if len(leaked) > 5 else "")
            self.error(
                "root.boundaries",
                f"{len(leaked)} local path(s) are tracked by the root repository: {shown}",
                "git rm --cached those paths; local state and preferences must stay "
                "out of the repository",
            )
            return
        self.ok(
            "root.boundaries",
            "local state, projects, agents and preferences are untracked",
        )

    # -- preferences, read only from the root's own layout --

    def _preferences_override(self) -> bool:
        return bool(os.environ.get(_PREFERENCES_OVERRIDE, "").strip())

    def root_preferences(self) -> prefs.Preferences:
        """This root's own preferences file, or EMPTY. Never a redirected one.

        `preferences.preferences_path` honours `HELM_PREFERENCES_FILE`, which
        is right for Helm at large -- it is how the suite points a root at a
        fixture. It is wrong for doctor, whose promise is that it opens what
        the layout says and nothing an environment variable names.
        """
        if self.root is None or self._preferences_override():
            return prefs.EMPTY
        path = self.root / prefs.PREFERENCES_FILENAME
        return prefs.load(path)

    def _check_preferences(self) -> None:
        assert self.root is not None
        if self._preferences_override():
            self.warn(
                "root.preferences",
                f"{_PREFERENCES_OVERRIDE} redirects preferences away from the root; "
                "doctor did not open the redirected file",
                f"unset {_PREFERENCES_OVERRIDE} and rerun to check the preferences "
                "Helm would actually use",
            )
            return
        path = self.root / prefs.PREFERENCES_FILENAME
        if _is_symlink(path):
            # Refused, not resolved: following it is how a "preferences" read
            # becomes a read of whatever the link points at.
            self.error(
                "root.preferences",
                f"preferences file must not be a symlink: {path}",
                "replace it with a real file; doctor refuses to follow it",
            )
            return
        try:
            loaded = self.root_preferences()
        except (prefs.PreferencesError, OSError, ValueError) as exc:
            self.error(
                "root.preferences",
                f"operator preferences cannot be loaded: {exc}",
                "fix or remove the file; helm prefs keys lists every supported key",
            )
            return
        if not loaded.present:
            self.ok(
                "root.preferences",
                "no operator preferences are set; Helm imposes none of its own",
            )
            return
        # Rebuilt from validated fields, never echoed from the file -- the same
        # rule `helm prefs show` follows, and what keeps this from becoming a
        # way to print whatever somebody parked in it.
        keys = ", ".join(key for key, _ in loaded.entries()) or "nothing"
        self.ok("root.preferences", f"operator preferences load cleanly ({keys})")

    def _check_domains(self) -> None:
        assert self.root is not None
        if not self._state_ok:
            # Domain lookup resolves its root through the store's recorded
            # Helm root, so an unreadable state turns every domain into a
            # second, misleading error about domains.
            self._unchecked("root.domains", "Helm state could not be read")
            return
        if "domains" in self._linked:
            self.error(
                "root.domains",
                "domains/ is a symlink, so no domain was read",
                "replace the link with a real directory inside the root",
            )
            return
        domain_root = self.root / "domains"
        if not domain_root.is_dir():
            self.warn(
                "root.domains",
                "no domains/ directory, so no shared knowledge can be composed",
                f"run helm init {self.root}, or add domains/<id>/knowledge.md",
            )
            return
        try:
            entries = sorted(domain_root.iterdir(), key=lambda item: item.name)
        except OSError as exc:
            self.error(
                "root.domains",
                f"domains/ cannot be listed: {exc}",
                "check the directory's permissions",
            )
            return
        broken: list[str] = []
        thin: list[str] = []
        names: list[str] = []
        for entry in entries:
            if _is_symlink(entry):
                broken.append(f"{entry.name} (directory is a symlink)")
                self._unusable_domains.add(entry.name)
                continue
            if not entry.is_dir():
                continue
            names.append(entry.name)
            linked = sorted(
                leaf
                for leaf in ("domain.json", "knowledge.md", "guardrails.md")
                if _is_symlink(entry / leaf)
            )
            if linked:
                broken.append(f"{entry.name} ({', '.join(linked)} is a symlink)")
                self._unusable_domains.add(entry.name)
                continue
            try:
                assert self.coordinator is not None
                self.coordinator._domain_chain(domain_root, entry.name)
            except (HelmError, SafetyError, OSError, ValueError) as exc:
                broken.append(f"{entry.name} ({exc})")
                self._unusable_domains.add(entry.name)
                continue
            if not (domain_root / entry.name / "knowledge.md").is_file():
                thin.append(entry.name)
        if broken:
            self.error(
                "root.domains",
                f"{len(broken)} domain(s) are invalid: {'; '.join(broken[:3])}",
                "fix the named domain manifests; an invalid extends chain or a "
                "linked manifest stops every task that resolves that domain",
            )
            return
        if thin:
            self.warn(
                "root.domains",
                f"{len(thin)} domain(s) have no knowledge.md: {', '.join(thin[:5])}",
                "add domains/<id>/knowledge.md, or remove the empty directory",
            )
            return
        self.ok("root.domains", f"{len(names)} domain(s) load cleanly")

    # -- configured profiles, read only from the root's own layout --

    def _agents_config_paths(self) -> list[Path]:
        """Every path `_load_agent_profiles` would read, from the layout alone."""
        assert self.root is not None
        candidates = [self.root / "agents.json", self.root / ".helm" / "agents.json"]
        agents_dir = self.root / "agents"
        if "agents" not in self._linked and agents_dir.is_dir():
            try:
                for entry in sorted(agents_dir.iterdir(), key=lambda item: item.name):
                    if _is_symlink(entry):
                        candidates.append(entry)
                    elif entry.is_file() and entry.suffix == ".json":
                        candidates.append(entry)
                    elif entry.is_dir():
                        candidates.append(entry / "profile.json")
            except OSError:  # pragma: no cover - reported by the caller
                pass
        return candidates

    def profiles(self) -> list[dict[str, Any]]:
        """Configured profiles, or [] when doctor declined to read them."""
        return self._profiles or []

    def _check_profiles(self) -> None:
        assert self.root is not None
        if not self._state_ok:
            self._unchecked("root.profiles", "Helm state could not be read")
            return
        if os.environ.get(_AGENTS_OVERRIDE, "").strip():
            self.warn(
                "root.profiles",
                f"{_AGENTS_OVERRIDE} redirects agent profiles away from the root; "
                "doctor did not open the redirected file",
                f"unset {_AGENTS_OVERRIDE} and rerun to check the profiles Helm "
                "would actually use",
            )
            return
        if "agents" in self._linked:
            self.error(
                "root.profiles",
                "agents/ is a symlink, so no profile was read",
                "replace the link with a real directory inside the root",
            )
            return
        linked = sorted(
            str(path) for path in self._agents_config_paths() if _is_symlink(path)
        )
        if linked:
            self.error(
                "root.profiles",
                f"agent configuration must not be a symlink: {', '.join(linked[:3])}",
                "replace the link with a real file; doctor refuses to follow it",
            )
            return
        assert self.coordinator is not None
        try:
            self._profiles = self.coordinator.list_agent_profiles()
        except (HelmError, SafetyError, OSError, ValueError) as exc:
            self.error(
                "root.profiles",
                f"configured agent profiles cannot be read: {exc}",
                "fix agents.json or agents/<id>/profile.json",
            )
            return
        problems = self._profile_problems(self._profiles)
        if problems:
            self.error(
                "root.profiles",
                f"{len(problems)} configured profile(s) cannot launch: "
                + "; ".join(problems[:3]),
                "install the named executable or correct the profile's command",
            )
            return
        ambient = self._ambient_worker_command_problem()
        if ambient:
            self.error("root.profiles", ambient[0], ambient[1])
            return
        if not self._profiles:
            self.ok(
                "root.profiles",
                "no configured agent profiles; the built-in runtimes are used",
            )
            return
        self.ok(
            "root.profiles",
            f"{len(self._profiles)} configured profile(s) name a launchable "
            "executable, including any availability check (executable presence "
            "only, not credential readiness)",
        )

    def _profile_problems(self, profiles: Sequence[dict[str, Any]]) -> list[str]:
        """Every configured profile that could not actually start a worker.

        The launch argv is not the whole of it. A profile's `check_command` is
        run at launch, so a check whose executable is missing fails the launch
        just as surely -- doctor verifies that it *exists* and deliberately
        never runs it, because an availability check is exactly the shape of
        command that prints account state.
        """
        assert self.coordinator is not None
        problems: list[str] = []
        # A profile command may be relative to the worktree a worker runs in,
        # which is what real launch validation resolves it against.
        cwd = self._project_root
        for profile in sorted(profiles, key=lambda item: str(item.get("id"))):
            resolved = self.coordinator._resolve_profile(profile, interactive=True)
            command = resolved.get("command")
            if not command:
                problems.append(f"{profile['id']}: no launch command")
                continue
            # The validator's own reason quotes argv[0], and argv comes out of
            # a configuration file whose contents doctor does not print. The
            # profile id is enough to find it, and is itself a narrow validated
            # identifier that `helm agent list` already shows.
            available, _ = self.coordinator._check_command(command, cwd=cwd)
            if not available:
                problems.append(f"{profile['id']}: launch executable is not available")
                continue
            check = resolved.get("check_command")
            if check:
                available, _ = self.coordinator._check_command(check, cwd=cwd)
                if not available:
                    problems.append(
                        f"{profile['id']}: availability-check executable is not available"
                    )
        return problems

    def _ambient_worker_command_problem(self) -> tuple[str, str] | None:
        """`HELM_WORKER_COMMAND` is a launch this root configured, so check it.

        Reported without quoting the variable's value: it is an environment
        value, and doctor prints none of those. The failure reason names the
        variable, which is all a reader needs to find it.
        """
        assert self.coordinator is not None
        if not os.environ.get("HELM_WORKER_COMMAND", "").strip():
            return None
        try:
            command = self.coordinator._worker_command(None)
        except HelmError:
            return (
                "HELM_WORKER_COMMAND is set but does not parse into a command",
                "correct or unset HELM_WORKER_COMMAND",
            )
        available, _ = self.coordinator._check_command(command, cwd=self._project_root)
        if available:
            return None
        return (
            "HELM_WORKER_COMMAND names an executable that is not available",
            "install it, correct HELM_WORKER_COMMAND, or unset it and use a "
            "built-in runtime",
        )

    # -- named runtimes and the models paired with them --

    def _vocabulary(self) -> set[str]:
        """Ids doctor may print: fixed built-ins plus configured profile ids."""
        return set(runtimes.builtin_runtime_ids()) | {
            str(profile["id"]) for profile in self.profiles()
        }

    def named_runtimes(self) -> dict[str, list[str]]:
        """Runtime id -> what named it. Only these are requirements.

        A project's own pin belongs here as much as a root preference does: a
        project that says `"agent": "codex"` has made every one of its workers
        depend on codex, and finding that out when the first spawn fails is
        exactly what this command exists to prevent.
        """
        named: dict[str, list[str]] = {}

        def name(runtime_id: str | None, source: str) -> None:
            if not runtime_id or runtime_id == "none":
                return
            named.setdefault(runtime_id, []).append(source)

        try:
            loaded = self.root_preferences()
        except (prefs.PreferencesError, OSError, ValueError):
            loaded = prefs.EMPTY
        name(loaded.default_agent, "preference agent.default")
        for family, allowed in sorted(loaded.model_runtimes.items()):
            for runtime_id in sorted(allowed):
                name(runtime_id, f"preference model.runtimes.{family}")
        pinned = self._project_settings.get("agent")
        if isinstance(pinned, str):
            name(pinned, "the project's agent pin")
        name(os.environ.get("HELM_AGENT", "").strip() or None, "HELM_AGENT")
        return named

    def _named_models(self) -> list[tuple[str, str, bool]]:
        """(model, source, printable) for every model this root names.

        `printable` is False for a model that came from an environment
        variable. The classifier still classifies it and the pairing is still
        enforced; it simply never reaches output.
        """
        try:
            loaded = self.root_preferences()
        except (prefs.PreferencesError, OSError, ValueError):
            loaded = prefs.EMPTY
        models: list[tuple[str, str, bool]] = []
        if loaded.default_model:
            models.append((loaded.default_model, "preference model.default", True))
        pinned = self._project_settings.get("model")
        if isinstance(pinned, str) and pinned:
            # Not printable. A project's `.helm/project.json` is project
            # configuration, the same class of content as a profile's launch
            # command -- only a preferences value is quotable, because
            # `helm prefs show` prints it by design.
            models.append((pinned, "the project's model pin", False))
        ambient = os.environ.get("HELM_MODEL", "").strip()
        if ambient:
            models.append((ambient, "HELM_MODEL", False))
        return models

    def _effort_drift_problems(self) -> list[str]:
        """Effort flags Helm records that the installed CLI no longer publishes.

        The capability table is a shipped default, and CLIs rename flags. A
        stale entry does not fail loudly: the flag is passed, the runtime
        rejects or ignores it, and the work runs at a level nobody chose. So
        the runtime's own --help is the authority, checked here rather than
        trusted at launch. Only a warning: --help output is not a contract,
        and doctor must not fail a healthy root over a formatting change.
        """
        problems: list[str] = []
        for runtime in runtimes.BUILTIN_RUNTIMES:
            if runtime.effort_mechanism == runtimes.EFFORT_UNSUPPORTED:
                continue
            if not shutil.which(runtime.command(interactive=True)[0]):
                continue
            try:
                result = subprocess.run(
                    [runtime.command(interactive=True)[0], "--help"],
                    capture_output=True,
                    text=True,
                    timeout=20,
                    check=False,
                )
            except (OSError, subprocess.SubprocessError):
                continue
            published = f"{result.stdout}\n{result.stderr}"
            # A flag appears in --help by name. A config KEY does not -- only
            # the option that carries it does -- so checking the key would
            # report every config-mechanism runtime as broken forever.
            expected = (
                runtime.effort_argument
                if runtime.effort_mechanism == runtimes.EFFORT_FLAG
                else "--config"
            )
            if expected and expected not in published:
                problems.append(
                    f"{runtime.id} no longer publishes {expected}; Helm's "
                    "recorded effort mechanism may be stale"
                )
        return problems

    def _pairing_problems(self, named: dict[str, list[str]]) -> list[str]:
        """Model families this root restricted, paired with a runtime it forbids.

        The restriction is enforced at launch; a preflight that ignored it
        would call a root healthy and then refuse its very first spawn.
        """
        try:
            loaded = self.root_preferences()
        except (prefs.PreferencesError, OSError, ValueError):
            return []
        if not loaded.model_runtimes:
            return []
        vocabulary = self._vocabulary()
        problems: list[str] = []
        for model, source, printable in self._named_models():
            constraint = loaded.constraint_for(model)
            if constraint is None:
                continue
            family, allowed = constraint
            for runtime_id in sorted(named):
                if runtime_id in allowed:
                    continue
                shown = _named(runtime_id, vocabulary)
                subject = f"model {model} ({source})" if printable else f"the model named by {source}"
                problems.append(
                    f"{subject} is in the {family} family, which this root restricts "
                    f"to {', '.join(sorted(allowed))}, but {shown} is named by "
                    f"{', '.join(sorted(set(named[runtime_id])))}"
                )
        return problems

    def _launchable_builtins(self, excluded: set[str]) -> list[str]:
        """Built-in runtimes this machine could start, by executable presence.

        Deliberately not `builtin_runtime_availability`, which additionally
        shells out to `herdr integration status`. Herdr recognizing an agent
        says nothing about whether Helm can launch it, and a preflight that
        spawns an external process to answer a question it does not use is one
        more thing that can hang or vary between runs.
        """
        assert self.coordinator is not None
        launchable: list[str] = []
        for runtime_id in sorted(runtimes.builtin_runtime_ids()):
            if runtime_id in excluded:
                continue
            runtime = runtimes.builtin_runtime(runtime_id)
            if runtime is None:  # pragma: no cover - ids come from the registry
                continue
            available, _ = self.coordinator._check_command(
                runtime.command(interactive=True), cwd=self._project_root
            )
            if available:
                launchable.append(runtime_id)
        return launchable

    def _primary_model(self) -> tuple[str, str, bool]:
        """The model a worker would actually get, most-specific first.

        Same ladder the launcher uses -- project pin, then `HELM_MODEL` as a
        session override, then the root default. `printable` is False when the
        value came from the environment.
        """
        pinned = self._project_settings.get("model")
        if isinstance(pinned, str) and pinned:
            return pinned, "the project's model pin", True
        ambient = os.environ.get("HELM_MODEL", "").strip()
        if ambient:
            return ambient, "HELM_MODEL", False
        try:
            loaded = self.root_preferences()
        except (prefs.PreferencesError, OSError, ValueError):
            return "", "", True
        if loaded.default_model:
            return loaded.default_model, "preference model.default", True
        return "", "", True

    def effective_default_agent(self) -> tuple[str | None, str]:
        """The runtime a task naming none would actually get, and its label.

        Resolved through `_default_agent_id`, the launcher's own ladder, so the
        detection step is included rather than approximated. Detection is the
        step a preflight most needs to simulate: it is the only one that
        produces a runtime nothing in the configuration mentions, which is
        exactly the case an inspection of named requirements cannot see.
        """
        assert self.coordinator is not None
        # `_project_agent` falls back to reading the project's own settings
        # when the record carries no pin, so it needs a root. Doctor has
        # already read those settings structurally, and points the fallback at
        # a directory that has none rather than letting it open anything else.
        project = {
            "id": self._project_id or "",
            "agent": self._project_settings.get("agent"),
            "root": str(self._project_root or (self.root / "projects" if self.root else ".")),
        }
        try:
            agent_id, _ = self.coordinator._default_agent_id(project)
        except (HelmError, SafetyError, OSError, ValueError):
            return None, ""
        if not agent_id:
            return None, ""
        # The reason string from `_default_agent_id` can quote HELM_AGENT's
        # value, so it is discarded; the id itself is printed only when it is a
        # vocabulary word.
        return agent_id, _named(agent_id, self._vocabulary())

    def _launch_candidates(self) -> list[tuple[str, dict[str, Any], list[str]]]:
        """(label, profile, command) for every launch doctor can simulate.

        These are the launches a worker would actually be given, as opposed to
        the runtime ids something merely names. A profile that inherits a
        built-in runtime, a profile whose command bakes its own `--model`, a
        custom argv from `HELM_WORKER_COMMAND`, and the runtime *detection*
        picks when nothing names one each fail differently at launch, and none
        of them is visible to an executable-presence check.
        """
        assert self.coordinator is not None
        candidates: list[tuple[str, dict[str, Any], list[str]]] = []
        for profile in sorted(self.profiles(), key=lambda item: str(item.get("id"))):
            resolved = self.coordinator._resolve_profile(profile, interactive=True)
            command = resolved.get("command")
            if command:
                candidates.append((f"profile {profile['id']}", resolved, list(command)))
        if os.environ.get("HELM_WORKER_COMMAND", "").strip():
            try:
                ambient = self.coordinator._worker_command(None)
            except HelmError:
                ambient = []
            if ambient:
                candidates.append((
                    "the launch named by HELM_WORKER_COMMAND",
                    {"id": "default", "builtin": False},
                    ambient,
                ))
        for runtime_id in sorted(self.named_runtimes()):
            runtime = runtimes.builtin_runtime(runtime_id)
            if runtime is None:
                continue
            profile = self.coordinator._builtin_profile(runtime, interactive=True)
            candidates.append((
                f"runtime {_named(runtime_id, self._vocabulary())}",
                profile,
                list(profile["command"]),
            ))
        # The effective default last, and only when nothing above already
        # covers it: a root that names no runtime at all still launches one,
        # and that is the launch a preflight has least excuse to miss.
        effective, shown = self.effective_default_agent()
        if effective and effective not in self.named_runtimes():
            try:
                profile = self.coordinator._profile_for_agent_id(
                    self.profiles(), effective, interactive=True
                )
            except HelmError:
                profile = {}
            command = profile.get("command") if profile else None
            if command:
                candidates.append((
                    f"the runtime this session detects ({shown})", profile, list(command)
                ))
        return candidates

    def _refusal_reason(
        self, profile: dict[str, Any], command: Sequence[str], model: str
    ) -> str:
        """Why a launch would be refused, in generic terms only.

        Deliberately not `_with_model`'s own message. That message is written
        for someone who already has the configuration in front of them, so it
        quotes the model it objected to -- and that model may have been parsed
        out of a *profile's launch command*, which is configuration-file
        content doctor does not print. A family name is different: it comes
        from a fixed enumeration in the classifier and is already a preference
        key, so it names the restriction to remove without disclosing anything.

        `_with_model` stays the authority on *whether* a launch is refused;
        this only says which of the three shapes of refusal it was.
        """
        try:
            loaded = self.root_preferences()
        except (prefs.PreferencesError, OSError, ValueError):  # pragma: no cover
            loaded = prefs.EMPTY
        baked = runtimes.model_in_command(command)
        if baked is not None:
            constraint = loaded.constraint_for(baked)
            if constraint is not None:
                family, allowed = constraint
                return (
                    "its launch command selects a model in the "
                    f"{family} family, which this root restricts to "
                    f"{', '.join(sorted(allowed))}"
                )
        if model:
            effective = profile.get("runtime") or profile.get("id")
            constraint = loaded.constraint_for(model)
            if constraint is not None and runtimes.builtin_runtime(effective) is not None:
                family, allowed = constraint
                return (
                    f"the resolved model is in the {family} family, which this "
                    f"root restricts to {', '.join(sorted(allowed))}"
                )
            if not profile.get("builtin"):
                return (
                    "it supplies its own launch command, which has nowhere to "
                    "carry a resolved model"
                )
        return "Helm would refuse this launch"

    def _effective_launch_problems(self) -> list[str]:
        """Launches this root has configured that Helm would refuse to start.

        Decided by `_with_model`, which is the code that actually refuses them,
        so a preflight cannot come to a different conclusion than the launcher
        for the same configuration. Nothing is started: the method either
        returns an argv nobody runs, or raises. The *wording* is doctor's own,
        because the launcher's is free to quote configuration doctor is not.
        """
        if self._preferences_unchecked:
            return []
        assert self.coordinator is not None
        model, _, _ = self._primary_model()
        problems: list[str] = []
        for label, profile, command in self._launch_candidates():
            try:
                self.coordinator._with_model(profile, command, model or None, "")
            except HelmError:
                problems.append(f"{label}: {self._refusal_reason(profile, command, model)}")
            except (SafetyError, OSError, ValueError):  # pragma: no cover
                continue
        return problems

    def _check_runtimes(self) -> None:
        if not self._state_ok:
            self._unchecked("root.runtimes", "Helm state could not be read")
            return
        named = self.named_runtimes()
        vocabulary = self._vocabulary()
        assert self.coordinator is not None
        try:
            # Safe to call now: the coordinator's preferences are pinned to the
            # file doctor resolved structurally, so this cannot reopen a
            # redirected one.
            excluded = self.coordinator.excluded_agents()
        except (HelmError, SafetyError, OSError, ValueError):
            excluded = set()
        profile_ids = {str(profile["id"]) for profile in self.profiles()}

        broken: list[str] = []
        for runtime_id in sorted(named):
            sources = ", ".join(sorted(set(named[runtime_id])))
            shown = _named(runtime_id, vocabulary)
            runtime = runtimes.builtin_runtime(runtime_id)
            if runtime is None:
                if runtime_id in profile_ids:
                    continue  # covered by root.profiles
                broken.append(f"{shown} is not a known runtime (named by {sources})")
                continue
            if runtime_id in excluded:
                broken.append(f"{shown} is named by {sources} but this root excludes it")
                continue
            available, reason = self.coordinator._check_command(
                runtime.command(interactive=True), cwd=self._project_root
            )
            if not available:
                broken.append(f"{shown} named by {sources}: {reason}")
        for drifted in (
            self._effort_drift_problems() if self.probe_runtimes else []
        ):
            self.warn("root.runtimes", drifted, "check the runtime's own --help")
        broken.extend(self._pairing_problems(named))
        broken.extend(self._effective_launch_problems())
        if broken:
            self.error(
                "root.runtimes",
                f"{len(broken)} named runtime requirement(s) are unusable: "
                + "; ".join(broken[:3]),
                "install the runtime, name a different one, or clear the preference "
                "with helm prefs unset",
            )
            return

        if self._preferences_unchecked:
            self._unchecked(
                "root.runtimes",
                f"{_PREFERENCES_OVERRIDE} redirects the preferences that decide "
                "exclusions and model-family restrictions",
            )
            return
        launchable = self._launchable_builtins(excluded)
        if not launchable:
            self.warn(
                "root.runtimes",
                "no built-in agent runtime is launchable on this machine",
                "install an agent CLI, or configure a profile in agents.json; "
                "delegation needs something to start",
            )
            return
        # Which runtime is *installed* is not the question a task asks. A task
        # naming no agent gets exactly one runtime, chosen by the ladder ending
        # in detection, and reporting the installed set as though any of them
        # would do is what let a detected runtime the launcher then refused
        # read as healthy here.
        effective, shown = self.effective_default_agent()
        if effective is None:
            self.warn(
                "root.runtimes",
                f"{len(launchable)} built-in runtime(s) are launchable but none is "
                "pinned, configured, or detectable, so a task naming no agent has "
                "nothing to resolve to",
                "name one with helm prefs set agent.default, or pin one in the "
                "project's .helm/project.json",
            )
            return
        if effective in excluded:
            self.error(
                "root.runtimes",
                f"the runtime a task would resolve to ({shown}) is excluded by this root",
                "name a different agent.default, or drop the exclusion with "
                "helm prefs unset agent.exclude",
            )
            return
        self.ok(
            "root.runtimes",
            f"a task naming no agent resolves to {shown}; "
            f"{len(launchable)} built-in runtime(s) launchable: {', '.join(launchable)} "
            "(executable presence only, not credential or catalogue readiness)",
        )

    def _check_herdr(self) -> None:
        """Herdr readiness. Absence outside a Herdr session is not a fault.

        Helm delegates either way: without Herdr the worker starts through the
        core process launcher into the same isolated worktree. So the only
        broken case is a session that declares itself Herdr-managed and has no
        `herdr` to call.
        """
        in_session = os.environ.get("HERDR_ENV") == "1"
        executable = shutil.which("herdr") is not None
        if in_session and not executable:
            self.error(
                "root.herdr",
                "HERDR_ENV=1 but no herdr executable is on PATH",
                "install herdr or unset HERDR_ENV; Helm would otherwise fall back "
                "to the process launcher on every spawn",
            )
            return
        if in_session:
            self.ok("root.herdr", "Herdr is available; workers get a project space")
            return
        self.ok(
            "root.herdr",
            "Herdr is not in use here; workers start through the process launcher"
            + ("" if not executable else " (herdr is installed but HERDR_ENV is not set)"),
        )

    def _check_authority(self) -> None:
        if not self._state_ok:
            self._unchecked("root.authority", "Helm state could not be read")
            return
        assert self.coordinator is not None
        try:
            configured = bool(self.coordinator._authority_hash())
        except (HelmError, SafetyError, OSError, ValueError) as exc:
            self._unchecked("root.authority", f"the authority record could not be read: {exc}")
            return
        self.ok(
            "root.authority",
            "protected commands require a capability"
            if configured
            else "protected commands are guarded by session role only "
            "(helm authority init adds a capability)",
        )

    # ---------- project ----------

    def preload_project(self, project_id: str) -> None:
        """Read the project's own settings before the root checks run.

        Its pinned runtime and model are part of what this root names, and
        `root.runtimes` is emitted before the project scope opens. Any problem
        with the file is reported by `project.config`, which reads it again
        through the same validator; failing quietly here would be a second
        opinion about the same file.
        """
        if self.root is None or "projects" in self._linked:
            return
        project_root = self.root / "projects" / project_id
        if _is_symlink(self.root / "projects") or _is_symlink(project_root):
            return
        if not project_root.is_dir() or self._settings_paths_linked(project_root):
            return
        self._project_root = project_root
        self._project_id = project_id
        try:
            assert self.coordinator is not None
            settings = self.coordinator._discovery_settings(project_root)
        except (HelmError, SafetyError, OSError, ValueError):
            return
        if isinstance(settings, dict):
            self._project_settings = settings

    @staticmethod
    def _settings_paths_linked(project_root: Path) -> bool:
        """Whether `.helm/` or its project.json is a link doctor must not follow.

        `_safe_configuration_path` only asks whether the *resolved* path stays
        inside the project, so a link from `.helm/project.json` to a credential
        file elsewhere in the same checkout resolves cleanly and gets read.
        Structure, not resolution, is the boundary that holds.
        """
        return _is_symlink(project_root / ".helm") or _is_symlink(
            project_root / ".helm" / "project.json"
        )

    def run_project(self, project_id: str) -> None:
        self._scope = "project"
        assert self.root is not None
        if "projects" in self._linked:
            self.error(
                "project.location",
                "projects/ is a symlink, so no project was inspected",
                "replace the link with a real directory inside the root",
            )
            for check in PROJECT_CHECKS[1:]:
                self._unchecked(check, "projects/ is a symlink")
            return
        project_root = self.root / "projects" / project_id
        if not self._check_location(project_id, project_root):
            for check in PROJECT_CHECKS[1:]:
                self._unchecked(check, "the project directory could not be inspected")
            return
        if not self._check_git(project_root):
            for check in PROJECT_CHECKS[2:]:
                self._unchecked(check, "the project is not a usable Git repository")
            return
        self._check_isolation(project_id, project_root)
        settings = self._check_config(project_root)
        if settings is None:
            for check in PROJECT_CHECKS[4:]:
                self._unchecked(check, "the project's settings could not be read")
            return
        self._check_base_branch(project_root, settings)
        self._check_project_domains(settings)
        self._check_skills(project_id, project_root, settings)
        self._check_retained(project_id)

    def _check_location(self, project_id: str, project_root: Path) -> bool:
        assert self.root is not None
        if _is_symlink(project_root):
            self.error(
                "project.location",
                f"projects/{project_id} is a symlink",
                "replace it with a real checkout; a linked project escapes the root",
            )
            return False
        if not project_root.is_dir():
            self.error(
                "project.location",
                f"no project directory at {project_root}",
                f"place the project's own Git checkout at {project_root}",
            )
            return False
        # Belt and braces after the structural checks above: the resolved
        # parent must still be this root's own projects/ directory.
        if canonical(project_root).parent != canonical(self.root / "projects"):
            self.error(
                "project.location",
                f"{project_root} resolves outside {self.root / 'projects'}",
                "place the project's own checkout directly under projects/",
            )
            return False
        self.ok("project.location", f"{project_root} is a direct child of projects/")
        return True

    def _check_git(self, project_root: Path) -> bool:
        # Screened before the first probe, not after it: the point is that git
        # never opens the included file, which a check running afterwards
        # cannot deliver.
        include = git_include_problem(project_root)
        if include:
            self.error(
                "project.git",
                f"the project was not inspected because {include}",
                "remove the include directive from the project's Git config; "
                "doctor will not run git against a repository that can redirect "
                "it into another file",
            )
            return False
        toplevel = _git_lines(project_root, "rev-parse", "--show-toplevel")
        if not toplevel:
            self.error(
                "project.git",
                f"{project_root} is not a Git repository",
                "commit the project as its own repository; Helm never initializes "
                "Git during discovery",
            )
            return False
        if canonical(toplevel[0]) != canonical(project_root):
            self.error(
                "project.git",
                f"{project_root} is inside another repository rooted at {toplevel[0]}",
                "make the project its own isolated repository root",
            )
            return False
        if _git_lines(project_root, "rev-parse", "--verify", "HEAD") is None:
            self.error(
                "project.git",
                f"{project_root} has no commit",
                "create an initial commit; a task's base has to resolve to something",
            )
            return False
        mid_operation = self._mid_operation(project_root)
        if mid_operation:
            self.warn(
                "project.git",
                f"the project checkout has {mid_operation}",
                "finish or abort it; a task's base is pinned from this checkout",
            )
            return True
        dirty = _git_lines(project_root, "status", "--porcelain=v1", "--untracked-files=no")
        if dirty is None:
            # A probe that did not answer is not a clean checkout. Reading the
            # two the same way is how a corrupt index reports as healthy.
            self.warn(
                "project.git",
                "git could not report the state of the project checkout",
                "run git status in the project to see why git cannot answer",
            )
            return True
        if dirty:
            self.warn(
                "project.git",
                "the project checkout has uncommitted changes to tracked files",
                "commit or stash them; a task's base is pinned from this checkout",
            )
            return True
        self.ok("project.git", "a committed Git repository with a clean checkout")
        return True

    @staticmethod
    def _mid_operation(project_root: Path) -> str:
        """An unresolved merge/rebase/cherry-pick, in core's own terms.

        The marker list is shared with `_project_checkout_conflict`, which is
        what actually refuses a task base, so the two cannot drift into
        disagreeing about what "mid-operation" means.
        """
        for marker in CHECKOUT_OPERATION_MARKERS:
            located = _git_lines(project_root, "rev-parse", "--git-path", marker)
            if located and (project_root / located[0]).exists():
                readable = marker.replace("_HEAD", "").replace("-", " ").lower()
                return f"an unresolved {readable} in progress"
        return ""

    def _check_isolation(self, project_id: str, project_root: Path) -> None:
        if not self._state_ok:
            self._unchecked("project.isolation", "Helm state could not be read")
            return
        resolved = canonical(project_root)
        conflicts: list[str] = []
        assert self.coordinator is not None
        try:
            registered = self.coordinator.list_projects()
        except (HelmError, SafetyError, OSError, ValueError):
            registered = []
        for other in registered:
            if not isinstance(other, dict) or other.get("id") == project_id:
                continue
            try:
                other_root = canonical(str(other.get("root", "")))
            except (OSError, ValueError):  # pragma: no cover - canonical rarely raises
                continue
            if overlaps(resolved, other_root):
                conflicts.append(f"{other['id']} ({other_root})")
        if conflicts:
            self.error(
                "project.isolation",
                f"the project root overlaps {', '.join(sorted(conflicts))}",
                "give each project its own non-overlapping checkout under projects/",
            )
            return
        self.ok("project.isolation", "the project root overlaps no other project")

    def _check_config(self, project_root: Path) -> dict[str, Any] | None:
        if self._settings_paths_linked(project_root):
            self.error(
                "project.config",
                ".helm/ or .helm/project.json is a symlink, so it was not read",
                "replace the link with a real file; doctor refuses to follow it",
            )
            return None
        assert self.coordinator is not None
        try:
            settings = self.coordinator._discovery_settings(project_root)
        except (HelmError, SafetyError, OSError, ValueError) as exc:
            self.error(
                "project.config",
                f"project settings are invalid: {exc}",
                "fix .helm/project.json; every later check reads it",
            )
            return None
        if not settings:
            self.ok("project.config", "no .helm/project.json; Helm's defaults apply")
            return settings
        self.ok(
            "project.config",
            ".helm/project.json is valid (" + ", ".join(sorted(settings)) + ")",
        )
        return settings

    def _check_base_branch(self, project_root: Path, settings: dict[str, Any]) -> None:
        """Resolve the base locally. Doctor never asks a remote.

        `ls-remote` is read-only but it is still the network, and a preflight
        that hangs on an unreachable remote is a preflight nobody runs. A
        remote's default that a clone or `git remote set-head` already recorded
        *is* local, though, so it counts -- ignoring it would warn about a
        repository whose base is written down right there in `refs/remotes`.
        """
        declared = settings.get("base_branch")
        if not declared and self._state_ok:
            assert self.coordinator is not None
            try:
                record = self.coordinator.store.load()["projects"].get(project_root.name)
            except (HelmError, OSError, ValueError, AttributeError):
                record = None
            if isinstance(record, dict):
                declared = record.get("base_branch")
        if declared:
            if _git_lines(project_root, "rev-parse", "--verify", f"refs/heads/{declared}"):
                self.ok("project.base_branch", f"base branch {declared} resolves locally")
                return
            self.error(
                "project.base_branch",
                f"the configured base branch {declared} does not exist in the checkout",
                'correct "base_branch" in .helm/project.json, or create the branch',
            )
            return
        remotes = _git_lines(project_root, "remote")
        if remotes is None:
            # Not the same as "no remotes". A repository whose remotes could
            # not be listed might have one, and the checked-out branch is only
            # evidence for a repository that certainly has none.
            self.warn(
                "project.base_branch",
                "no base branch is configured and git could not list the remotes",
                'name one explicitly with "base_branch" in .helm/project.json',
            )
            return
        recorded = self._recorded_remote_default(project_root, remotes)
        if recorded:
            self.ok(
                "project.base_branch",
                f"no base branch is configured; {recorded} is recorded locally as "
                "the default of every remote",
            )
            return
        current = _git_lines(project_root, "symbolic-ref", "--quiet", "--short", "HEAD")
        if current and not remotes:
            self.ok(
                "project.base_branch",
                f"no base branch is configured; the checked-out {current[0]} is the "
                "repository's own default",
            )
            return
        self.warn(
            "project.base_branch",
            "no base branch can be determined locally"
            + (" (the project has a remote whose recorded default is missing or "
               "contested, so the checked-out branch is not evidence)"
               if remotes else " (the checkout is detached)"),
            'name one explicitly with "base_branch" in .helm/project.json',
        )

    @staticmethod
    def _recorded_remote_default(project_root: Path, remotes: Sequence[str]) -> str:
        """The one branch every remote's locally-recorded HEAD agrees on.

        Read from `refs/remotes/<remote>/HEAD`, which a clone writes and
        `git remote set-head` updates -- no network. Disagreement between two
        remotes is not an answer, exactly as it is not one at registration.
        """
        if not remotes:
            return ""
        candidates: set[str] = set()
        for remote in remotes:
            symbolic = _git_lines(
                project_root, "symbolic-ref", "--quiet", "--short",
                f"refs/remotes/{remote}/HEAD",
            )
            prefix = f"{remote}/"
            if not symbolic or not symbolic[0].startswith(prefix):
                # A remote with no recorded HEAD is missing evidence, not
                # absent from the vote. Skipping it would let one configured
                # remote speak for a repository whose other remote might well
                # disagree -- which is agreement inferred from silence.
                return ""
            candidates.add(symbolic[0][len(prefix):])
        if len(candidates) != 1:
            return ""
        branch = next(iter(candidates))
        if not _git_lines(project_root, "rev-parse", "--verify", f"refs/heads/{branch}"):
            return ""
        return branch

    def _check_project_domains(self, settings: dict[str, Any]) -> None:
        assert self.root is not None
        declared = list(settings.get("domains") or [])
        if not declared:
            self.ok(
                "project.domains",
                "no default domain is declared; tasks resolve one explicitly",
            )
            return
        if "domains" in self._linked:
            self._unchecked("project.domains", "domains/ is a symlink")
            return
        domain_root = self.root / "domains"
        missing = [
            name
            for name in declared
            if name in self._unusable_domains or not (domain_root / name).is_dir()
        ]
        if missing:
            self.error(
                "project.domains",
                "declared domain(s) do not exist or are unreadable: "
                + ", ".join(sorted(missing)),
                "add the domain under domains/, fix the one root.domains named, "
                'or correct the project\'s "domains" setting',
            )
            return
        thin = sorted(
            f"{name}/{leaf}"
            for name in declared
            for leaf in ("knowledge.md", "guardrails.md")
            if not (domain_root / name / leaf).is_file()
        )
        if thin:
            self.warn(
                "project.domains",
                f"declared domain(s) are missing {', '.join(thin[:5])}",
                "a missing source is a missing source, never permission to invent "
                "guidance; add the file",
            )
            return
        self.ok(
            "project.domains",
            f"declared domain(s) resolve: {', '.join(sorted(declared))}",
        )

    def _check_skills(
        self, project_id: str, project_root: Path, settings: dict[str, Any]
    ) -> None:
        project = {"id": project_id, "root": str(project_root)}
        assert self.coordinator is not None
        try:
            discovered = self.coordinator.discover_skills(project, settings.get("agent"))
        except (HelmError, SafetyError, OSError, ValueError) as exc:
            self.error(
                "project.skills",
                f"project skills cannot be read: {exc}",
                "fix the project's SKILL.md manifests",
            )
            return
        available = {skill["id"] for skill in discovered["skills"]}
        pinned = [
            str(entry).strip()
            for entry in (settings.get("skills") or {}).get("pin", [])
            if str(entry).strip()
        ]
        unmet = sorted(name for name in pinned if name not in available)
        if unmet:
            self.error(
                "project.skills",
                f"pinned skill(s) have no readable SKILL.md: {', '.join(unmet)}",
                "add the manifest, or remove the pin from .helm/project.json; a "
                "pinned skill Helm cannot read is a required input that never arrives",
            )
            return
        problems = discovered["problems"]
        if problems:
            shown = ", ".join(
                f"{entry['id'] or entry['root']} ({entry['problem']})" for entry in problems[:3]
            )
            self.warn(
                "project.skills",
                f"{len(problems)} skill manifest(s) could not be read: {shown}",
                "fix or remove them; an unreadable manifest is never offered to a worker",
            )
            return
        self.ok(
            "project.skills",
            f"{len(available)} skill manifest(s) readable"
            + (f", {len(pinned)} pinned" if pinned else ""),
        )

    def _check_retained(self, project_id: str) -> None:
        """What this project's tasks still hold, in one pass over the workers.

        `task_retained_resources` rescans every worker in the root for each
        task it is given, so calling it in a loop is quadratic in a root's
        whole history -- and a preflight that gets slower every week is one
        that stops being run. The workers are indexed by task once here and
        each task is handed only its own, which leaves the policy exactly
        where it is enforced and makes the pass linear.
        """
        if not self._state_ok:
            self._unchecked("project.retained", "Helm state could not be read")
            return
        assert self.coordinator is not None
        try:
            data = self.coordinator.store.load()
        except (HelmError, OSError, ValueError) as exc:  # pragma: no cover
            self._unchecked("project.retained", f"state could not be read: {exc}")
            return
        workers_by_task: dict[str, dict[str, Any]] = {}
        for worker_id, worker in data.get("workers", {}).items():
            if isinstance(worker, dict):
                workers_by_task.setdefault(str(worker.get("task_id")), {})[worker_id] = worker
        held: list[str] = []
        holds = 0
        tasks = sorted(
            (
                task
                for task in data.get("tasks", {}).values()
                if isinstance(task, dict) and task.get("project_id") == project_id
            ),
            key=lambda task: str(task.get("id")),
        )
        for task in tasks:
            scoped = {"workers": workers_by_task.get(str(task.get("id")), {})}
            retained = self.coordinator.task_retained_resources(task, scoped)
            if retained:
                held.append(f"{task['id']} holds {', '.join(retained)}")
            if self.coordinator.task_hold(task) is not None:
                holds += 1
        if holds:
            self.warn(
                "project.retained",
                f"{holds} task(s) carry an open approval hold"
                + (f"; {len(held)} task(s) still hold resources" if held else ""),
                "inspect with helm status, then release or repair the hold",
            )
            return
        if held:
            self.warn(
                "project.retained",
                f"{len(held)} task(s) still hold resources: {'; '.join(held[:3])}",
                "run helm task cleanup <task> once the commander approves it",
            )
            return
        self.ok("project.retained", "no task retains a worktree, branch, or worker directory")


def run(
    coordinator: Coordinator,
    helm_root: Path | None,
    project_id: str | None = None,
    *,
    probe_runtimes: bool = False,
) -> Report:
    """Inspect a root, and optionally one project, changing nothing.

    `probe_runtimes` additionally runs each installed agent's `--help` to see
    whether Helm's recorded effort mechanism still exists. Opt-in because
    doctor's whole contract is that it is cheap and starts nothing; spawning
    five CLIs is neither, and a preflight nobody dares run is worthless.
    """
    doctor = _Doctor(coordinator, helm_root)
    doctor.probe_runtimes = probe_runtimes
    if project_id is not None:
        doctor.preload_project(project_id)
    ran = doctor.run_root()
    if project_id is not None:
        if ran:
            doctor.run_project(project_id)
        else:
            doctor._scope = "project"
            for check in PROJECT_CHECKS:
                doctor._unchecked(check, "the root could not be inspected")
    return Report(doctor.root, project_id, doctor.findings)


def report_unopenable_state(
    helm_root: Path | None, detail: str, project_id: str | None = None
) -> Report:
    """A report for a root whose state store could not even be constructed.

    Opening the store is what `helm` does before dispatching any command, so a
    malformed state file used to exit 2 with nothing printed -- the one case
    where an operator most needs a machine-readable answer got none. The checks
    that do not need a store still run; the rest say why they did not.
    """
    doctor = _Doctor(None, helm_root, state_error=detail or "the state store could not be opened")
    if helm_root is None:
        doctor.run_root()
        return Report(doctor.root, project_id, doctor.findings)
    doctor.ok("root.configured", f"Helm root {doctor.root}")
    doctor._check_layout()
    doctor._check_symlinks()
    doctor._check_state()
    doctor._check_boundaries()
    doctor._check_preferences()
    for check in ("root.domains", "root.profiles", "root.runtimes"):
        doctor._unchecked(check, "Helm state could not be opened")
    doctor._check_herdr()
    doctor._unchecked("root.authority", "Helm state could not be opened")
    if project_id is not None:
        doctor._scope = "project"
        for check in PROJECT_CHECKS:
            doctor._unchecked(check, "Helm state could not be opened")
    return Report(doctor.root, project_id, doctor.findings)


# ---------- rendering ----------


def render_text(report: Report) -> list[str]:
    lines = [f"helm doctor: root {report.root or '(none resolved)'}"]
    scope = "root"
    for finding in report.findings:
        if finding.scope != scope:
            scope = finding.scope
            if scope == "project":
                lines.append(f"project {report.project}")
        lines.append(f"  {finding.severity:<8} {finding.id:<18} {finding.message}")
        if finding.remediation:
            lines.append(f"           -> {finding.remediation}")
    counts = report.summary
    lines.append(
        f"{counts[ERROR]} error{'' if counts[ERROR] == 1 else 's'}, "
        f"{counts[WARNING]} warning{'' if counts[WARNING] == 1 else 's'}, "
        f"{counts[OK]} ok"
    )
    return lines


def render_json(report: Report) -> str:
    return json.dumps(report.document(), indent=2, sort_keys=True)
