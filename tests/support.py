"""Shared fixtures for the Helm test suite.

Every test module subclasses :class:`HelmTestCase`, which owns the temporary
Helm root, the fake Herdr provider and the Git plumbing the suite builds its
fixtures from. Helpers used by a single module live in that module instead.
"""

from __future__ import annotations

import contextlib
import json
import os
import shlex
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from helm.core import Coordinator, StateStore, inside
from helm.herdr import HerdrNotFound, HerdrUnavailable

#: The checkout under test, so a suite run from another directory reads the
#: domains and sources it is actually testing rather than whatever happens to
#: sit under the current working directory.
REPO_ROOT = Path(__file__).resolve().parents[1]
SHIPPED_DOMAINS = REPO_ROOT / "domains"


class FakeHerdr:
    def __init__(self, available: bool = True) -> None:
        self.is_available = available
        self.next_id = 0
        self.missing: set[str] = set()
        self.unreachable = False
        # Set False to model a pane whose shell swallowed the command.
        self.runs_start_the_runner = True
        self.workspaces: list[tuple[str, str]] = []
        self.tabs: list[tuple[str, str, str, str]] = []
        self.runs: list[tuple[str, str]] = []
        self.closed_tabs: list[str] = []
        self.closed_workspaces: list[str] = []
        self.renamed: list[tuple[str, str]] = []
        self.sent_text: list[tuple[str, str]] = []
        self.sent_keys: list[tuple[str, str]] = []

    def available(self) -> bool:
        return self.is_available

    def _id(self, prefix: str) -> str:
        self.next_id += 1
        return f"{prefix}-opaque-{self.next_id}"

    def workspace_get(self, workspace_id: str) -> dict[str, object]:
        if self.unreachable:
            raise HerdrUnavailable("herdr server unreachable")
        if workspace_id in self.missing or workspace_id in self.closed_workspaces:
            raise HerdrNotFound("workspace_not_found")
        return {"result": {"workspace": {"workspace_id": workspace_id}}}

    def workspace_create(self, label: str) -> dict[str, object]:
        workspace_id = self._id("workspace")
        tab_id = self._id("tab")
        pane_id = self._id("pane")
        self.workspaces.append((workspace_id, label))
        return {
            "result": {
                "workspace": {"workspace_id": workspace_id},
                "tab": {"tab_id": tab_id},
                "root_pane": {"pane_id": pane_id},
            }
        }

    def tab_rename(self, tab_id: str, label: str) -> dict[str, object]:
        self.renamed.append((tab_id, label))
        return {"result": {"type": "ok"}}

    def tab_create(self, workspace_id: str, label: str, cwd: str) -> dict[str, object]:
        tab_id = self._id("tab")
        pane_id = self._id("pane")
        self.tabs.append((workspace_id, label, cwd, tab_id))
        return {"result": {"tab": {"tab_id": tab_id}, "root_pane": {"pane_id": pane_id}}}

    def pane_run(self, pane_id: str, command: str) -> dict[str, object]:
        self.runs.append((pane_id, command))
        # A real pane actually runs the command, and the runner's first act is
        # to write its banner to the log. Helm now treats a log that stays
        # empty as "the process never started", so the fake has to model a
        # command that runs -- otherwise every Herdr test looks like a launch
        # the shell swallowed.
        if not self.runs_start_the_runner:
            return {}
        parts = shlex.split(command)
        if "--config" in parts:
            config_path = Path(parts[parts.index("--config") + 1])
            with contextlib.suppress(OSError, ValueError, KeyError):
                config = json.loads(config_path.read_text(encoding="utf-8"))
                Path(config["log"]).write_text("[helm] worker started\n", encoding="utf-8")
        return {}

    def pane_send_text(self, pane_id: str, text: str) -> dict[str, object]:
        self.sent_text.append((pane_id, text))
        return {}

    def pane_send_keys(self, pane_id: str, keys: str) -> dict[str, object]:
        self.sent_keys.append((pane_id, keys))
        return {}

    def tab_close(self, tab_id: str) -> dict[str, object]:
        self.closed_tabs.append(tab_id)
        return {}

    def workspace_close(self, workspace_id: str) -> dict[str, object]:
        self.closed_workspaces.append(workspace_id)
        return {}



class HelmTestCase(unittest.TestCase):
    """Base case owning the temporary Helm root shared by every module."""

    def setUp(self) -> None:
        # `helm run` now defaults to the Herdr path, so an unguarded suite run
        # inside a Herdr session would create real workspaces on the developer's
        # machine.  Tests must never reach a live provider.  HELM_AGENT=none
        # additionally stops runtime auto-detection from delegating a test task
        # to the real agent CLI this suite happens to be running under; the
        # tests that exercise selection set it themselves.
        herdr_env = mock.patch.dict(os.environ, {"HERDR_ENV": "0", "HELM_AGENT": "none"})
        herdr_env.start()
        self.addCleanup(herdr_env.stop)
        self.temp = tempfile.TemporaryDirectory()
        base = Path(self.temp.name)
        self.state = StateStore(base / "state")
        self.coordinator = Coordinator(self.state)
        self.repos: list[Path] = []

    def tearDown(self) -> None:
        self.temp.cleanup()

    def repo(self, name: str, *, non_git: bool = False) -> Path:
        root = Path(self.temp.name) / name
        root.mkdir()
        if non_git:
            return root
        subprocess.run(["git", "init", "-q", str(root)], check=True)
        subprocess.run(["git", "-C", str(root), "config", "user.name", "Test"], check=True)
        subprocess.run(["git", "-C", str(root), "config", "user.email", "test@example.invalid"], check=True)
        (root / "README.txt").write_text(name)
        subprocess.run(["git", "-C", str(root), "add", "README.txt"], check=True)
        subprocess.run(["git", "-C", str(root), "commit", "-qm", "initial"], check=True)
        self.repos.append(root)
        return root

    def commit_on_task_branch(self, task: dict, text: str = "worker change") -> None:
        """Put a commit on a task branch so there is something to review.

        A review of a branch holding no commits is refused, because a reviewer
        correctly reports nothing to review and Helm reads that as approval.
        """
        workspace = Path(task["workspace"])
        (workspace / "change.txt").write_text(text, encoding="utf-8")
        subprocess.run(["git", "-C", str(workspace), "add", "-A"], check=True)
        subprocess.run(
            ["git", "-C", str(workspace), "commit", "-qm", text], check=True
        )

    def _run_git(self, root: Path, *args: str) -> str:
        proc = subprocess.run(
            ["git", "-C", str(root), *args],
            check=True, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )
        return proc.stdout.strip()

    def _helm_root(self, name: str = "helm") -> Path:
        root = Path(self.temp.name) / name
        root.mkdir()
        (root / "projects").mkdir()
        StateStore(root / "state").initialize_root(root)
        return root

    def _completed_task_awaiting_approval(self, name: str) -> tuple[Path, dict, dict]:
        root = self.repo(name)
        project = self.coordinator.register_project(name, str(root), project_id=name)
        task = self.coordinator.create_task(project["id"], "commit a change")
        code = (
            "from pathlib import Path; import subprocess; "
            "Path('change.txt').write_text('worker'); "
            "subprocess.run(['git','add','change.txt'],check=True); "
            "subprocess.run(['git','commit','-m','worker change'],check=True)"
        )
        self.coordinator.launch_worker(task["id"], [sys.executable, "-c", code])
        return root, project, task

    def _domain_root_project(self, name: str) -> tuple[Path, dict[str, Any]]:
        helm_root = self._helm_root(f"{name}-root")
        project_root = self.repo(name)
        destination = helm_root / "projects" / name
        shutil.move(str(project_root), str(destination))
        coordinator = Coordinator(StateStore(helm_root / "state", helm_root=helm_root))
        project = coordinator.discover_project(helm_root, name)
        self._coordinator = coordinator
        return helm_root, project

    def _shipped_domains_project(self, name: str) -> tuple[Coordinator, dict]:
        """A project whose domain root is the pack this repository ships.

        The thing under test is the wiring in `domains/*/domain.json`, so a
        fixture domain would prove nothing about it. Copy the real pack.
        """
        helm_root = self._helm_root(f"{name}-root")
        shutil.rmtree(helm_root / "domains")
        shutil.copytree(SHIPPED_DOMAINS, helm_root / "domains")
        shutil.move(str(self.repo(name)), str(helm_root / "projects" / name))
        coordinator = Coordinator(StateStore(helm_root / "state", helm_root=helm_root))
        return coordinator, coordinator.discover_project(helm_root, name)

    @staticmethod
    def _flat(text: str) -> str:
        """Collapse wrapping so a prose assertion survives a reflowed paragraph.

        These tests assert on guidance sentences, which are hard-wrapped in
        their source files and escaped again by `json.dumps`. Matching the
        wrapping instead of the sentence makes every reflow a false failure,
        and tempts the next reader to weaken the assertion to a fragment.
        """
        return " ".join(text.replace("\\n", " ").split())

    def _composed(self, coordinator: Coordinator, project: dict, task: dict) -> str:
        return self._flat(
            json.dumps(coordinator._context(project, task, f"w-{task['id']}"))
        )
