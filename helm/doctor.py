"""`helm doctor` -- a read-only preflight of a Helm root and one project.

Every precondition a Helm root has is already checked somewhere: the layout at
`helm init`, the preferences file at load, the runtime executable at launch, the
committed Git repository at discovery. Each of those checks fires at the moment
it is needed, which is the moment it is most expensive to be wrong. Doctor runs
them early, together, and changes nothing.

Three properties are load-bearing, and the tests hold each of them:

**It is read-only.** Nothing here initializes, registers, repairs, cleans,
fetches, or discovers. A broken root is reported broken -- `helm init` stays an
explicit human operation, and a project that is not registered stays that way,
because a preflight that quietly fixed what it found would make "is this sound"
unanswerable.

**Absence and breakage are different findings.** An optional capability that is
not installed is not a fault. A requirement this root *named* -- in
`agent.default`, in a model-family restriction, in `HELM_AGENT`, in a configured
profile, in a project's pin -- that cannot be satisfied is, because something
already decided to depend on it. Collapsing the two makes the report noise.

**It never touches a secret.** Doctor reads no credential store, prints no
environment *value*, and runs no provider auth, status, or catalogue command.
Runtime readiness here is executable presence and says so; it is deliberately
not credential readiness.

The contract -- check ids, severities, output shape, exit codes -- is in
`docs/doctor.md`. Checks reuse the existing loaders and validators rather than
restating policy; a check with a rule of its own would be a rule that had
drifted from where it is enforced.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import preferences as prefs
from . import runtimes
from .core import Coordinator, HelmError, SafetyError, canonical, overlaps

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


# ---------- small read-only helpers ----------


def _git_lines(root: Path, *args: str) -> list[str] | None:
    """Local, non-interactive git output, or None when git could not answer.

    Never fetches and never writes: doctor's whole Git surface is queries about
    a checkout that is already on disk.
    """
    env = dict(os.environ)
    env["GIT_TERMINAL_PROMPT"] = "0"
    try:
        result = subprocess.run(
            ["git", "-C", str(root), *args],
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


def _world_readable(path: Path) -> bool:
    try:
        return bool(path.stat().st_mode & 0o077)
    except OSError:
        return False


# ---------- the checks ----------


class _Doctor:
    """Accumulates findings in declared order. One method per check id."""

    def __init__(self, coordinator: Coordinator, helm_root: Path | None) -> None:
        self.coordinator = coordinator
        self.root = canonical(helm_root) if helm_root is not None else None
        self.findings: list[Finding] = []
        self._scope = "root"

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
        self._check_authority()
        return True

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
        assert self.root is not None
        linked = sorted(
            name
            for name in (*_REQUIRED_LAYOUT, *_OPTIONAL_LAYOUT)
            if (self.root / name).is_symlink()
        )
        if linked:
            self.error(
                "root.symlinks",
                f"Helm-owned root director{'ies' if len(linked) > 1 else 'y'} "
                f"{', '.join(linked)}/ {'are' if len(linked) > 1 else 'is'} a symlink",
                "replace the symlink with a real directory; a link can point at "
                "state another root owns",
            )
            return
        self.ok("root.symlinks", "no Helm-owned root directory is a symlink")

    def _check_state(self) -> None:
        store = self.coordinator.store
        try:
            data = store.load()
        except (HelmError, OSError) as exc:
            self.error(
                "root.state",
                f"Helm state cannot be read: {exc}",
                "restore or remove the state file; nothing else here is trustworthy "
                "until it opens",
            )
            return
        exposed = [
            str(path)
            for path in (store.directory, store.state_file)
            if path.exists() and _world_readable(path)
        ]
        if exposed:
            self.warn(
                "root.state",
                f"Helm state is readable beyond its owner: {', '.join(exposed)}",
                "chmod 700 the state directory and 600 the state file",
            )
            return
        self.ok(
            "root.state",
            f"state opens at {store.directory} "
            f"({len(data.get('projects', {}))} project(s), {len(data.get('tasks', {}))} task(s))",
        )

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
        leaked = sorted(entry for entry in tracked if Path(entry).name != ".gitkeep")
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

    def _check_preferences(self) -> None:
        path = prefs.preferences_path(self.root)
        try:
            loaded = prefs.load(path)
        except prefs.PreferencesError as exc:
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
        domain_root = self.root / "domains"
        if not domain_root.is_dir():
            self.warn(
                "root.domains",
                "no domains/ directory, so no shared knowledge can be composed",
                f"run helm init {self.root}, or add domains/<id>/knowledge.md",
            )
            return
        broken: list[str] = []
        thin: list[str] = []
        names = sorted(
            entry.name
            for entry in domain_root.iterdir()
            if entry.is_dir() and not entry.is_symlink()
        )
        for name in names:
            try:
                self.coordinator._domain_chain(domain_root, name)
            except (HelmError, SafetyError) as exc:
                broken.append(f"{name} ({exc})")
                continue
            if not (domain_root / name / "knowledge.md").is_file():
                thin.append(name)
        if broken:
            self.error(
                "root.domains",
                f"{len(broken)} domain(s) are invalid: {'; '.join(broken[:3])}",
                "fix the named domain.json manifests; an invalid extends chain "
                "stops every task that resolves that domain",
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

    def _check_profiles(self) -> None:
        try:
            profiles = self.coordinator.list_agent_profiles()
        except (HelmError, SafetyError) as exc:
            self.error(
                "root.profiles",
                f"configured agent profiles cannot be read: {exc}",
                "fix agents.json or agents/<id>/profile.json",
            )
            return
        if not profiles:
            self.ok(
                "root.profiles",
                "no configured agent profiles; the built-in runtimes are used",
            )
            return
        unlaunchable: list[str] = []
        for profile in profiles:
            resolved = self.coordinator._resolve_profile(profile, interactive=True)
            command = resolved.get("command")
            if not command:
                unlaunchable.append(f"{profile['id']} (no launch command)")
                continue
            available, reason = self.coordinator._check_command(command)
            if not available:
                unlaunchable.append(f"{profile['id']} ({reason})")
        if unlaunchable:
            self.error(
                "root.profiles",
                f"{len(unlaunchable)} configured profile(s) cannot launch: "
                + "; ".join(sorted(unlaunchable)[:3]),
                "install the named executable or correct the profile's command",
            )
            return
        self.ok(
            "root.profiles",
            f"{len(profiles)} configured profile(s) name a launchable executable "
            "(executable presence only, not credential readiness)",
        )

    def named_runtimes(self) -> dict[str, list[str]]:
        """Runtime id -> what named it. Only these are requirements."""
        named: dict[str, list[str]] = {}

        def name(runtime_id: str | None, source: str) -> None:
            if not runtime_id or runtime_id == "none":
                return
            named.setdefault(runtime_id, []).append(source)

        try:
            loaded = self.coordinator.preferences()
        except (HelmError, prefs.PreferencesError):
            loaded = prefs.EMPTY
        name(loaded.default_agent, "preference agent.default")
        for family, allowed in sorted(loaded.model_runtimes.items()):
            for runtime_id in sorted(allowed):
                name(runtime_id, f"preference model.runtimes.{family}")
        name(os.environ.get("HELM_AGENT", "").strip() or None, "HELM_AGENT")
        return named

    def _check_runtimes(self) -> None:
        named = self.named_runtimes()
        try:
            excluded = self.coordinator.excluded_agents()
        except (HelmError, SafetyError):
            excluded = set()
        try:
            profile_ids = {profile["id"] for profile in self.coordinator.list_agent_profiles()}
        except (HelmError, SafetyError):
            profile_ids = set()

        broken: list[str] = []
        for runtime_id in sorted(named):
            sources = ", ".join(sorted(set(named[runtime_id])))
            runtime = runtimes.builtin_runtime(runtime_id)
            if runtime is None:
                if runtime_id in profile_ids:
                    continue  # covered by root.profiles
                broken.append(f"{runtime_id} is not a known runtime ({sources})")
                continue
            if runtime_id in excluded:
                broken.append(f"{runtime_id} is named by {sources} but this root excludes it")
                continue
            available, reason = self.coordinator._check_command(
                runtime.command(interactive=True)
            )
            if not available:
                broken.append(f"{runtime_id} named by {sources}: {reason}")
        if broken:
            self.error(
                "root.runtimes",
                f"{len(broken)} named runtime(s) are unusable: " + "; ".join(broken[:3]),
                "install the runtime, name a different one, or clear the preference "
                "with helm prefs unset",
            )
            return

        launchable = sorted(
            entry["id"]
            for entry in self.coordinator.builtin_runtime_availability()
            if entry["available"] and entry["id"] not in excluded
        )
        if not launchable:
            self.warn(
                "root.runtimes",
                "no built-in agent runtime is launchable on this machine",
                "install an agent CLI, or configure a profile in agents.json; "
                "delegation needs something to start",
            )
            return
        detail = "named runtimes usable; " if named else ""
        self.ok(
            "root.runtimes",
            f"{detail}{len(launchable)} built-in runtime(s) launchable: "
            f"{', '.join(launchable)} (executable presence only, not credential "
            "or catalogue readiness)",
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
        configured = bool(self.coordinator._authority_hash())
        self.ok(
            "root.authority",
            "protected commands require a capability"
            if configured
            else "protected commands are guarded by session role only "
            "(helm authority init adds a capability)",
        )

    # ---------- project ----------

    def run_project(self, project_id: str) -> None:
        self._scope = "project"
        assert self.root is not None
        project_root = self.root / "projects" / project_id
        if not self._check_location(project_id, project_root):
            return
        if not self._check_git(project_root):
            return
        self._check_isolation(project_id, project_root)
        settings = self._check_config(project_root)
        if settings is None:
            return
        self._check_base_branch(project_root, settings)
        self._check_project_domains(settings)
        self._check_skills(project_id, project_root, settings)
        self._check_retained(project_id)

    def _check_location(self, project_id: str, project_root: Path) -> bool:
        if project_root.is_symlink():
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
        self.ok("project.location", f"{project_root} is a direct child of projects/")
        return True

    def _check_git(self, project_root: Path) -> bool:
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
        dirty = _git_lines(project_root, "status", "--porcelain=v1", "--untracked-files=no")
        if dirty:
            self.warn(
                "project.git",
                "the project checkout has uncommitted changes to tracked files",
                "commit or stash them; a task's base is pinned from this checkout",
            )
            return True
        self.ok("project.git", "a committed Git repository with a clean checkout")
        return True

    def _check_isolation(self, project_id: str, project_root: Path) -> None:
        resolved = canonical(project_root)
        conflicts: list[str] = []
        try:
            registered = self.coordinator.list_projects()
        except (HelmError, SafetyError):
            registered = []
        for other in registered:
            if other["id"] == project_id:
                continue
            try:
                other_root = canonical(other["root"])
            except OSError:  # pragma: no cover - canonical rarely raises
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
        try:
            settings = self.coordinator._discovery_settings(project_root)
        except (HelmError, SafetyError) as exc:
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
        that hangs on an unreachable remote is a preflight nobody runs. Where a
        local answer is not available the finding says exactly that.
        """
        declared = settings.get("base_branch")
        if not declared:
            record = None
            try:
                record = self.coordinator.store.load()["projects"].get(project_root.name)
            except (HelmError, OSError):
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
        current = _git_lines(project_root, "symbolic-ref", "--quiet", "--short", "HEAD")
        remotes = _git_lines(project_root, "remote") or []
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
            + (" (the project has a remote, so the checked-out branch is not evidence)"
               if remotes else " (the checkout is detached)"),
            'name one explicitly with "base_branch" in .helm/project.json',
        )

    def _check_project_domains(self, settings: dict[str, Any]) -> None:
        assert self.root is not None
        declared = list(settings.get("domains") or [])
        if not declared:
            self.ok(
                "project.domains",
                "no default domain is declared; tasks resolve one explicitly",
            )
            return
        domain_root = self.root / "domains"
        missing = [name for name in declared if not (domain_root / name).is_dir()]
        if missing:
            self.error(
                "project.domains",
                f"declared domain(s) do not exist: {', '.join(sorted(missing))}",
                "add the domain under domains/, or correct the project's "
                '"domains" setting',
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
        try:
            discovered = self.coordinator.discover_skills(project, settings.get("agent"))
        except (HelmError, SafetyError) as exc:
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
        try:
            data = self.coordinator.store.load()
        except (HelmError, OSError) as exc:  # pragma: no cover - root.state reports it
            self.warn(
                "project.retained",
                f"retained resources cannot be read: {exc}",
                "see root.state",
            )
            return
        held: list[str] = []
        holds = 0
        for task in sorted(
            (t for t in data.get("tasks", {}).values() if t.get("project_id") == project_id),
            key=lambda t: str(t.get("id")),
        ):
            retained = self.coordinator.task_retained_resources(task, data)
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
    coordinator: Coordinator, helm_root: Path | None, project_id: str | None = None
) -> Report:
    """Inspect a root, and optionally one project, changing nothing."""
    doctor = _Doctor(coordinator, helm_root)
    if doctor.run_root() and project_id is not None:
        doctor.run_project(project_id)
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
