"""Herdr presentation adapter for Helm.

The coordinator owns task/worktree truth; this module owns only the optional
Herdr layout and delivery of already-recorded coordinator messages.  Provider
IDs are opaque and are persisted before they are reused.  No lookup by label,
focus, or workspace order is performed.
"""
from __future__ import annotations

import contextlib
import json
import re
import os
import shlex
import shutil
import subprocess
import time
from pathlib import Path
from typing import Any, Protocol, Sequence

from .core import (
    Coordinator,
    HelmError,
    SafetyError,
    _git,
    _safe_text,
    canonical,
    now,
    project_glyph,
)


class HerdrUnavailable(HelmError):
    """Herdr is not installed, not running, or its API could not be reached."""


class HerdrNotFound(HerdrUnavailable):
    """Herdr answered, and the requested resource does not exist.

    Distinguished from a transient failure on purpose: a resource Helm recorded
    but that Herdr no longer has is a stale record to replace, while an
    unreachable server must never cause a second workspace to be created.
    """


DEFAULT_WAIT_TIMEOUT = 5.0


class HerdrClient(Protocol):
    """Small transport boundary used by :class:`HerdrAdapter` and its tests."""

    def available(self) -> bool: ...

    def workspace_get(self, workspace_id: str) -> dict[str, Any]: ...

    def workspace_create(self, label: str, cwd: str | None = None) -> dict[str, Any]: ...

    def tab_create(self, workspace_id: str, label: str, cwd: str) -> dict[str, Any]: ...

    def workspace_rename(self, workspace_id: str, label: str) -> dict[str, Any]: ...

    def tab_rename(self, tab_id: str, label: str) -> dict[str, Any]: ...

    def pane_run(self, pane_id: str, command: str) -> dict[str, Any]: ...

    def pane_send_text(self, pane_id: str, text: str) -> dict[str, Any]: ...

    def pane_send_keys(self, pane_id: str, keys: str) -> dict[str, Any]: ...

    def pane_status(self, pane_id: str) -> dict[str, Any]: ...

    def tab_close(self, tab_id: str) -> dict[str, Any]: ...

    def workspace_close(self, workspace_id: str) -> dict[str, Any]: ...


class SubprocessHerdrClient:
    """Use the installed Herdr CLI without making it part of Helm's core path."""

    def __init__(self, executable: str | Sequence[str] = "herdr"):
        self.executable = list(executable) if not isinstance(executable, str) else [executable]

    def available(self) -> bool:
        # Herdr's CLI is session-scoped. Outside a Herdr-managed pane there
        # is no safe caller context, so Helm must use its core fallback rather
        # than risk touching an ambient/default session.
        return os.environ.get("HERDR_ENV") == "1" and shutil.which(self.executable[0]) is not None

    def _call(self, args: Sequence[str]) -> dict[str, Any]:
        if not self.available():
            raise HerdrUnavailable("Herdr is unavailable; using Helm's terminal worker path")
        # Herdr's socket CLI already answers with one JSON object per call, and
        # rejects an explicit --json on these subcommands.  Send the documented
        # argv only.
        command = [*self.executable, *args]
        try:
            proc = subprocess.run(
                command,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )
        except OSError as exc:
            raise HerdrUnavailable(f"could not invoke Herdr: {exc}") from exc
        if proc.returncode != 0:
            detail = proc.stderr.strip() or proc.stdout.strip() or "Herdr command failed"
            # Herdr reports structured errors on stderr, not stdout.
            if _is_not_found(proc.stdout) or _is_not_found(proc.stderr):
                raise HerdrNotFound(detail)
            raise HerdrUnavailable(detail)
        if not proc.stdout.strip():
            return {}
        try:
            parsed = json.loads(proc.stdout)
        except json.JSONDecodeError as exc:
            raise HerdrUnavailable("Herdr returned a non-JSON response") from exc
        if not isinstance(parsed, dict):
            raise HerdrUnavailable("Herdr returned an invalid JSON response")
        # Herdr answers some failures with exit status 0 and the error in the
        # body -- `pane get` on a deleted pane is one. Reading that as success
        # hands the caller a dict with no status field in it, which liveness
        # then reports as "cannot tell" rather than "gone", so a worker whose
        # pane the user closed stays recorded as running forever.
        if isinstance(parsed.get("error"), dict):
            error = parsed["error"]
            detail = str(
                error.get("message") or error.get("code") or "Herdr reported an error"
            )
            if _is_not_found(proc.stdout):
                raise HerdrNotFound(detail)
            raise HerdrUnavailable(detail)
        return parsed

    def workspace_get(self, workspace_id: str) -> dict[str, Any]:
        return self._call(["workspace", "get", workspace_id])

    def workspace_create(self, label: str, cwd: str | None = None) -> dict[str, Any]:
        command = ["workspace", "create", "--label", label, "--no-focus"]
        if cwd is not None:
            # Herdr labels a workspace from its first pane's directory. Without
            # this the overview pane opens wherever Helm happened to be -- the
            # Helm root -- so a project row reported the Helm repository's
            # branch and unpushed count instead of the project's own.
            command += ["--cwd", cwd]
        return self._call(command)

    def tab_create(self, workspace_id: str, label: str, cwd: str) -> dict[str, Any]:
        return self._call(
            [
                "tab",
                "create",
                "--workspace",
                workspace_id,
                "--label",
                label,
                "--cwd",
                cwd,
                "--no-focus",
            ]
        )

    def workspace_rename(self, workspace_id: str, label: str) -> dict[str, Any]:
        return self._call(["workspace", "rename", workspace_id, label])

    def tab_rename(self, tab_id: str, label: str) -> dict[str, Any]:
        return self._call(["tab", "rename", tab_id, label])

    def pane_run(self, pane_id: str, command: str) -> dict[str, Any]:
        # `pane run` takes the pane ID and a variadic command; it has no focus
        # flag, so a stray one would be swallowed as part of the command.
        return self._call(["pane", "run", pane_id, command])

    def pane_send_text(self, pane_id: str, text: str) -> dict[str, Any]:
        return self._call(["pane", "send-text", pane_id, text])

    def pane_send_keys(self, pane_id: str, keys: str) -> dict[str, Any]:
        return self._call(["pane", "send-keys", pane_id, keys])

    def pane_status(self, pane_id: str) -> dict[str, Any]:
        return self._call(["pane", "get", pane_id])

    def tab_close(self, tab_id: str) -> dict[str, Any]:
        return self._call(["tab", "close", tab_id])

    def workspace_close(self, workspace_id: str) -> dict[str, Any]:
        return self._call(["workspace", "close", workspace_id])


def _paint_command(color: str, text: str) -> str:
    """Build the command that prints one routed line into a project's pane.

    Deliberately uncoloured.  Two approaches were tried and both failed: a
    pre-painted string loses its escape bytes crossing `pane run` and prints
    literal noise like "48;2;3;105;161;97m...", and emitting the escapes from
    printf's format string produces no colour either -- a minimal
    `printf '\\033[41m...'` renders plain.  Per-project colour is delivered
    where it works, in the Helm session's own output; a pane line carries the
    project's name and ID instead, which is the identity anyway.

    A coloured glyph is prefixed instead, since a character survives where a
    control sequence does not.
    """
    glyph = project_glyph(color)
    line = f"{glyph} {text}" if glyph else text
    return f"printf '%s\\n' {shlex.quote(line)}"


def _is_not_found(stdout: str) -> bool:
    """Only a structured Herdr error code counts as 'the resource is gone'."""
    try:
        parsed = json.loads(stdout)
    except (json.JSONDecodeError, TypeError):
        return False
    error = parsed.get("error") if isinstance(parsed, dict) else None
    code = error.get("code") if isinstance(error, dict) else None
    return isinstance(code, str) and code.endswith("_not_found")


def _body(response: dict[str, Any]) -> dict[str, Any]:
    result = response.get("result")
    return result if isinstance(result, dict) else response


def _resource_id(response: dict[str, Any], resource: str, *id_keys: str) -> str:
    """Extract an ID from documented Herdr result shapes, never synthesize one."""
    body = _body(response)
    candidates: list[Any] = [body.get(resource)]
    candidates.extend(body.get(key) for key in id_keys)
    candidates.append(body)
    for candidate in candidates:
        if isinstance(candidate, dict):
            for key in id_keys:
                value = candidate.get(key)
                if isinstance(value, str) and value:
                    return value
            value = candidate.get("id")
            if isinstance(value, str) and value:
                return value
        elif isinstance(candidate, str) and candidate:
            return candidate
    raise HerdrUnavailable(f"Herdr did not return a {resource} ID")


class HerdrAdapter:
    """Present Helm tasks in Herdr while leaving task logic provider-neutral."""

    # One Helm workspace per project.  Worker tabs and routed messages both live
    # inside it, so a project's work is visible in exactly one place.  Every
    # space is created with --no-focus and Helm never issues a focus call, so
    # spawning a worker never moves the user away from what they were doing.
    COORDINATOR_LABEL = "Helm Coordinator · all projects · stable IDs and colors"

    def __init__(self, coordinator: Coordinator, client: HerdrClient | None = None):
        self.coordinator = coordinator
        self.client = client or SubprocessHerdrClient()

    @staticmethod
    def _project_label(project: dict[str, Any]) -> str:
        # Color supplements, but never replaces, stable text identity.
        return f"{project['name']} · {project['id']} · {project['color']}"

    # A Herdr panel shows only the first few characters of a label, so any
    # shared prefix truncates every entry to the same useless stub. These
    # labels are display only -- Helm identifies every resource by opaque ID
    # and never looks one up by label -- so they exist purely to be
    # distinguishable at a glance, and the distinguishing part goes first.
    _LABEL_STOPWORDS = frozenset({
        "a", "an", "and", "the", "for", "of", "to", "in", "on", "with", "that",
        "this", "it", "its", "is", "are", "be", "do", "so", "as", "at", "by",
        "from", "into", "one", "all", "any", "make", "made", "new", "please",
        "help", "use", "using", "run", "then", "not", "no", "your", "you",
    })

    @classmethod
    def _brief_slug(cls, brief: str, limit: int = 12) -> str:
        words = re.findall(r"[a-z0-9]+", str(brief or "").lower())
        picked = [word for word in words if word not in cls._LABEL_STOPWORDS]
        slug = "-".join(picked[:3])[:limit].strip("-")
        return slug

    @classmethod
    def _workspace_label(cls, project: dict[str, Any]) -> str:
        # The glyph carries the project's colour in one character, so identity
        # survives without the hex value pushing the ID out of view.
        glyph = project_glyph(project.get("color", ""))
        return f"{glyph} {project['id']}" if glyph else str(project["id"])

    @classmethod
    def _worker_tab_label(cls, task: dict[str, Any], worker: dict[str, Any]) -> str:
        # A foreman is one per project and its brief is the same standing text
        # every time, so slugging it produced a tab named after the opening
        # words of that text -- "you-are-this-projects-f-fe58" -- which says
        # nothing about which pane it is. It is the foreman; name it that.
        if (task or {}).get("role") == "foreman":
            return "foreman"
        slug = cls._brief_slug(task.get("brief", "") if task else "")
        suffix = str(worker["id"]).replace("w-", "")[:4]
        return f"{slug}-{suffix}" if slug else f"w{suffix}"

    def _herdr_state(self, data: dict[str, Any]) -> dict[str, Any]:
        integrations = data.setdefault("integrations", {})
        state = integrations.setdefault("herdr", {})
        state.setdefault("coordinator", None)
        state.setdefault("projects", {})
        state.setdefault("workers", {})
        return state

    @staticmethod
    def _owned(record: dict[str, Any], description: str) -> None:
        if record.get("owned") is not True:
            raise SafetyError(f"refusing to operate on unowned Herdr {description}")

    @staticmethod
    def _optional_resource_id(response: dict[str, Any], resource: str, *keys: str) -> str | None:
        with contextlib.suppress(HerdrUnavailable):
            return _resource_id(response, resource, *keys)
        return None

    def _compensate_response(self, response: dict[str, Any]) -> None:
        """Close IDs returned by a failed provider create call."""
        tab_id = self._optional_resource_id(response, "tab", "tab_id")
        workspace_id = self._optional_resource_id(response, "workspace", "workspace_id")
        if tab_id:
            with contextlib.suppress(HerdrUnavailable):
                self.client.tab_close(tab_id)
        if workspace_id:
            with contextlib.suppress(HerdrUnavailable):
                self.client.workspace_close(workspace_id)

    def _close_layout(self, record: dict[str, Any]) -> None:
        self._owned(record, "resource")
        tab_id = record.get("tab_id", record.get("overview_tab_id"))
        if tab_id:
            with contextlib.suppress(HerdrUnavailable):
                self.client.tab_close(tab_id)
        with contextlib.suppress(HerdrUnavailable):
            self.client.workspace_close(record["workspace_id"])

    def _remove_provisional_layouts(self, *, project_id: str | None) -> None:
        data = self.coordinator.store.load()
        state = self._herdr_state(data)
        project = state["projects"].get(project_id) if project_id else None
        if project is None:
            return
        self._close_layout(project)
        with self.coordinator.store.locked() as current:
            self._herdr_state(current)["projects"].pop(project_id, None)

    def _recorded_workspace_is_live(self, workspace_id: str) -> bool:
        """Reuse a recorded space only while Herdr still has it.

        A resource Helm created can be closed by the user or lost with the
        session.  Reusing that ID produces a hard failure on the next call, so
        the record is stale and must be replaced.  Any answer other than an
        explicit not-found is treated as live, so an unreachable server can
        never cause a duplicate workspace.
        """
        getter = getattr(self.client, "workspace_get", None)
        if getter is None:
            return True
        try:
            getter(workspace_id)
        except HerdrNotFound:
            return False
        except HerdrUnavailable:
            return True
        return True

    def _ensure_project_workspace(self, project: dict[str, Any]) -> dict[str, Any]:
        data = self.coordinator.store.load()
        state = self._herdr_state(data)
        existing = state["projects"].get(project["id"])
        if existing is not None:
            self._owned(existing, f"workspace for project {project['id']}")
            if self._recorded_workspace_is_live(existing["workspace_id"]):
                return existing
            # Drop the stale record and the worker tabs that lived inside it.
            with self.coordinator.store.locked() as current:
                current_state = self._herdr_state(current)
                current_state["projects"].pop(project["id"], None)
                current_state["workers"] = {
                    key: value
                    for key, value in current_state["workers"].items()
                    if value.get("workspace_id") != existing["workspace_id"]
                }
        label = self._workspace_label(project)
        response = self._create_workspace(label, project)
        try:
            record = {
                "project_id": project["id"],
                "workspace_id": _resource_id(response, "workspace", "workspace_id"),
                "overview_tab_id": _resource_id(response, "tab", "tab_id"),
                "overview_pane_id": _resource_id(response, "root_pane", "pane_id", "root_pane_id"),
                "label": label,
                "color": project["color"],
                "owned": True,
                "created_at": now(),
            }
        except HerdrUnavailable:
            self._compensate_response(response)
            raise
        # The root tab cannot be closed while it is a workspace's only tab, and
        # Helm prints this project's reports into it.  Name it for that job so
        # it does not sit in the space as an unexplained "1".
        rename = getattr(self.client, "tab_rename", None)
        if rename is not None:
            with contextlib.suppress(HerdrUnavailable):
                rename(record["overview_tab_id"], f"Helm Reports · {project['id']}")
        with self.coordinator.store.locked() as data:
            self._herdr_state(data)["projects"][project["id"]] = record
        return record

    def _create_workspace(self, label: str, project: dict[str, Any]) -> dict[str, Any]:
        """Open a project's space in that project's own directory.

        Herdr labels a workspace from its first pane, so a space created from
        the Helm root reported the Helm repository's branch and unpushed count
        on the project's row. A client that predates the argument still works;
        the directory is presentation, never isolation.
        """
        try:
            return self.client.workspace_create(label, str(project["root"]))
        except TypeError:
            return self.client.workspace_create(label)

    def _record_worker_layout(
        self,
        worker: dict[str, Any],
        project_layout: dict[str, Any],
        tab: dict[str, Any],
        label: str,
    ) -> dict[str, Any]:
        record = {
            "worker_id": worker["id"],
            "task_id": worker["task_id"],
            "project_id": worker["project_id"],
            "workspace_id": project_layout["workspace_id"],
            "tab_id": _resource_id(tab, "tab", "tab_id"),
            "pane_id": _resource_id(tab, "root_pane", "pane_id", "root_pane_id"),
            "cwd": worker["workspace"],
            "label": label,
            "owned": True,
            "created_at": now(),
            "routed_message_ids": [],
        }
        with self.coordinator.store.locked() as data:
            self._herdr_state(data)["workers"][worker["id"]] = record
        return record

    @staticmethod
    def _runner_command(worker: dict[str, Any]) -> str:
        # The runner mirrors worker output into the Herdr tab and into Helm's
        # bounded log, behind a banner marking it as worker output.  A silent
        # tab looked identical to a dead worker, which is worse than the risk
        # the silence was guarding: worker text remains data either way.
        command = [
            "env",
            f"PYTHONPATH={worker['runner_pythonpath']}",
            *worker["runner_command"],
        ]
        return shlex.join(command)

    def _route_messages(self, worker: dict[str, Any]) -> None:
        data = self.coordinator.store.load()
        state = self._herdr_state(data)
        layout = state["workers"].get(worker["id"])
        # One space per project: messages land in that project's own overview
        # pane rather than a separate global workspace.
        project_layout = state["projects"].get(worker["project_id"])
        if layout is None or project_layout is None:
            return
        self._owned(layout, f"tab for worker {worker['id']}")
        self._owned(project_layout, f"workspace for project {worker['project_id']}")
        routed = set(layout.get("routed_message_ids", []))
        report = self.coordinator.inspect_task(worker["task_id"])
        project = report["project"]
        allowed = {"status", "result", "blocker", "failure", "approval-needed", "artifact"}
        for message in report["messages"]:
            if message.get("worker_id") != worker["id"] or message["kind"] not in allowed:
                continue
            if message["id"] in routed:
                continue
            line = (
                f"[Helm project {self._project_label(project)}] "
                f"worker={worker['id']} {message['kind']}: {message['text']}"
            )
            try:
                self.client.pane_run(
                    project_layout["overview_pane_id"],
                    _paint_command(project.get("color", ""), line),
                )
            except HerdrNotFound:
                # The pane a project prints into is gone, so its space is gone.
                # A human closing their own workspace is their prerogative, and
                # the answer is to drop the stale record rather than to raise
                # out of a routine poll -- the next spawn makes a fresh space.
                with self.coordinator.store.locked() as current:
                    self._herdr_state(current)["projects"].pop(
                        worker["project_id"], None
                    )
                return
            routed.add(message["id"])
        with self.coordinator.store.locked() as data:
            current = self._herdr_state(data)["workers"].get(worker["id"])
            if current is not None:
                current["routed_message_ids"] = sorted(routed)

    def _fallback(
        self,
        task_id: str,
        command: str | Sequence[str] | None,
        wait: bool,
        *,
        domain: str | None = None,
        agent: str | None = None,
    ) -> dict[str, Any]:
        return self.coordinator.launch_worker(task_id, command, wait=wait, domain=domain, agent=agent)

    def launch_task(
        self,
        task_id: str,
        command: str | Sequence[str] | None,
        *,
        wait: bool = True,
        domain: str | None = None,
        agent: str | None = None,
    ) -> dict[str, Any]:
        """Launch one Helm worker in one Herdr tab, or use the core fallback."""
        if not self.client.available():
            return self._fallback(task_id, command, wait, domain=domain, agent=agent)

        # Do all provider setup before reserving a core worker.  A missing or
        # stopped Herdr server therefore falls back without changing task
        # assignment semantics.
        project_id: str | None = None
        try:
            data = self.coordinator.store.load()
            task = data["tasks"].get(task_id)
            if task is None:
                raise HelmError(f"unknown task: {task_id}")
            project = data["projects"].get(task["project_id"])
            if project is None:
                raise HelmError(f"unknown project: {task['project_id']}")
            project_id = project["id"]
            project_layout = self._ensure_project_workspace(project)
        except HerdrUnavailable:
            self._remove_provisional_layouts(project_id=project_id)
            return self._fallback(task_id, command, wait, domain=domain, agent=agent)
        except HelmError:
            self._remove_provisional_layouts(project_id=project_id)
            raise

        worker = self.coordinator.prepare_external_worker(
            task_id,
            command,
            execution="herdr",
            domain=domain,
            agent=agent,
        )
        worker_label = self._worker_tab_label(task, worker)
        worker_layout: dict[str, Any] | None = None
        tab: dict[str, Any] | None = None
        try:
            tab = self.client.tab_create(
                project_layout["workspace_id"], worker_label, worker["workspace"]
            )
            worker_layout = self._record_worker_layout(worker, project_layout, tab, worker_label)
            self.client.pane_run(worker_layout["pane_id"], self._runner_command(worker))
        except HerdrUnavailable as exc:
            # A tab/pane created before a later provider failure is still Helm's
            # responsibility.  Close it before leaving the failed assignment.
            if worker_layout is not None:
                with contextlib.suppress(HerdrUnavailable):
                    self.client.tab_close(worker_layout["tab_id"])
                with self.coordinator.store.locked() as current:
                    self._herdr_state(current)["workers"].pop(worker["id"], None)
            elif tab is not None:
                tab_id = self._optional_resource_id(tab, "tab", "tab_id")
                if tab_id:
                    with contextlib.suppress(HerdrUnavailable):
                        self.client.tab_close(tab_id)
            # The assignment is deliberately not silently converted to a
            # process worker after it is persisted.  It remains auditable and
            # cannot accidentally run twice in two execution surfaces.
            self.coordinator.fail_worker_start(worker["id"], str(exc))
            raise
        if not self._runner_started(worker):
            # The tab stays open on purpose: that pane holds the shell line
            # that explains why, and it is the only place the reason exists.
            self.coordinator.fail_worker_start(
                worker["id"],
                "the worker process never started in its pane: the launch command "
                "did not run. Read the pane for the shell's own error -- shell "
                "startup output (an update prompt, a banner, a slow rc) can consume "
                "the typed command.",
            )
            raise HelmError(
                f"worker {worker['id']} never started in its Herdr pane; "
                f"its tab is kept so the pane shows why"
            )
        self._route_messages(worker)
        if wait:
            return self.wait_worker(worker["id"])
        return worker

    # A pane runs the command by typing it at whatever the pane's shell is
    # doing, so anything the user's shell prints at startup races it. An
    # oh-my-zsh "[Y/n]" update prompt ate the leading character of a launch
    # and left `nv PYTHONPATH=... ` -- "command not found", no runner, no
    # process. Helm still recorded the worker as running, so `helm review`
    # waited forever on a reviewer that had never existed and a foreman
    # reported itself as driving while nothing drove.
    #
    # The runner writes its banner to the log as its first act, and the log is
    # created empty at prepare time, so a log that is still empty means the
    # process never started. Wait briefly for that proof and fail loudly
    # without it: a worker that never starts must not be indistinguishable
    # from one that is working.
    RUNNER_START_TIMEOUT = 15.0

    def _runner_started(self, worker: dict[str, Any]) -> bool:
        log_file = Path(worker["log_file"])
        exit_file = Path(worker["exit_file"])
        deadline = time.monotonic() + self.RUNNER_START_TIMEOUT
        while time.monotonic() < deadline:
            with contextlib.suppress(OSError):
                # An immediate failure writes the exit record instead, and it
                # still proves the runner ran.
                if exit_file.exists() or log_file.stat().st_size > 0:
                    return True
            time.sleep(0.25)
        return False

    def _provider_worker_alive(self, worker: dict[str, Any]) -> bool | None:
        """Return provider liveness when available; None means unknown."""
        if not self.client.available():
            return False
        data = self.coordinator.store.load()
        layout = self._herdr_state(data)["workers"].get(worker["id"])
        if not layout:
            return None
        for method_name in ("worker_status", "pane_status"):
            method = getattr(self.client, method_name, None)
            if method is None:
                continue
            try:
                response = method(layout["pane_id"])
            except HerdrNotFound:
                # The provider answered and the pane is not there. That is the
                # strongest evidence available that the session is over, so it
                # must not be filed under "cannot tell" with the transient
                # failures below -- doing so left a worker whose space the user
                # closed recorded as running with nothing able to correct it.
                return False
            except HerdrUnavailable:
                continue
            body = _body(response) if isinstance(response, dict) else {}
            if isinstance(body.get("pane"), dict):
                body = body["pane"]
            if isinstance(body.get("alive"), bool):
                return body["alive"]
            if isinstance(body.get("exists"), bool) and not body["exists"]:
                return False
            status = body.get("status")
            if isinstance(status, str) and status.lower() in {
                "missing", "closed", "dead", "lost", "not-found", "exited",
            }:
                return False
            if isinstance(status, str) and status.lower() in {"running", "alive", "active"}:
                return True
            # A pane record that still resolves means the layout is intact.  The
            # worker's own completion is decided by its exit record, never here.
            pane_id = body.get("pane_id")
            if isinstance(pane_id, str) and pane_id:
                return True
        return None

    # States that mean a project still has something to show the driver: work in
    # flight, a decision waiting, or evidence worth reading.
    _ACTIVE_TASK_STATES = {"created", "allocated", "running"}
    _ATTENTION_TASK_STATES = {"blocked", "failed", "approval-needed"}

    def _terminal_message(self, worker_id: str, after: int = 0) -> dict[str, Any] | None:
        data = self.coordinator.store.load()
        found = [
            message
            for message in data.get("messages", [])[after:]
            if message.get("worker_id") == worker_id
            and message.get("kind") in {"result", "blocker", "failure"}
        ]
        return found[-1] if found else None

    def _message_count(self) -> int:
        return len(self.coordinator.store.load().get("messages", []))

    def _await_terminal(
        self,
        worker_id: str,
        *,
        after: int,
        timeout: float,
        poll: float = 5.0,
        output_mark: int | None = None,
        brief: str = "",
    ) -> dict[str, Any] | None:
        # The offset is captured by the caller BEFORE it launches or answers,
        # so a worker that reports instantly is not missed and a stale verdict
        # from an earlier round is never mistaken for this round's answer.
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            message = self._terminal_message(worker_id, after)
            if message is not None:
                return message
            # Recovery is checked every round, not only once the wait runs
            # out. A reviewer whose report is refused reaches its verdict in
            # about a minute; making that wait out a thirty-minute timeout
            # meant the fallback existed and never helped, and the driver
            # blocked in this call the whole time looking dead.
            if output_mark is not None:
                recovered = self._verdict_from_output(worker_id, output_mark, brief)
                if recovered is not None:
                    recovered["recovered"] = True
                    return recovered
            time.sleep(poll)
        return None

    # A verdict the reviewer typed itself, at the start of its own line. The
    # brief that asks for one names BOTH words in a single sentence and ends
    # the second with a comma, so neither shape can be mistaken for an answer
    # to it.
    _VERDICT_LINE = re.compile(r"^[\s>*#\-•]*(APPROVED|CHANGES-REQUESTED)(?=\s|$)")

    @staticmethod
    def _verdict_for_outcome(outcome: dict[str, Any]) -> str:
        """Read one round's verdict, distinguishing a crash from an objection.

        A reviewer that died is not a reviewer with findings. Deriving the
        verdict from the text alone turned "Worker exited with code 1" into
        changes-requested, which is indistinguishable from a real objection:
        the author was then sent to fix findings that never existed and the
        run ended in author-timeout. The direction was safe -- a crash never
        reads as APPROVED -- but a fabricated objection is still a fabricated
        review, and this is the one loop whose whole point is that the verdict
        can be trusted.
        """
        if outcome.get("kind") in {"failure", "blocker"}:
            return "review-unavailable"
        text = str(outcome.get("text", ""))
        match = HerdrAdapter._VERDICT_LINE.match(text.strip().upper())
        if match is None:
            # Neither verdict word is present, so the reviewer did not reach
            # one. Reading that as changes-requested was the same fabrication
            # this method already refuses for a crash, arriving by a different
            # door: a reviewer whose workspace was empty reported "Review could
            # not run", and the loop recorded a considered objection to code
            # nobody had read. Absence of a verdict is not a verdict.
            return "review-unavailable"
        return "approved" if match.group(1) == "APPROVED" else "changes-requested"

    def _verdict_from_output(
        self, worker_id: str, since: int, brief: str = ""
    ) -> dict[str, Any] | None:
        """Recover a verdict the reviewer reached but failed to push.

        The protocol is how a verdict is meant to arrive, and it can fail: one
        `helm worker message` refused against the state directory is enough to
        lose a finished review whole, which is exactly what happened -- a real
        defect found, reported to nobody, and indistinguishable from a
        reviewer that never ran. The reviewer still said it in its own pane, so
        read it there rather than calling a completed review a timeout.

        Recovery is deliberately last, never first. A pane is a lossy surface
        and Helm should not learn to prefer it to the record.

        The pane also contains the brief, because an agent that renders a TUI
        draws its own prompt. Two reviewers "reported" the instruction back:
        the pane wrapped mid-sentence, a line began `CHANGES-REQUESTED -- Helm
        reads...`, and both single-line guards below missed it because the
        words that would have disqualified it had wrapped onto the line above.
        Helm wrote that brief, so it can recognise it: a candidate whose text
        is a fragment of the instruction is the instruction, however the
        terminal chose to break it.
        """
        try:
            lines = self.coordinator.worker_output(worker_id, lines=400, since=since)
        except HelmError:
            return None
        instruction = " ".join(brief.split())
        for index in range(len(lines) - 1, -1, -1):
            line = lines[index]
            match = self._VERDICT_LINE.match(line)
            if match is None:
                continue
            other = "CHANGES-REQUESTED" if match.group(1) == "APPROVED" else "APPROVED"
            if other in line or "FIRST WORD" in line:
                continue
            candidate = " ".join(line.split())
            if instruction and candidate and candidate in instruction:
                continue
            return {"text": "\n".join(lines[index:]).strip()}
        return None

    def _output_mark(self, worker_id: str) -> int:
        with contextlib.suppress(HelmError, OSError):
            return self.coordinator.worker_output_mark(worker_id)
        return 0

    def _keep_review_session_warm(self, worker_id: str) -> bool:
        """Reopen a completed reviewer for the next round of the same task.

        Review results are terminal for a round, not necessarily for the whole
        review loop. Keeping the same reviewer pane through that bounded loop
        avoids paying the context-sharing cost again while preserving the
        larger isolation rule: a reviewer is still tied to one task and is not
        reused after this loop ends.
        """
        data = self.coordinator.store.load()
        worker = data.get("workers", {}).get(worker_id)
        if worker is None or worker.get("status") != "completed":
            return False
        task = data.get("tasks", {}).get(worker.get("task_id"))
        if task is None or task.get("role") != "reviewer" or not task.get("reviews"):
            return False
        layout = self._herdr_state(data).get("workers", {}).get(worker_id)
        if layout is None:
            return False
        self._owned(layout, f"tab for worker {worker_id}")
        if self._provider_worker_alive(worker) is False:
            return False
        reopened_at = now()
        with self.coordinator.store.locked() as current:
            current_worker = current.get("workers", {}).get(worker_id)
            current_task = current.get("tasks", {}).get(worker["task_id"])
            if current_worker is None or current_task is None:
                return False
            if current_worker.get("status") != "completed":
                return current_worker.get("status") == "running"
            current_worker["status"] = "running"
            current_worker["exit_code"] = None
            current_worker["ended_at"] = None
            current_worker["reopened_at"] = reopened_at
            current_task["status"] = "running"
        return True

    def run_review_cycle(
        self,
        task_id: str,
        *,
        reviewer_agent: str | None = None,
        reviewer_model: str | None = None,
        rounds: int = 2,
        timeout: float = 1800.0,
    ) -> dict[str, Any]:
        """Run author and reviewer against each other until both are satisfied.

        The author keeps its session, so a review round is delivered into it
        rather than starting a new author who would have lost the context that
        produced the change. The loop is bounded: the `code-review` domain puts
        the limit at two rounds of direct disagreement, after which the
        coordinator decides rather than letting two agents negotiate forever.
        """
        data = self.coordinator.store.load()
        task = data["tasks"].get(task_id)
        if task is None:
            raise HelmError(f"unknown task: {task_id}")
        project = data["projects"].get(task["project_id"])
        author = next(
            (w for w in data["workers"].values() if w["task_id"] == task_id), None
        )
        if author is None:
            raise HelmError(f"task {task_id} has no worker to review")
        # Resolved once, before any reviewer starts, so every round of this
        # review measures against the same tree -- and so an empty target is
        # refused before an agent is spent on it rather than after.
        review_base = self._review_target(project, task)
        cleared = self._clear_stale_reviewers_for(task_id)
        if cleared:
            data = self.coordinator.store.load()
        existing = self._live_reviewer_for(data, task_id)
        if existing is not None:
            # Two drivers on one task is the failure this guards. A project's
            # foreman runs the review loop as part of its brief, and a
            # coordinator driving the same task directly runs it too -- both
            # correct on their own, and nineteen seconds apart in practice.
            # Two reviewers then read the same worktree, burn two agents, and
            # whichever finishes first sets the verdict while the other's
            # findings reach nobody. Refused rather than deduplicated silently,
            # because the caller needs to know a review is already happening.
            raise HelmError(
                f"task {task_id} already has a running reviewer "
                f"({existing['id']} on {existing.get('agent_id')}); wait for it, or stop "
                "it first. A project with a foreman already runs this loop -- driving it "
                "from the coordinator as well starts a second one."
            )
        choice = self.coordinator.pick_reviewer_agent(
            author.get("agent_id"), explicit=reviewer_agent, model=reviewer_model
        )
        history: list[dict[str, Any]] = []
        reviewer_worker: dict[str, Any] | None = None
        verdict = "unresolved"

        for round_number in range(1, max(1, rounds) + 1):
            before = self._message_count()
            # Taken before the reviewer is asked anything, so recovery reads
            # only what this round produced.
            mark = 0 if reviewer_worker is None else self._output_mark(reviewer_worker["id"])
            reviewer_is_running = False
            if reviewer_worker is not None:
                current = self.coordinator.store.load().get("workers", {}).get(reviewer_worker["id"])
                reviewer_is_running = bool(current and current.get("status") == "running")
            if reviewer_worker is None or not reviewer_is_running:
                # Terminal protocol results settle workers even when their
                # interactive pane remains open. Do not reopen a completed
                # worker for another review round; launch a fresh reviewer task.
                review_task = self.coordinator.create_task(
                    project["id"],
                    # Only the facts of this review and the one contract Helm
                    # itself parses. How to review -- what to read first, what
                    # to check, what a finding is worth -- is the `code-review`
                    # domain's, attached below and composed into this task's
                    # context. Restating it here would be a second copy free to
                    # drift from the one that is versioned and reviewable.
                    (
                        f"Run every git command in {task['workspace']} -- that is the "
                        "author's checkout of this branch, and it is the only "
                        "repository you have. Your own workspace is deliberately empty: "
                        "you are reading a diff, not building one, so you were given no "
                        "checkout of your own.\n\n"
                        "You MAY run the test suite, the type checker and the linter "
                        "there, and you are expected to: a review that only reads is "
                        "worth less than one that ran, and the caches those tools write "
                        "(jest cache, node_modules/.cache, target/, coverage) are "
                        "ignored build artefacts, not the author's work. What you must "
                        "not do is change what is under review -- no edits to tracked "
                        "files, no commit, no stage, no branch or checkout change. "
                        "Leave `git status` as clean as you found it, and say in your "
                        "verdict what you ran and what it returned.\n\n"
                        "The previous wording forbade running 'anything that writes', "
                        "which a careful reviewer correctly read as a ban on the test "
                        "suite -- so it asked permission, nobody answered, and it "
                        "published a review it had to caveat as static-only. That is "
                        "the gap this paragraph closes.\n\n"
                        f"Review the change on branch {task['branch']} against "
                        f"{review_base}, following the code-review domain in "
                        "your context. Diff against that commit exactly, not against "
                        f"{task['base_branch']}: the base branch has moved since this "
                        "work started, and measuring against a different tree turns a "
                        "correct figure into a finding. Finish with one result message "
                        "whose FIRST WORD is APPROVED or CHANGES-REQUESTED -- Helm reads "
                        "that word to decide whether the loop continues -- followed by "
                        "your findings."
                    ),
                    domain="code-review",
                    agent=choice["agent"],
                    # What this reviewer reviews, so a second driver can see it
                    # exists before starting another.
                    reviews=task_id,
                    # A reviewer reads a diff; it never writes one. It needs the
                    # branch and the base commit, both of which are in the brief
                    # above, not a checkout of its own -- and a checkout of its
                    # own is what it had: thirty-five review tasks on one
                    # project, thirty-five clones with submodules, every review
                    # branch deleted as empty when the tasks were cleaned up.
                    role="reviewer",
                )
                reviewer_worker = self.launch_task(
                    review_task["id"], choice["command"], wait=False
                )
            else:
                self.answer_worker(
                    reviewer_worker["id"],
                    f"The author has pushed changes for round {round_number}. Re-read the "
                    f"diff on {task['branch']} and reply again with APPROVED or "
                    "CHANGES-REQUESTED as the first word of a result message.",
                )
            outcome = self._await_terminal(
                reviewer_worker["id"],
                after=before,
                timeout=timeout,
                output_mark=mark,
                # The reviewer's own brief, so an echo of it in the pane is
                # never recovered as the answer to it.
                brief=review_task["brief"],
            )
            source = "protocol"
            if outcome is not None and outcome.get("recovered"):
                source = "output"
                # Put it back on the record the push failed to reach, so the
                # task settles and every other reader -- board, status, the
                # project's own log -- sees the review that happened.
                with contextlib.suppress(HelmError, OSError):
                    self.coordinator.record_worker_message(
                        reviewer_worker["id"],
                        "result",
                        outcome["text"],
                        payload={"recovered_from": "worker-output"},
                    )
            if outcome is None:
                history.append({"round": round_number, "verdict": "timeout"})
                verdict = "timeout"
                with contextlib.suppress(HelmError, OSError):
                    self.coordinator.record_task_progress_summary(
                        task_id,
                        f"review round {round_number}: timeout waiting for reviewer",
                        source="Review loop",
                    )
                break
            text = str(outcome.get("text", ""))
            round_verdict = self._verdict_for_outcome(outcome)
            history.append({
                "round": round_number,
                "verdict": round_verdict,
                "source": source,
                "text": text,
            })
            summary = _safe_text(text).replace("\n", " ").strip()[:500]
            with contextlib.suppress(HelmError, OSError):
                self.coordinator.record_task_progress_summary(
                    task_id,
                    f"review round {round_number}: {round_verdict}"
                    + (f" -- {summary}" if summary else ""),
                    source="Review loop",
                )
            if round_verdict == "review-unavailable":
                verdict = "review-unavailable"
                break
            approved = round_verdict == "approved"
            if approved:
                verdict = "approved"
                break
            if round_number >= rounds:
                # Bounded on purpose: agreement reached because one side gave
                # up is not agreement, so the objection stands and a human
                # decides.
                verdict = "unresolved"
                break
            author_before = self._message_count()
            self.answer_worker(
                author["id"],
                "Review findings from an independent reviewer. Address them on your task "
                f"branch and commit, then report a result. Findings: {text}",
            )
            with contextlib.suppress(HelmError, OSError):
                self.coordinator.record_task_progress_summary(
                    task_id,
                    f"author sent back after review round {round_number} to address reviewer findings",
                    source="Review loop",
                )
            if self._await_terminal(
                author["id"], after=author_before, timeout=timeout
            ) is None:
                verdict = "author-timeout"
                with contextlib.suppress(HelmError, OSError):
                    self.coordinator.record_task_progress_summary(
                        task_id,
                        f"author did not report back after review round {round_number}",
                        source="Review loop",
                    )
                break
            if reviewer_worker is not None and self._keep_review_session_warm(reviewer_worker["id"]):
                with contextlib.suppress(HelmError, OSError):
                    self.coordinator.record_task_progress_summary(
                        task_id,
                        f"reviewer kept live for review round {round_number + 1}",
                        source="Review loop",
                    )

        return {
            "task_id": task_id,
            "author_agent": author.get("agent_id"),
            "reviewer_agent": choice["agent"],
            "independence": choice["independence"],
            "reviewer_reason": choice["reason"],
            "verdict": verdict,
            "rounds": history,
        }

    def _task_has_live_pane(self, data: dict[str, Any], task_id: str) -> bool:
        """Whether any recorded tab still exists for this task's workers."""
        state = self._herdr_state(data)
        for worker_id, layout in state.get("workers", {}).items():
            worker = data.get("workers", {}).get(worker_id)
            if worker is None or worker.get("task_id") != task_id:
                continue
            if layout.get("tab_id"):
                return True
        return False

    # What is worth interrupting a driver for. Routine progress is not: a
    # foreman told about every status push spends its attention reading
    # instead of driving, which is the same failure as an attention list full
    # of healthy workers. A status explicitly marked as a summary is different:
    # it is the worker saying "this intermediate outcome changes what the
    # driver/user should know", such as a completed review round.
    _FOREMAN_WAKE_KINDS = ("result", "blocker", "failure", "approval-needed", "question")

    def notify_foreman(self, worker_id: str) -> bool:
        """Push a worker's terminal message into its project's foreman session.

        Helm's protocol only ever pushed upward: a worker reports, Helm
        records it and routes it to the project's pane. Nothing was pushed
        into the thing that drives the work. So a foreman had to poll `helm
        watch` to learn that what it delegated had finished -- and it has no
        reason to poll at any particular moment, so a completed task sat done
        and unadvanced while every component reported correctly.

        A pane is where a human looks. This is where the driver is told.
        """
        data = self.coordinator.store.load()
        worker = data.get("workers", {}).get(worker_id)
        if worker is None:
            return False
        task = data.get("tasks", {}).get(worker.get("task_id")) or {}
        # A foreman's own reports go to Helm, never back into itself.
        if task.get("role") == "foreman":
            return False
        foreman = self.coordinator.foreman_for(worker.get("project_id", ""))
        if foreman is None or foreman["id"] == worker_id:
            return False
        latest = None
        for message in data.get("messages", []):
            summary_status = (
                message.get("kind") == "status"
                and self.coordinator._summary_payload(message.get("payload"))
            )
            if (
                message.get("worker_id") == worker_id
                and (message.get("kind") in self._FOREMAN_WAKE_KINDS or summary_status)
            ):
                latest = message
        if latest is None:
            return False
        kind = latest["kind"]
        task_id = worker.get("task_id")
        # Say what it is and what to do about it. A driver handed a wall of
        # text re-reads the whole task; a driver handed the next command acts.
        nudge = (
            f"run `helm review {task_id}` if this change is code and the work is done"
            if kind == "result"
            else f"read it with `helm inspect {task_id}` and decide"
        )
        text = (
            f"WORKER UPDATE from Helm: {worker_id} on task {task_id} pushed a "
            f"{kind}. It is yours to act on -- {nudge}. Message: "
            f"{_safe_text(latest.get('text', ''))[:600]}"
        )
        with contextlib.suppress(HelmError, HerdrUnavailable, OSError):
            return bool(self.answer_worker(foreman["id"], text))
        return False

    def stop_worker(self, worker_id: str, reason: str = "") -> dict[str, Any]:
        """Stop a worker wherever it is actually hosted.

        A process worker dies by signal; a Herdr worker has no pid Helm owns,
        and its pane is the thing running it. Both settle the same record, so
        the caller does not have to know which surface it is stopping -- and
        the record settles even when the provider cannot be reached, because a
        stop nobody can record is exactly the state this command exists to
        make impossible.

        Evidence is captured before the pane closes. A worker stopped mid-task
        is one somebody will want to read afterwards, and the pane is where
        the reason lives.
        """
        with contextlib.suppress(HelmError, OSError):
            self.coordinator.capture_evidence(worker_id)
        stopped = self.coordinator.stop_worker(worker_id, reason)
        data = self.coordinator.store.load()
        layout = self._herdr_state(data).get("workers", {}).get(worker_id) or {}
        tab_id = layout.get("tab_id")
        stopped["tab_closed"] = False
        if tab_id:
            try:
                self.client.tab_close(tab_id)
                stopped["tab_closed"] = True
            except HerdrNotFound:
                stopped["tab_closed"] = True
            except HerdrUnavailable:
                # The record is already settled; a pane Helm cannot reach is a
                # presentation problem, not an unstoppable worker.
                return stopped
            with self.coordinator.store.locked() as live:
                self._herdr_state(live).get("workers", {}).pop(worker_id, None)
        return stopped

    def release_finished_tabs(self) -> list[str]:
        """Close the tab of every worker whose work is cleanly done.

        Helm keeps a pane because it is evidence: a failed, blocked or
        approval-needed task needs somewhere to look. A task that completed and
        was recorded has nothing left to show, so its tab is just clutter that
        makes the panel harder to read -- which is the same failure as a status
        report full of settled projects.
        """
        data = self.coordinator.store.load()
        state = self._herdr_state(data)
        keep = {"blocked", "failed", "approval-needed"}
        closed: list[str] = []
        for worker_id, layout in list(state.get("workers", {}).items()):
            worker = data.get("workers", {}).get(worker_id)
            tab_id = layout.get("tab_id")
            if worker is None or not tab_id or worker.get("status") == "running":
                continue
            task = data.get("tasks", {}).get(worker.get("task_id"))
            if task is None or task.get("status") in keep:
                continue
            # Evidence first, always. A pane released before its diagnosis is
            # written loses the reason it failed with nothing to recover it
            # from.
            with contextlib.suppress(HelmError, OSError):
                self.coordinator.capture_evidence(worker_id)
            try:
                self.client.tab_close(tab_id)
                closed.append(worker_id)
            except HerdrNotFound:
                closed.append(worker_id)
            except HerdrUnavailable:
                continue
            # Closing the tab is not the whole job. An interactive agent that
            # reported a result keeps its session, so the runner never writes
            # an exit record, and `_session_still_live` reads a worker without
            # one as live forever -- which pins its worktree behind
            # `helm task cleanup`. Recording the exit here is what lets the
            # directory be reclaimed later.
            with contextlib.suppress(HelmError):
                self.coordinator.stop_worker(
                    worker_id, "session released after a clean finish"
                )
        if closed:
            with self.coordinator.store.locked() as live:
                live_state = self._herdr_state(live)
                for worker_id in closed:
                    live_state.get("workers", {}).pop(worker_id, None)
        self._reconcile_paneless_sessions()
        return closed

    def _reconcile_paneless_sessions(self) -> None:
        """Record the exit of a settled worker that no longer has a pane.

        An interactive agent reports its result and keeps its session, so the
        runner never writes an exit record, and `_session_still_live` reads a
        worker without one as live forever. That is the right default -- a
        directory an agent is still writing in must not be removed underneath
        it -- but nothing ever resolved it, so the worker's directory could
        never be reclaimed. Eighteen of twenty on this root were stranded that
        way, and one of them held 15 GB of a spike's derived data.

        A worker whose tab Helm no longer has, or whose pane the provider says
        is gone, is not writing anywhere. That is evidence, not a guess, and it
        is the same evidence `poll_worker` already acts on for a running one.
        """
        data = self.coordinator.store.load()
        layouts = self._herdr_state(data)["workers"]
        for worker_id, worker in data.get("workers", {}).items():
            if worker.get("execution") != "herdr" or worker.get("status") == "running":
                continue
            exit_file = worker.get("exit_file")
            if not exit_file or Path(exit_file).exists():
                continue
            if worker_id in layouts and self._provider_worker_alive(worker) is not False:
                continue
            with contextlib.suppress(HelmError):
                self.coordinator.stop_worker(
                    worker_id, "session gone: no pane remains for it"
                )

    def relabel(self) -> list[dict[str, Any]]:
        """Rename Helm's own recorded spaces and tabs to the short scheme.

        Existing resources keep whatever label they were created with, so a
        naming change would otherwise only apply to future workers while the
        panel a human is actually looking at stays unreadable. Only IDs Helm
        recorded are touched; a user's own workspace is never renamed.
        """
        data = self.coordinator.store.load()
        state = self._herdr_state(data)
        renamed: list[dict[str, Any]] = []
        for project_id, layout in state.get("projects", {}).items():
            project = data["projects"].get(project_id)
            workspace_id = layout.get("workspace_id")
            if project is None or not workspace_id:
                continue
            label = self._workspace_label(project)
            try:
                self.client.workspace_rename(workspace_id, label)
                renamed.append({"kind": "workspace", "id": workspace_id, "label": label})
            except HerdrUnavailable as exc:
                renamed.append({"kind": "workspace", "id": workspace_id, "error": str(exc)})
        for worker_id, layout in state.get("workers", {}).items():
            tab_id = layout.get("tab_id")
            worker = data.get("workers", {}).get(worker_id)
            if not tab_id or worker is None:
                continue
            task = data.get("tasks", {}).get(worker.get("task_id"))
            label = self._worker_tab_label(task or {}, worker)
            try:
                self.client.tab_rename(tab_id, label)
                renamed.append({"kind": "tab", "id": tab_id, "label": label})
            except HerdrUnavailable as exc:
                renamed.append({"kind": "tab", "id": tab_id, "error": str(exc)})
        return renamed

    @staticmethod
    def _live_reviewer_for(data: dict[str, Any], task_id: str) -> dict[str, Any] | None:
        """The running reviewer already assigned to this task, if there is one.

        Reviewer tasks carry `reviews`, the id of what they review, so a
        reviewer is discoverable without parsing its brief. Only a *running*
        worker counts: a settled one is history, and the next round is
        deliberately a fresh reviewer task rather than a reopened session.
        """
        reviewing = {
            candidate["id"]
            for candidate in data.get("tasks", {}).values()
            if candidate.get("role") == "reviewer" and candidate.get("reviews") == task_id
        }
        if not reviewing:
            return None
        for worker in data.get("workers", {}).values():
            if worker.get("task_id") in reviewing and worker.get("status") == "running":
                return worker
        return None

    def _clear_stale_reviewers_for(self, task_id: str) -> list[dict[str, Any]]:
        """Stop failed/blocked reviewer sessions before launching a replacement.

        The one-live-reviewer guard correctly refuses a second reviewer while
        the first is recorded as running. It used to miss the opposite stale
        shape: a reviewer task had already reported blocked/failed, but its
        Herdr pane or runner process was still alive. Relaunching then left two
        PI agents attached to the same author checkout. A replacement review is
        the moment Helm knows that old session is no longer the driver, so
        settle it first and keep its log/workspace as evidence.
        """
        data = self.coordinator.store.load()
        stale: list[dict[str, Any]] = []
        reviewing = {
            candidate["id"]
            for candidate in data.get("tasks", {}).values()
            if candidate.get("role") == "reviewer" and candidate.get("reviews") == task_id
        }
        if not reviewing:
            return stale
        layouts = self._herdr_state(data).get("workers", {})
        for worker in data.get("workers", {}).values():
            if worker.get("task_id") not in reviewing:
                continue
            if worker.get("status") not in {"blocked", "failed"}:
                continue
            if worker.get("id") in layouts or self.coordinator._pid_alive(worker.get("pid")):
                stopped = self.stop_worker(
                    worker["id"],
                    "stale reviewer session superseded before launching a replacement review",
                )
                stale.append(stopped)
        return stale

    def _review_target(self, project: dict[str, Any], task: dict[str, Any]) -> str:
        """The commit a review diffs against, having checked there is a diff.

        Two separate false verdicts came out of naming a base *branch* here and
        trusting the task branch to hold the work.

        A base branch moves. The author measured against the commit it pointed
        at when the work started; the reviewer resolves the same name an hour
        later and gets a different tree, then reports a count as wrong when
        both numbers were right about different bases. Pinning a commit makes
        both sides measure the same thing.

        Which commit matters. The merge-base is the right answer only when the
        task branch was cut from the base branch, and Helm cuts it from the
        project's HEAD -- which can already carry work nobody has merged. On
        the first review that actually ran, that difference put a stranger's
        commit inside the diff: two commits and fourteen files reviewed where
        the author wrote one commit and four, and the whole verdict landed on
        somebody else's offline-recording change. Helm already records the
        commit the worktree was cut from, so prefer it, and fall back to the
        merge-base only when it is missing or no longer an ancestor of the
        branch -- after a rebase it is neither, and a base outside the branch's
        history would diff against nothing coherent.

        An empty target is worse and is refused outright. A reviewer handed a
        branch with no commits truthfully reports that there is nothing to
        review, Helm reads the leading word as APPROVED, and the result is a
        confident green over work nobody read -- which is more dangerous than
        having no review at all.
        """
        root = canonical(project["root"])
        base = ""
        pinned = str(task.get("base_revision") or "").strip()
        if pinned:
            # The test is ancestry, not existence, and the difference is not
            # academic: a rebase leaves the old base a perfectly resolvable
            # object that is no longer on the branch. Checking only that it
            # resolved put 426 commits into one review -- the same defect this
            # code exists to prevent, arriving after the branch moved.
            #
            # `merge-base --is-ancestor` answers by exit code, which `_git` does
            # not surface. Plain `merge-base` prints a sha, and it prints
            # exactly `pinned` when `pinned` is an ancestor -- so one command
            # covers ancestry and existence at once: a commit git cannot
            # resolve yields nothing, which is not equal to `pinned` either.
            if _git(root, "merge-base", pinned, task["branch"], check=False).strip() == pinned:
                base = pinned
        if not base:
            # Prefer the remote-tracking ref over the local branch of the same
            # name. A local `main` is only as fresh as the last fetch, and a
            # stale one is not a small error: here it sat 423 commits behind
            # after four days, so the merge-base against it put 425 commits,
            # 3,222 files and 172,000 lines in front of a reviewer asked to
            # judge a four-file change. The change is going to be merged into
            # the remote branch, so that is the thing to measure against.
            for candidate in (f"origin/{task['base_branch']}", task["base_branch"]):
                base = _git(
                    root, "merge-base", candidate, task["branch"], check=False
                ).strip()
                if base:
                    break
        if not base:
            # No shared history to diff against -- naming the branch is the
            # honest fallback, and the emptiness check below still applies.
            base = task["base_branch"]
        commits = _git(
            root, "rev-list", "--count", f"{base}..{task['branch']}", check=False
        ).strip()
        if commits in {"", "0"}:
            raise HelmError(
                f"refusing to review {task['branch']}: it holds no commits over "
                f"{base}. A reviewer would correctly report nothing to review and "
                "Helm would read that as approval. Point the review at the branch "
                "the work is actually on."
            )
        return base

    def close_project_space_if_finished(self, project_id: str) -> bool:
        """Close a project's space once its work is done and reported.

        Only a clean finish releases the space.  A failed or blocked task keeps
        it, because that pane is the evidence someone needs to diagnose the
        failure, and an approval-needed task keeps it because a human still has
        to look.  Set HELM_KEEP_SPACES=1 to always keep them.
        """
        if os.environ.get("HELM_KEEP_SPACES"):
            return False
        if not self.client.available():
            return False
        data = self.coordinator.store.load()
        for worker in data.get("workers", {}).values():
            if worker.get("project_id") == project_id and worker.get("status") == "running":
                return False
        tasks = [
            task for task in data.get("tasks", {}).values() if task.get("project_id") == project_id
        ]
        if not tasks:
            return False
        for task in tasks:
            # A task releases the space only once it is genuinely resolved:
            # delivered by a merge, or cleaned up.  "completed" is not enough,
            # because the work is still awaiting review and closing the space
            # would take the session away mid-review.  A failed or blocked task
            # holds the space as evidence until someone cleans it up, so old
            # failures stop pinning a space forever once they are dealt with.
            if task.get("status") in {"merged", "pr-merged"} or task.get("workspace_removed"):
                continue
            # Evidence has to actually exist to be worth keeping a space for.
            # A failed task whose pane is already gone -- released because it
            # settled, or closed by the user -- holds nothing, and pinning the
            # space for it retains an empty room for a diagnosis that is no
            # longer there.
            if not self._task_has_live_pane(data, task["id"]):
                continue
            return False
        if self._herdr_state(data)["projects"].get(project_id) is None:
            return False
        return self.cleanup_project(project_id)

    def close_finished_project_spaces(self) -> list[str]:
        """Close every recorded project space that no longer has work to show.

        A project can become empty without a task transition noticing: a
        coordinator may stop the last worker, or `helm watch` may release the
        final finished tab while no worker remains running. The per-project
        release check already knows the safety rules; this method makes routine
        sweeps apply it to all recorded spaces.
        """
        data = self.coordinator.store.load()
        project_ids = list(self._herdr_state(data).get("projects", {}).keys())
        closed: list[str] = []
        for project_id in project_ids:
            with contextlib.suppress(HelmError, SafetyError, OSError):
                if self.close_project_space_if_finished(project_id):
                    closed.append(project_id)
        return closed

    ANSWER_SETTLE_SECONDS = 1.5

    def answer_worker(self, worker_id: str, text: str) -> bool:
        """Deliver a coordinator answer into the worker's own session.

        `pane run` executes a command in a pane; talking to the agent living in
        it needs send-text followed by a separate Enter, because send-text only
        fills the input buffer.  Sending one without the other leaves the answer
        unsubmitted while the sender believes it was delivered.
        """
        if not self.client.available():
            return False
        send_text = getattr(self.client, "pane_send_text", None)
        send_keys = getattr(self.client, "pane_send_keys", None)
        if send_text is None or send_keys is None:
            return False
        data = self.coordinator.store.load()
        layout = self._herdr_state(data)["workers"].get(worker_id)
        if layout is None:
            return False
        self._owned(layout, f"tab for worker {worker_id}")
        pane = layout["pane_id"]
        # Escape first. An agent mid-execution treats an arriving paste as an
        # interruption, and the session then sits in an interrupted state where
        # the next text lands in a buffer that never submits -- the answer looks
        # delivered and the worker waits forever.
        with contextlib.suppress(HerdrUnavailable):
            send_keys(pane, "Escape")
            time.sleep(self.ANSWER_SETTLE_SECONDS)
        send_text(pane, text)
        # And a pause before Enter. Sent immediately, the newline races the
        # text and submits a fragment of it.
        time.sleep(self.ANSWER_SETTLE_SECONDS)
        send_keys(pane, "Enter")
        return True

    def route_worker_messages(self, worker_id: str) -> bool:
        """Deliver a worker's newly pushed messages without polling its process.

        Called from the worker's own `helm worker message` invocation, so a
        pushed update reaches the project's visible pane immediately instead of
        waiting for the coordinator to come looking.
        """
        if not self.client.available():
            return False
        data = self.coordinator.store.load()
        worker = data.get("workers", {}).get(worker_id)
        if worker is None or worker.get("execution") != "herdr":
            return False
        self._route_messages(worker)
        return True

    def poll_worker(self, worker_id: str) -> dict[str, Any]:
        worker = self.coordinator.poll_worker(worker_id)
        if worker.get("execution") == "herdr" and worker.get("status") == "running":
            alive = self._provider_worker_alive(worker)
            if alive is False:
                worker = self.coordinator.mark_worker_lost(
                    worker_id, "Herdr pane or session disappeared before an exit record arrived"
                )
        if worker.get("execution") == "herdr":
            self._route_messages(worker)
            # A worker's final push happens while it is still running, so the
            # push path can never be the moment a project becomes idle.  Check
            # again once it actually reaches a terminal state.
            if worker.get("status") != "running":
                with contextlib.suppress(HelmError):
                    self.close_project_space_if_finished(worker["project_id"])
        return worker

    def wait_worker(self, worker_id: str, timeout: float | None = None) -> dict[str, Any]:
        """Wait for a terminal state, on the same contract as `Coordinator.wait_worker`.

        A budget running out means *this call* stopped waiting.  It is not
        evidence that the worker died, and it must never be recorded as if it
        were.  An agent worker takes minutes, so a bounded wait that fails the
        assignment on expiry durably killed healthy workers -- marking the task
        failed and writing them a returncode-1 exit record -- while they were
        still working in their pane and still pushing messages.

        A worker that genuinely disappeared is caught inside the loop instead:
        `poll_worker` asks the provider and fails the assignment only when the
        answer is a definite no.  That check is the one holding real evidence,
        which is why this one does not need to guess.

        `timeout=None` means wait until the worker is terminal, matching the
        coordinator's method of the same name.  Two `wait_worker`s whose `None`
        meant opposite things is what let a five-second probe stand in for
        `--wait`.
        """
        started = time.monotonic()
        while True:
            worker = self.poll_worker(worker_id)
            if worker["status"] != "running":
                return worker
            if timeout is not None and time.monotonic() - started >= timeout:
                return worker
            time.sleep(0.05)

    def cleanup_task(self, task_id: str) -> bool:
        data = self.coordinator.store.load()
        live = [
            worker for worker in data.get("workers", {}).values()
            if worker.get("task_id") == task_id and worker.get("status") == "running"
        ]
        if live:
            raise SafetyError(f"refusing Herdr cleanup while worker {live[0]['id']} is still running")
        state = self._herdr_state(data)
        task_workers = [
            worker
            for worker in state["workers"].values()
            if worker.get("task_id") == task_id
        ]
        cleaned = False
        for worker in task_workers:
            if worker.get("owned") is not True:
                continue
            self.client.tab_close(worker["tab_id"])
            with self.coordinator.store.locked() as current:
                self._herdr_state(current)["workers"].pop(worker["worker_id"], None)
            cleaned = True
        return cleaned

    def cleanup_project(self, project_id: str) -> bool:
        data = self.coordinator.store.load()
        live = [
            worker for worker in data.get("workers", {}).values()
            if worker.get("project_id") == project_id and worker.get("status") == "running"
        ]
        if live:
            raise SafetyError(f"refusing Herdr project cleanup while worker {live[0]['id']} is still running")
        state = self._herdr_state(data)
        project = state["projects"].get(project_id)
        if project is None:
            return False
        self._owned(project, f"workspace for project {project_id}")
        workers = [
            worker
            for worker in state["workers"].values()
            if worker.get("project_id") == project_id
        ]
        # A resource that is already gone is the outcome this method wants, not
        # a failure -- and after a human closes the space by hand, every one of
        # these is gone. Raising here would make the stale records permanent,
        # which is the opposite of cleaning up.
        for worker in workers:
            if worker.get("owned") is True:
                with contextlib.suppress(HerdrNotFound):
                    self.client.tab_close(worker["tab_id"])
        with contextlib.suppress(HerdrNotFound):
            self.client.workspace_close(project["workspace_id"])
        with self.coordinator.store.locked() as current:
            current_state = self._herdr_state(current)
            current_state["workers"] = {
                key: value
                for key, value in current_state["workers"].items()
                if value.get("project_id") != project_id or value.get("owned") is not True
            }
            current_state["projects"].pop(project_id, None)
        return True

    def cleanup_coordinator(self) -> bool:
        """Close a legacy standalone coordinator workspace.

        Helm now keeps one workspace per project and routes messages into that
        project's own overview pane, so no coordinator workspace is created.
        This remains so a root recorded by an older version can close the one it
        already owns.
        """
        data = self.coordinator.store.load()
        record = self._herdr_state(data).get("coordinator")
        if record is None:
            return False
        self._owned(record, "coordinator workspace")
        self.client.workspace_close(record["workspace_id"])
        with self.coordinator.store.locked() as current:
            self._herdr_state(current)["coordinator"] = None
        return True
