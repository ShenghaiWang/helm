"""The lifecycle of a project and a task, and the isolation of its workspace.

A mixin over `CoordinatorBase`. Registering a project, discovering one under
`projects/`, creating a task, allocating its worktree, and enforcing the
boundary that worktree is supposed to be -- one subject, previously split
across two files by accident rather than by design.

`create_task` and `allocate_task` lived in `skills`, which meant describing
that module honestly gave you "domains AND skills AND runtime/model/effort
resolution AND task allocation". Task allocation is not a skill; it shared a
file because both needed the project record. Isolation came from `core`,
where `register_project` and `_verify_workspace_record` sat apart from the
`allocate_task` that creates the very workspace they verify -- so the rule and
its enforcement could drift without either file looking wrong.

Moved verbatim; it imports nothing from `helm.core`.
"""

from __future__ import annotations

import contextlib
import os
import re
import stat
from pathlib import Path
from typing import Any

from .. import git
from ..errors import HelmError, SafetyError
from ..git import (
    _StaleBaseResolution,
    _git,
    _git_common_dir,
    _git_root,
    _has_head,
    _resolve_base_branch,
)
from ..paths import _private_dir, canonical, inside, overlaps
from ..values import (
    DELIVERY_POLICIES,
    GATE_TYPES,
    TASK_ROLES,
    WORKTREELESS_ROLES,
    _ROLE_DIRECTORY,
    _color_for,
    _safe_text,
    _validate_agent_id,
    _validate_branch_name,
    _validate_effort,
    _validate_shape,
    _validate_model_id,
    _validate_project_id,
    _validate_ticket_id,
    new_id,
    now,
    task_branch_name,
)


class LifecycleMixin:
    """Projects, tasks, worktrees, and the isolation between them."""

    def register_project(
        self,
        name: str,
        root: str,
        *,
        project_id: str | None = None,
        delivery_policy: str = "local",
        init_git: bool = False,
        confirm: bool = False,
        color: str | None = None,
        label: str | None = None,
        discovered: bool = False,
    ) -> dict[str, Any]:
        name = _safe_text(label if label is not None else name).strip()
        if not name:
            raise HelmError("project name is required")
        if delivery_policy not in DELIVERY_POLICIES:
            raise HelmError("delivery policy must be 'local' or 'pr'")
        if color is not None and not re.fullmatch(r"#[0-9A-Fa-f]{6}", color):
            raise HelmError("project color must be a six-digit hex value such as #2563eb")
        requested_project_path = Path(root).expanduser()
        if requested_project_path.is_symlink():
            raise SafetyError(f"project root must not be a symlink: {requested_project_path}")
        requested_project_root = requested_project_path.absolute()
        project_root = canonical(root)
        if not project_root.is_dir():
            raise HelmError(f"project root is not a directory: {project_root}")
        configured_root = self.store.configured_root()
        if configured_root is not None:
            owned_roots = [
                configured_root / "state",
                configured_root / "domains",
                configured_root / "agents",
            ]
            if any(
                overlaps(requested_project_root, owned) or overlaps(project_root, owned)
                for owned in owned_roots
            ):
                raise SafetyError(
                    "project root cannot overlap Helm-owned state, domains, or agents trees"
                )
            projects_root = configured_root / "projects"
            if inside(project_root, projects_root) and project_root.parent != projects_root:
                raise SafetyError(
                    f"project root must be a direct child of Helm's projects directory: {project_root}"
                )
        if inside(self.store.directory, project_root):
            raise SafetyError("project root cannot contain Helm's state directory")

        pid = _validate_project_id(project_id or (project_root.name if discovered else new_id("p")))
        project_settings = self._discovery_settings(project_root)
        repo_root = _git_root(project_root)
        if repo_root is None:
            if not init_git or not confirm:
                raise SafetyError(
                    f"{project_root} is not a Git project; automatic discovery never initializes Git. "
                    f"To opt in, run: helm project add {pid} {project_root} --init-git --confirm"
                )
            _git(project_root, "init")
            # A worktree needs a commit. Do not stage user files implicitly;
            # create a clearly local, empty bootstrap commit instead.
            if not _has_head(project_root):
                _git(
                    project_root,
                    "-c",
                    "user.name=Helm",
                    "-c",
                    "user.email=helm@localhost",
                    "commit",
                    "--allow-empty",
                    "-m",
                    "Initialize Helm project",
                )
            repo_root = _git_root(project_root)
        if repo_root != project_root:
            raise SafetyError(
                f"registered root must be the Git repository root ({repo_root}), not {project_root}"
            )
        if not _has_head(project_root):
            raise HelmError("Git project has no commit; create an initial commit before registering it")

        branch = _resolve_base_branch(project_root, project_settings)
        common_dir = _git_common_dir(project_root)
        with self.store.locked() as data:
            for existing in data["projects"].values():
                existing_root = canonical(existing["root"])
                if overlaps(existing_root, project_root):
                    raise SafetyError(
                        f"project roots overlap: {existing['id']} ({existing_root}) and {project_root}"
                    )
                existing_common = canonical(existing.get("git_common_dir", "")) if existing.get("git_common_dir") else _git_common_dir(existing_root)
                if existing_common == common_dir:
                    raise SafetyError(
                        f"project uses the same Git repository as {existing['id']}; register one project identity per repository"
                    )
            if pid in data["projects"]:
                raise HelmError(f"project id already exists: {pid}")
            record = {
                "id": pid,
                "name": name,
                "label": name,
                "root": str(project_root),
                "delivery_policy": delivery_policy,
                "color": color
                or _color_for(
                    pid,
                    [other.get("color", "") for other in data["projects"].values()],
                ),
                "base_branch": branch,
                "git_common_dir": str(common_dir),
                "discovered": discovered,
                "domains": project_settings.get("domains", []),
                "agent": project_settings.get("agent"),
                "model": project_settings.get("model"),
                "created_at": now(),
            }
            data["projects"][pid] = record
            return record
    def _verify_workspace_record(
        self,
        data: dict[str, Any],
        project: dict[str, Any],
        task: dict[str, Any],
    ) -> Path:
        expected = canonical(task["workspace"])
        if task.get("workspace_removed"):
            raise SafetyError("task workspace has been cleaned")
        if not expected.exists() or not expected.is_dir():
            raise SafetyError(f"assigned task workspace is missing: {expected}")
        if canonical(project["root"]) == expected:
            raise SafetyError("task workspace cannot be the project root")
        for other in data["projects"].values():
            if other["id"] != project["id"] and overlaps(expected, canonical(other["root"])):
                raise SafetyError("task workspace overlaps another registered project")
        if task.get("role") in WORKTREELESS_ROLES:
            # No worktree and no branch to match, so the isolation that still
            # applies is where the directory is: somewhere Helm owns, never a
            # project or a user's checkout.
            if not inside(expected, self.store.directory):
                raise SafetyError(f"foreman workspace must be Helm-owned state: {expected}")
            return expected
        actual_root = _git_root(expected)
        if actual_root != expected:
            raise SafetyError(f"workspace is not a Git worktree at the assigned path: {expected}")
        common = _git_common_dir(expected)
        expected_common = canonical(project.get("git_common_dir", _git_common_dir(canonical(project["root"]))))
        if common != expected_common:
            raise SafetyError("workspace belongs to a different Git project")
        branch = _git(expected, "symbolic-ref", "--quiet", "--short", "HEAD", check=False)
        if branch != task["branch"]:
            raise SafetyError("workspace branch does not match the assigned task")
        return expected
    def verify_task_workspace(self, task_id: str) -> Path:
        data = self.store.load()
        task = self._task(data, task_id)
        project = self._project(data, task["project_id"])
        return self._verify_workspace_record(data, project, task)
    def ensure_discovered_project(
        self,
        project_id: str,
        root: str | os.PathLike[str],
        *,
        helm_root: str | os.PathLike[str],
        data: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Create or reuse the internal record for one projects/<id> child.

        Takes an optional snapshot so a caller sweeping every project pays one
        read rather than one per child. It is only ever used for the read
        checks; anything that writes takes the lock and reads again under it.
        """
        pid = _validate_project_id(project_id)
        configured_root = canonical(helm_root)
        project_root = canonical(root)
        projects_root = configured_root / "projects"
        if project_root.parent != projects_root:
            raise SafetyError(
                f"discovered project must be an isolated direct child of {projects_root}: {project_root}"
            )
        settings = self._discovery_settings(project_root)
        repo_root = _git_root(project_root)
        if repo_root is None:
            raise SafetyError(
                f"{project_root} is not a Git project; automatic discovery never initializes Git. "
                f"To opt in, run: helm project add {pid} {project_root} --init-git --confirm"
            )
        if repo_root != project_root:
            raise SafetyError(
                f"discovered project {pid} is not an isolated Git repository root; "
                f"the repository root is {repo_root}"
            )
        if not _has_head(project_root):
            raise HelmError(
                f"discovered project {pid} has no commit; create an initial commit before running it"
            )

        data = self.store.load() if data is None else data
        existing = data["projects"].get(pid)
        if existing is not None:
            existing_root = canonical(existing["root"])
            if existing_root != project_root:
                raise SafetyError(
                    f"project id already exists for a different root: {pid} ({existing_root})"
                )
            for other in data["projects"].values():
                if other["id"] != pid and overlaps(project_root, canonical(other["root"])):
                    raise SafetyError(
                        f"project roots overlap: {other['id']} ({other['root']}) and {project_root}"
                    )
            if settings:
                # What this project's own file says its record should hold.
                # `label` is the one that is not a straight copy: it names two
                # fields, because the record carries both.
                #
                # Only an explicit setting updates a recorded base branch. An
                # already-registered project keeps whatever base it was
                # registered with even if the checkout later switches branches
                # -- re-guessing one on every discovery pass is exactly the
                # "whatever HEAD happens to be" behavior this setting exists
                # to replace.
                wanted: dict[str, Any] = {}
                for key in (
                    "delivery_policy", "color", "domains", "agent",
                    "model", "foreman", "review", "base_branch",
                ):
                    if key in settings:
                        wanted[key] = settings[key]
                if "label" in settings:
                    wanted["name"] = settings["label"]
                    wanted["label"] = settings["label"]
                # Take the lock only when it would change something. Discovery
                # runs at the head of most commands and re-applied these every
                # time, so an unchanged root still paid a lock acquisition and
                # two full serialisations of a tens-of-megabytes document per
                # project to write back what was already there. `locked` was
                # comparing and correctly declining to save -- the cost was
                # reaching that verdict, not the write. Reading a project's own
                # settings must not be a write path.
                if any(existing.get(key) != value for key, value in wanted.items()):
                    with self.store.locked() as current:
                        record = current["projects"][pid]
                        record.update(wanted)
                        existing = record
                else:
                    existing = dict(existing)
                    existing.update(wanted)
            return existing

        for other in data["projects"].values():
            if canonical(other["root"]) == project_root:
                raise SafetyError(
                    f"project root is already registered as {other['id']}; use that project id"
                )
        return self.register_project(
            settings.get("label", pid),
            str(project_root),
            project_id=pid,
            delivery_policy=settings.get("delivery_policy", "local"),
            color=settings.get("color"),
            label=settings.get("label"),
            discovered=True,
        )
    def discover_projects(
        self, helm_root: str | os.PathLike[str] | None = None
    ) -> list[dict[str, Any]]:
        """Discover and persist every direct project child under a Helm root."""
        configured_root = canonical(helm_root) if helm_root is not None else self.store.configured_root()
        if configured_root is None:
            raise HelmError("no Helm root is configured; run helm init first")
        projects_root = configured_root / "projects"
        if not projects_root.is_dir():
            raise HelmError(f"Helm root is not initialized; run helm init in {configured_root}")
        discovered: list[dict[str, Any]] = []
        # One snapshot for the sweep. Discovery runs at the head of most
        # commands and every child re-read the whole document to answer
        # "do I already know this project", which on a mature root is tens of
        # megabytes parsed once per directory entry.
        #
        # It is refreshed whenever a project is actually registered, and that
        # is not an optimisation detail: the check below it refuses roots that
        # overlap an already-known project, so running it against a snapshot
        # taken before a sibling was added would stop seeing the sibling. A
        # safety check is only worth what it can see, so the read is only
        # reused while nothing has changed underneath it.
        data = self.store.load()
        for entry in sorted(projects_root.iterdir(), key=lambda item: item.name):
            if not entry.is_dir():
                continue
            if entry.is_symlink():
                raise SafetyError(f"discovered project must not be a symlink: {entry}")
            known = entry.name in data.get("projects", {})
            discovered.append(
                self.ensure_discovered_project(
                    entry.name,
                    entry,
                    helm_root=configured_root,
                    data=data,
                )
            )
            if not known:
                data = self.store.load()
        return discovered

    def discover_project(
        self,
        helm_root: str | os.PathLike[str] | None = None,
        project_id: str | None = None,
    ) -> dict[str, Any]:
        """Discover one project after scanning the root for overlap conflicts."""
        if project_id is None:
            if helm_root is None:
                raise HelmError("project id is required")
            project_id = str(helm_root)
            helm_root = None
        projects = self.discover_projects(helm_root)
        configured_root = canonical(helm_root) if helm_root is not None else self.store.configured_root()
        if configured_root is None:
            raise HelmError("no Helm root is configured; run helm init first")
        for project in projects:
            if project["id"] == project_id:
                return project
        raise HelmError(
            f"unknown project {project_id}; expected a Git project at "
            f"{configured_root / 'projects' / project_id}"
        )
    def get_project(self, project_id: str) -> dict[str, Any]:
        """One registered project, or an error naming the unknown id."""
        return dict(self._project(self.store.load(), project_id))
    def list_projects(self, *, data: dict[str, Any] | None = None) -> list[dict[str, Any]]:
        data = data if data is not None else self.store.load()
        return sorted(data["projects"].values(), key=lambda item: item["created_at"])
    def reshape_task(self, task_id: str, shape: str, *, reason: str = "") -> dict[str, Any]:
        """Change a task's shape, keeping the old one and why it changed on the record.

        A review's shape check can say the foreman's word was wrong; this is
        how it is put right before approval, and the history is kept so a
        re-shape to `small` after a critical finding is visible for what it is.
        """
        shape = _validate_shape(shape, "task")
        with self.store.locked() as data:
            task = self._task(data, task_id)
            previous = task.get("shape") or "standard"
            task.setdefault("shape_history", []).append({
                "from": previous, "to": shape, "reason": _safe_text(reason).strip(), "at": now(),
            })
            task["shape"] = shape
            if reason:
                task["shape_reason"] = _safe_text(reason).strip()
            return dict(task)

    @staticmethod
    def _open_task_for_ticket(
        data: dict[str, Any], project: dict[str, Any], ticket: str, base_branch: str | None
    ) -> dict[str, Any] | None:
        """The worker task already holding this ticket's worktree on this base, if any."""
        candidates = [
            task for task in data.get("tasks", {}).values()
            if task.get("project_id") == project["id"]
            and task.get("role") == "worker"
            and not task.get("read_only")
            and task.get("ticket") == ticket
            and task.get("base_branch") == base_branch
            and task.get("status") not in ("merged", "pr-merged")
            and task.get("workspace") and Path(task["workspace"]).is_dir()
        ]
        if not candidates:
            return None
        return max(candidates, key=lambda task: str(task.get("created_at") or ""))

    def create_task(
        self,
        project_id: str,
        brief: str,
        *,
        delivery_policy: str | None = None,
        domain: str | None = None,
        agent: str | None = None,
        model: str | None = None,
        effort: str | None = None,
        no_domain: bool = False,
        role: str = "worker",
        reviews: str | None = None,
        ticket: str | None = None,
        read_only: bool = False,
        base: str | None = None,
        new: bool = False,
        shape: str | None = None,
        shape_reason: str | None = None,
    ) -> dict[str, Any]:
        brief = _safe_text(brief).strip()
        if not brief:
            raise HelmError("task brief is required")
        if role not in TASK_ROLES:
            raise HelmError(f"task role must be one of {sorted(TASK_ROLES)}")
        # Validated before it is used, not after: this value goes into a git
        # ref, so an unusable one must fail here rather than at worktree
        # creation with a git error nobody can map back to the input.
        ticket = _validate_ticket_id(ticket, "task") if ticket else None
        model = _validate_model_id(model, "task") if model else None
        shape = _validate_shape(shape, "task") if shape else "standard"
        # Same reason as the ticket above: this becomes a git ref, so a bad
        # name fails here rather than deep in worktree creation. A task-level
        # base is for the one-off -- a fix that must sit on a release branch
        # while every other task on the project still starts from its
        # configured default. Pinning every task belongs in the project's own
        # `base_branch`, not in a flag repeated by hand.
        base = _validate_branch_name(base, "--base") if base else None
        if agent is not None:
            _validate_agent_id(agent, "--agent")
        if agent is not None and effort:
            # Both named here, so the pairing is knowable NOW. It used to be
            # checked only at launch, which created a task that could never
            # start: --agent cursor --effort low was accepted, recorded, and
            # then refused every time, with no command to clear an effort off
            # an existing task. A foreman burned three launch attempts on one.
            # Refusing at creation is the same rule enforced where the caller
            # can still act on it -- the launch check stays, for the runtimes
            # that are only resolved there.
            self._require_effort_supported(effort, agent, "task")

        worktree_backed = role not in WORKTREELESS_ROLES
        # Bounded retry, not a loop that can spin forever: a fetch runs
        # outside Helm's state lock (see phase 2 below), so the project's own
        # configuration can change while it runs. Phase 3 below re-checks
        # that against what phase 1 resolved and raises _StaleBaseResolution
        # to redo the whole resolution rather than silently trusting a base
        # computed for a configuration the project no longer has.
        for attempt in range(3):
            # Phase 1: resolve everything that needs Helm's own state, under
            # its lock. Nothing here touches the network or mutates project
            # state -- learning a default domain is deferred to phase 3, so a
            # fetch failure or a stale-config retry never leaves that side
            # effect behind for a task that was never created.
            with self.store.locked() as data:
                project = self._project(data, project_id)
                if role == "worker" and not read_only and ticket and not new:
                    # One ticket, one worktree. A foreman that creates a task
                    # per round -- or a new task for every retry -- gives one
                    # ticket a dozen checkouts, each with its own install and
                    # cold build; on this root 32 of 85 tickets had more than
                    # one, and one had twelve. The round belongs on the task
                    # that already holds the branch: `task continue`, or
                    # `task reopen` first for one that failed. A deliberate
                    # second line of work says so with --new.
                    prior = self._open_task_for_ticket(
                        data, project, ticket, base or project.get("base_branch")
                    )
                    if prior is not None:
                        verb = (
                            f"helm task reopen {prior['id']}, then "
                            if prior.get("status") in ("failed", "blocked") else ""
                        )
                        raise HelmError(
                            f"ticket {ticket} already has a task on "
                            f"{prior.get('base_branch')} in {project_id}: {prior['id']} "
                            f"[{prior.get('status')}], worktree still present. Run the "
                            f"next round on it -- {verb}helm task continue {prior['id']} "
                            "--state-changing|--read-only -- so the ticket keeps one "
                            "worktree and one branch. Pass --new only for a deliberate "
                            "second line of work."
                        )
                # Scoped to a foreman caller: the gate stops an agent driving
                # a project from launching state-changing work unconfirmed. A
                # root/commander call already carries full authority -- the
                # same reason root skips every other foreman-only restriction
                # in `_authority_refusal` -- so root creating a task directly
                # (`helm run`, `helm task create`, or a test's direct call)
                # is not gated behind its own confirmation.
                if role == "worker" and not read_only and self.caller_role() == "foreman":
                    self._require_gates_confirmed(data, project_id)
                # A project that has declined review declines it for every
                # caller. This sits in creation, not in the review command,
                # because a reviewer task is a reviewer task whichever door
                # it came through -- and because the last unnecessary
                # reviewer was launched by a foreman correctly weighing a
                # prose line that had gone stale. Policy a commander has
                # ruled on is recorded as a setting and enforced here, so it
                # cannot be re-litigated by anything downstream.
                if role == "reviewer" and not self._project_wants_review(project):
                    raise HelmError(
                        f"project {project_id} has declined independent review"
                        ' ("review": false in its .helm/project.json).'
                        " Merge on the worker's own evidence, or lift the"
                        " setting if the commander wants this reviewed."
                    )
                selected_domain, domain_reason = self.resolve_domain(
                    project, brief, explicit=domain, no_domain=no_domain
                )
                learn_default = (
                    selected_domain is not None
                    and not no_domain
                    and not self._project_domains(project)
                )
                policy = delivery_policy or project["delivery_policy"]
                if policy not in DELIVERY_POLICIES:
                    raise HelmError("delivery policy must be 'local' or 'pr'")
                root = canonical(project["root"])
                if _git_root(root) != root:
                    raise SafetyError("registered project root is no longer the Git repository root")
                if not _has_head(root):
                    raise HelmError("project has no commit from which to allocate a worktree")
                snapshot_root = project["root"]
                snapshot_project_base = project["base_branch"]
                # An explicit --base overrides the project's configured
                # default for this one task. Both are kept: the effective
                # base is what phase 2 resolves against, and the project's
                # own value is what phase 3 re-checks for a concurrent edit.
                snapshot_base_branch = base or snapshot_project_base

            # Phase 2: resolve the immutable base this task starts from. This
            # is deliberately outside Helm's state lock: a worktree-backed
            # task may fetch here, and a fetch is a network call. The state
            # lock is what every worker's protocol message waits on next, so
            # a slow or unreachable remote must not hold it hostage.
            # Resolution reads and, when fetching, fetches -- it never
            # switches, resets, rebases, merges, or otherwise touches the
            # project's own checkout.
            base_info = git._resolve_task_base(
                root,
                snapshot_base_branch,
                fetch=worktree_backed,
                local_delivery=policy == "local",
            )

            # Phase 3: write the task record. The project is re-read fresh
            # and checked against what phase 1 resolved against -- a
            # concurrent edit to the project's root or configured base branch
            # while the fetch above was running must not silently commit a
            # task built against the configuration that no longer applies.
            try:
                with self.store.locked() as data:
                    project = self._project(data, project_id)
                    # The project's base_branch is only worth re-checking
                    # when this task actually derived its base from it. With
                    # an explicit --base, a concurrent edit to the project
                    # default changes nothing about what was resolved, and
                    # failing on it would be a retry for no reason.
                    if project["root"] != snapshot_root or (
                        base is None and project["base_branch"] != snapshot_project_base
                    ):
                        raise _StaleBaseResolution()
                    if learn_default and not self._project_domains(project):
                        # Learn the project's default from the first domain
                        # actually chosen for it, so nobody names one again.
                        # Domain knowledge is supposed to attach by itself; a
                        # default that exists but is never populated means
                        # every task falls back to --domain or to no domain
                        # at all, which ships a worker with no code review,
                        # verification, or definition of done.
                        #
                        # The evidence is a decision already made on THIS
                        # project by something that read the task and the
                        # domain catalogue -- not words in a brief, which is
                        # what routed a video script to the software domain,
                        # and not the shape of the repository, which would do
                        # the same to a video project that happens to hold a
                        # Python file. One prior judgement, reused.
                        project["domains"] = [selected_domain]
                        domain_reason = (
                            f"{domain_reason}; recorded as this project's default "
                            f"(change it with helm project domain {project['id']} <domain-id>)"
                        )
                    task_id = new_id("t")
                    if role == "worker" and not read_only and self.caller_role() == "foreman":
                        # The authoritative check-and-consume: phase 1 above
                        # already fast-failed on an unsettled or already-spent
                        # pair before paying for a network fetch, but only
                        # this pass, holding the lock at the moment the task
                        # id it binds to actually exists, can consume it
                        # without a window where two concurrent creates both
                        # see the pair as free. A failed launch after this
                        # point still leaves the binding on this task's own
                        # id in the foreman's gates record -- an auditable
                        # trail, not a silently spent authorization, since
                        # the task that spent it is right there to inspect.
                        self._require_gates_confirmed(
                            data, project_id, consume_for_task_id=task_id
                        )
                    elif role == "worker" and not read_only and self.caller_role() == "root":
                        self._consume_gates_if_settled(data, project_id, task_id)
                    if role in WORKTREELESS_ROLES:
                        # These roles drive or read; they never edit. Handing
                        # one a checkout and a task branch invites it to do
                        # the work itself, and leaves a branch to shed for a
                        # task that never had a change in it. They read the
                        # project through project.root in their context,
                        # which needs no worktree.
                        branch = None
                        workspace = self.store.directory / _ROLE_DIRECTORY[role] / project_id / task_id
                    else:
                        # The ticket goes in the human-facing names because
                        # those are the places reviewers and coordinators
                        # actually scan. The task id stays in both names: it
                        # is what Helm routes worktrees and cleanup by, and it
                        # keeps retries for one ticket distinct.
                        branch = task_branch_name(project_id, task_id, ticket)
                        workspace_name = f"{ticket}-{task_id}" if ticket else task_id
                        workspace = self.store.directory / "worktrees" / project_id / workspace_name
                    task = {
                        "id": task_id,
                        "project_id": project_id,
                        "role": role,
                        "brief": brief,
                        "delivery_policy": policy,
                        "domain": selected_domain,
                        "domain_selection": domain_reason,
                        "agent_override": agent,
                        "agent_id": None,
                        "agent_reason": None,
                        # The model this task asked for, if any. Separate from
                        # the runtime: naming a model does not name the agent
                        # that runs it, and either can be stated without the
                        # other.
                        "model": model,
                        # For a reviewer task, the task it reviews. Without
                        # this link a reviewer is only discoverable by
                        # reading its brief, so two drivers -- a coordinator
                        # and a project's foreman, or two foremen -- each
                        # start one and neither can see the other's.
                        "reviews": reviews,
                        # The tracker id this task implements, if any.
                        # Recorded as well as put in the branch so a reader
                        # does not have to parse it back out of a ref.
                        "ticket": ticket,
                        # The base this task started from, resolved once in
                        # phase 2 and immutable from here on: allocate_task
                        # must build the worktree/branch from base_revision,
                        # never from project HEAD, so nothing that moves the
                        # project's own checkout between this call and
                        # allocation can change what the task is built on.
                        # base_source/base_upstream/base_fetched/
                        # base_resolved_at/base_notes are the evidence a
                        # later review reconstructs this from.
                        "base_branch": base_info["base_branch"],
                        "base_revision": base_info["base_revision"],
                        "base_source": base_info["base_source"],
                        "base_upstream": base_info["base_upstream"],
                        "base_fetched": base_info["base_fetched"],
                        "base_resolved_at": base_info["base_resolved_at"],
                        "base_notes": base_info.get("base_notes", []),
                        "branch": branch,
                        "workspace": str(workspace),
                        "status": "created",
                        "created_at": now(),
                        "allocated_at": None,
                        "approval": None,
                        # The open pause, if any: what protected action this
                        # task is waiting on a human for, which session asked,
                        # and what the answer was bound to. Separate from
                        # `approval`, which is the reviewed-branch gate that
                        # precedes a merge.
                        "hold": None,
                        # Explicitly represented so a pure investigation task
                        # (read-only work, exempt from the confirmation gates
                        # below) is never indistinguishable from one that just
                        # happens not to have changed anything yet.
                        "read_only": bool(read_only),
                        #: The level this task's agent should reason at, when
                        #: one was chosen. Absent means the ladder decides at
                        #: launch; see `_resolve_effort`.
                        "effort": _validate_effort(effort, "task") if effort else None,
                        #: How much ceremony this change gets -- review rounds,
                        #: effort floor, evidence gate; see `SHAPE_POLICY`.
                        "shape": shape,
                        "shape_reason": _safe_text(shape_reason).strip() if shape_reason else "",
                        # Sticky: once any round is state-changing the task is
                        # a delivery candidate for good, however its last
                        # round was classified.
                        "was_state_changing": not read_only,
                        # The two commander confirmation gates a foreman task
                        # drives before it may launch a state-changing worker.
                        # None until the foreman proposes one; see
                        # `propose_gate`/`decide_gate`. Only meaningful on a
                        # foreman task -- a worker/reviewer task never gates
                        # itself.
                        "gates": (
                            self._inherited_gates(data, project["id"])
                            if role == "foreman"
                            else {gate_type: None for gate_type in GATE_TYPES}
                        ),
                        "delivery": {
                            "policy": policy,
                            "state": "worktree",
                            "events": [],
                        },
                        "workspace_removed": False,
                    }
                    data["tasks"][task_id] = task
                    self._message(
                        data,
                        project,
                        task,
                        None,
                        "status",
                        "Task created",
                        {"status": "created"},
                    )
                    return task
            except _StaleBaseResolution:
                continue
        raise HelmError(
            f"project {project_id}'s configuration kept changing while resolving a "
            "fresh base; try creating the task again"
        )
    def allocate_task(self, task_id: str) -> dict[str, Any]:
        with self.store.locked() as data:
            task = self._task(data, task_id)
            project = self._project(data, task["project_id"])
            if task.get("workspace_removed"):
                raise SafetyError("task workspace was already cleaned and cannot be reallocated")
            workspace = canonical(task["workspace"])
            if task["status"] in {"allocated", "running", "completed", "blocked", "failed", "approval-needed", "approved", "merged"}:
                self._verify_workspace_record(data, project, task)
                return task
            root = canonical(project["root"])
            if _git_root(root) != root:
                raise SafetyError("project root no longer identifies its registered Git repository")
            if not _has_head(root):
                raise HelmError("project has no commit from which to allocate a worktree")
            if workspace.exists():
                raise SafetyError(f"task workspace already exists: {workspace}")
            for other in data["projects"].values():
                if overlaps(workspace, canonical(other["root"])):
                    raise SafetyError("refusing a workspace that overlaps another registered project")
            if task.get("role") in WORKTREELESS_ROLES:
                # Somewhere Helm owns to run in, and nothing to edit in it.
                _private_dir(self.store.directory / _ROLE_DIRECTORY[task["role"]])
                _private_dir(workspace.parent)
                _private_dir(workspace)
            else:
                _private_dir(self.store.directory / "worktrees")
                _private_dir(workspace.parent)
                if _git(root, "show-ref", "--verify", f"refs/heads/{task['branch']}", check=False):
                    raise HelmError(f"task branch already exists: {task['branch']}")
                # Build from the commit resolved and pinned at task creation,
                # never from the project's current HEAD: HEAD can move (a
                # checkout switched, a commit added) in the time between
                # `create_task` and this call, and none of that may change
                # what the task is actually built on. Re-verify the pinned
                # commit still resolves rather than trusting a value that may
                # be minutes or days old -- a force-push or a gc could have
                # made it unreachable since it was recorded.
                base_revision = task["base_revision"]
                if not _git(
                    root, "rev-parse", "--verify", "--quiet", f"{base_revision}^{{commit}}", check=False
                ):
                    raise HelmError(
                        f"task {task['id']}'s recorded base commit no longer resolves in the "
                        f"project: {base_revision}"
                    )
                _git(root, "worktree", "add", "-b", task["branch"], str(workspace), base_revision)
            self._verify_workspace_record(data, project, task)
            task["status"] = "allocated"
            task["allocated_at"] = now()
            self._message(
                data,
                project,
                task,
                None,
                "status",
                f"Isolated worktree allocated at {workspace}",
                {"status": "allocated", "workspace": str(workspace)},
            )
            populate = task.get("role") not in WORKTREELESS_ROLES
            read_only = bool(task.get("read_only"))
        # Deliberately outside the lock: cloning submodules takes minutes, and
        # the state lock is what every worker's message push waits on.
        if populate:
            self._populate_submodules(task_id)
            if read_only:
                # After submodules, not before: populating one writes into the
                # worktree, and locking first would only make that fail too.
                self._lock_down_read_only_workspace(task_id)
        return task
    def _populate_submodules(self, task_id: str) -> None:
        """Fill a fresh worktree's submodules from Helm's own process.

        `git worktree add` leaves them empty, and initializing them from inside
        the worktree writes module metadata into the *main* repository's .git --
        outside the workspace a worker is confined to. So an agent that respects
        that boundary could not build, while one running with its permissions
        bypassed could, and whether a review verified anything or only read the
        diff came down to which runtime it happened to get. Helm owns both the
        worktree and that metadata, so it does this once, here, and no agent
        ever needs to write outside its own workspace.

        A failure here does not fail allocation -- a worktree without its
        submodules is still worth working in, and losing the task to a network
        hiccup would be worse. It is recorded instead, because the thing that
        must never happen is this failing silently and a static review being
        reported as a clean one.
        """
        data = self.store.load()
        task = self._task(data, task_id)
        workspace = canonical(task["workspace"])
        if not (workspace / ".gitmodules").exists():
            return
        _git(workspace, "submodule", "update", "--init", "--recursive", check=False)
        pending = [
            line
            for line in _git(
                workspace, "submodule", "status", "--recursive", check=False
            ).splitlines()
            if line.startswith("-")
        ]
        if not pending:
            return
        with self.store.locked() as locked:
            task = self._task(locked, task_id)
            project = self._project(locked, task["project_id"])
            self._message(
                locked,
                project,
                task,
                None,
                "status",
                f"{len(pending)} submodule(s) could not be initialized in this worktree; "
                "builds and tests needing them will fail, so treat a review from "
                "it as reading only",
                {"submodules_pending": len(pending)},
            )
    @classmethod
    def _set_workspace_writable(
        cls, workspace: Path, *, writable: bool, exclude: Path | None = None
    ) -> None:
        """Flip every path under a task's own worktree between locked and normal.

        This is the actual enforcement for a `read_only` task: `read_only` is
        a label nothing else on the write path consulted, so a foreman could
        brief a read-only task to "implement X and commit it" and the worker
        would simply do it. Removing the write bit from the whole tree makes
        authoring new content -- writing, editing, or deleting a tracked file
        in the worktree -- impossible at the OS level rather than trusting a
        worker to respect a flag it was merely told about.

        This does not by itself make the branch unable to gain a commit: the
        index and object store for a linked worktree live under the project's
        own `.git/worktrees/<branch>`, outside this directory, so `git rm
        --cached` (an index-only removal) followed by `git commit` succeeds
        with no worktree write at all -- the lock never engages because
        nothing here was touched. The same is true of `git hash-object -w`
        plus `git update-index --cacheinfo`. That gap is why `approve_task`
        and `merge_task` refuse a `read_only` task outright rather than
        relying on this lock alone: a read-only task is never a candidate for
        delivery, so what a locked worktree cannot prevent, delivery refuses
        regardless of how a commit was produced. It does not touch the shared
        `.git` object store or per-worktree admin files for the same reason
        those live outside this directory and outside what this lock reaches.
        """
        if not workspace.is_dir():
            return

        def _locked(mode: int) -> int:
            # Strip every write bit (owner/group/other); leave read, exec,
            # and any setuid/setgid/sticky bit exactly as they were.
            return mode & ~0o222

        def _unlocked_file(mode: int) -> int:
            # Restore only the owner write bit. A file that was never
            # owner-writable stays that way; this only undoes the lock.
            return mode | 0o200

        def _unlocked_dir(mode: int) -> int:
            # Directories need write *and* exec/search to accept new or
            # removed entries; restore both on the owner bit only.
            return mode | 0o300

        # Bottom-up: a directory already stripped of its write bit refuses to
        # have entries removed or added, but `os.walk` only needs to list it,
        # and the mode is set on the way back up so descending is never
        # blocked by a parent this pass already locked.
        for dirpath, dirnames, filenames in os.walk(workspace, topdown=False):
            # The task's own output directory is the one place a read-only
            # worker may write, so the lock must not reach into it.
            if exclude is not None and (
                Path(dirpath) == exclude or exclude in Path(dirpath).parents
            ):
                continue
            for name in filenames:
                path = Path(dirpath) / name
                with contextlib.suppress(OSError):
                    if not path.is_symlink():
                        current = stat.S_IMODE(path.stat().st_mode)
                        os.chmod(path, _unlocked_file(current) if writable else _locked(current))
            with contextlib.suppress(OSError):
                current = stat.S_IMODE(os.stat(dirpath).st_mode)
                os.chmod(dirpath, _unlocked_dir(current) if writable else _locked(current))
    def _lock_down_read_only_workspace(self, task_id: str) -> None:
        data = self.store.load()
        task = self._task(data, task_id)
        workspace = canonical(task["workspace"])
        # Made BEFORE the lock, because the lock strips the workspace root's
        # write bit and mkdir inside it would then fail -- silently, since the
        # failure is an OSError like any other. Excluded from the sweep so it
        # keeps the write bits everything else is losing.
        output = workspace / self.READ_ONLY_OUTPUT_DIR
        with contextlib.suppress(OSError):
            output.mkdir(exist_ok=True)
            os.chmod(output, 0o700)
        self._set_workspace_writable(workspace, writable=False, exclude=output)
