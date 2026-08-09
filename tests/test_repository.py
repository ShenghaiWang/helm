"""Root/state security, file modes and the tracked repository contract."""

from __future__ import annotations

import contextlib
import os
import json
import re
import shutil
import subprocess
import sys
import threading
from pathlib import Path
from unittest import mock

from helm import runtimes
from helm.core import (
    DELIVERY_DECISION_PROJECT_TEXT,
    DELIVERY_DECISION_TASK_TEXT,
    FOREMAN_RULES,
    Coordinator,
    HelmError,
    SafetyError,
    StateStore,
    inside,
)

from tests.support import FakeHerdr, HelmTestCase, REPO_ROOT, SHIPPED_DOMAINS


class RepositoryTests(HelmTestCase):
    def test_root_state_namespace_and_project_containment_are_enforced(self) -> None:
        helm_root = self._helm_root("root-one")
        other_root = Path(self.temp.name) / "root-two"
        other_root.mkdir()
        with self.assertRaises(SafetyError):
            StateStore(helm_root / "state", helm_root=other_root)
        with self.assertRaises(SafetyError):
            StateStore(other_root / "state", helm_root=helm_root)
        root_coordinator = Coordinator(StateStore(helm_root / "state", helm_root=helm_root))
        for name in ("state", "domains", "agents"):
            candidate = helm_root / name / "nested"
            candidate.mkdir(parents=True)
            with self.assertRaises(SafetyError):
                root_coordinator.register_project(name, str(candidate), project_id=f"bad-{name}")

    def test_private_state_and_worker_files_have_owner_only_modes(self) -> None:
        root = self.repo("private-files")
        project = self.coordinator.register_project("Private", str(root), project_id="private")
        task = self.coordinator.create_task(project["id"], "private output")
        worker = self.coordinator.launch_worker(task["id"], [sys.executable, "-c", ""])
        paths = [
            self.state.directory,
            self.state.lock_file,
            self.state.state_file,
            Path(worker["context_file"]).parent,
            Path(worker["context_file"]),
            Path(worker["log_file"]),
            Path(worker["config_file"]),
            Path(worker["exit_file"]),
        ]
        for path in paths:
            self.assertEqual(path.stat().st_mode & 0o777, 0o700 if path.is_dir() else 0o600, str(path))

    def test_knowledge_symlink_outside_allowed_root_is_rejected(self) -> None:
        root = self.repo("symlink-context")
        project = self.coordinator.register_project("Symlink", str(root), project_id="symlink")
        settings = root / ".helm"
        settings.mkdir()
        outside = Path(self.temp.name) / "outside-knowledge"
        outside.write_text("secret outside project")
        (settings / "knowledge.md").symlink_to(outside)
        task = self.coordinator.create_task(project["id"], "read project context")
        with self.assertRaises(SafetyError):
            self.coordinator.launch_worker(task["id"], [sys.executable, "-c", ""])
        self.assertEqual(self.coordinator.inspect_task(task["id"])["task"]["status"], "created")

    def test_launch_validation_and_process_failure_roll_back_cleanly(self) -> None:
        root = self.repo("launch-rollback")
        project = self.coordinator.register_project("Rollback", str(root), project_id="rollback")
        task = self.coordinator.create_task(project["id"], "missing command")
        with mock.patch.dict("os.environ", {"HELM_WORKER_COMMAND": ""}, clear=False):
            with self.assertRaises(HelmError):
                self.coordinator.launch_worker(task["id"], None)
        inspected = self.coordinator.inspect_task(task["id"])
        self.assertEqual(inspected["task"]["status"], "created")
        self.assertFalse(Path(inspected["task"]["workspace"]).exists())
        self.coordinator.allocate_task(task["id"])
        with mock.patch("helm.core.subprocess.Popen", side_effect=OSError("spawn failed")):
            with self.assertRaises(HelmError):
                self.coordinator.launch_worker(task["id"], [sys.executable, "-c", ""])
        inspected = self.coordinator.inspect_task(task["id"])
        self.assertEqual(inspected["task"]["status"], "allocated")
        self.assertEqual(inspected["workers"], [])

    def test_agent_check_is_read_only_and_setup_docs_are_copyable(self) -> None:
        root = self.repo("agent-check")
        project = self.coordinator.register_project("Agent", str(root), project_id="agent")
        (Path(self.temp.name) / "agents.json").write_text(json.dumps({
            "agents": [{"id": "local", "command": [sys.executable, "-c", ""]}]
        }))
        # This store has no configured root, so verify the public no-allocation
        # behavior separately through the documented Helm-root layout below.
        helm_root = self._helm_root("agent-helm")
        destination = helm_root / "projects" / "agent2"
        shutil.copytree(root, destination)
        (helm_root / "agents.json").write_text(json.dumps({
            "agents": [{"id": "local", "command": [sys.executable, "-c", ""]}]
        }))
        coordinator = Coordinator(StateStore(helm_root / "state", helm_root=helm_root))
        self.assertEqual(len(coordinator.agent_availability()), 1)
        self.assertEqual(coordinator.list_projects(), [])
        self.assertIn("helm agent check", Path("README.md").read_text())
        self.assertIn("domains/publishing/knowledge.md", Path("README.md").read_text())

    def test_default_root_layout_and_ignore_rules(self) -> None:
        expected = {"projects", "domains", "agents", "state"}
        self.assertEqual(
            {path.name for path in Path.cwd().iterdir() if path.is_dir()} & expected,
            expected,
        )
        for name in expected:
            self.assertTrue((Path(name) / ".gitkeep").is_file(), name)
        for path in (
            "projects/example/.git/config",
            "agents/local/profile.json",
            "state/state.json",
            "state/.lock",
            "state/worktrees/task/output.txt",
        ):
            result = subprocess.run(
                ["git", "check-ignore", "--no-index", "--quiet", path], check=False
            )
            self.assertEqual(result.returncode, 0, path)
        for name in expected:
            self.assertNotEqual(
                subprocess.run(
                    ["git", "check-ignore", "--no-index", "--quiet", f"{name}/.gitkeep"],
                    check=False,
                ).returncode,
                0,
                name,
            )
        # Domain knowledge is repository content: it is committed so it travels
        # with the repo instead of living on one machine. Private material
        # belongs in a project's own .helm/knowledge.md, which is not tracked.
        for shared in ("domains/anything/knowledge.md", "domains/code-review/knowledge.md"):
            self.assertNotEqual(
                subprocess.run(
                    ["git", "check-ignore", "--no-index", "--quiet", shared], check=False
                ).returncode,
                0,
                shared,
            )

    def test_a_private_file_is_never_observable_half_written(self) -> None:
        """A reader sees the old file or the new one, never an empty one.

        `poll_worker` reads `exit.json` the moment it exists and reads its
        absence of content as an unreadable exit record -- which marked a
        worker that had exited 0 as failed, failing its task and emitting a
        failure message for work that had actually succeeded.
        """
        from helm.core import _write_private_text

        target = Path(self.temp.name) / "exit.json"
        _write_private_text(target, json.dumps({"returncode": 0}) + "\n")
        payload = json.dumps({"returncode": 0, "detail": "x" * 200_000}) + "\n"
        seen: list[str] = []
        stop = threading.Event()

        def reader() -> None:
            while not stop.is_set():
                with contextlib.suppress(OSError):
                    if target.exists():
                        seen.append(target.read_text(encoding="utf-8"))

        watcher = threading.Thread(target=reader, daemon=True)
        watcher.start()
        try:
            for _ in range(25):
                _write_private_text(target, payload)
        finally:
            stop.set()
            watcher.join(timeout=5)

        self.assertTrue(seen, "the reader never managed to observe the file")
        for observed in seen:
            # Every observation parses: no truncated or empty intermediate.
            self.assertEqual(json.loads(observed)["returncode"], 0)
        self.assertEqual(oct(target.stat().st_mode & 0o777), oct(0o600))

    def test_tracked_helm_files_do_not_capture_managed_project_details(self) -> None:
        """Helm is generic product code; managed-project facts stay local."""
        tracked = subprocess.run(
            ["git", "ls-files"],
            text=True,
            stdout=subprocess.PIPE,
            check=True,
        ).stdout.splitlines()
        public_files = [
            path
            for path in tracked
            if not path.startswith(("projects/", "state/", "agents/"))
        ]
        private_patterns = [
            r"\byt\b",
            "DSK" + r"-\d+",
            "APP" + r"-\d+",
            "INT" + r"-\d+",
            "MOD" + r"-\d+",
            "slack" + "-payload",
        ]
        offenders: list[str] = []
        for path in public_files:
            try:
                text = Path(path).read_text(encoding="utf-8")
            except UnicodeDecodeError:
                continue
            for pattern in private_patterns:
                if re.search(pattern, text):
                    offenders.append(f"{path}: {pattern}")
        self.assertEqual(offenders, [])

    def test_delivery_decision_surface_carries_no_concrete_project_state(self) -> None:
        """The gate is generic product code, so nothing real may ride in it.

        A decision item is assembled from constants and printed into docs and
        panes, which makes it an easy place for one root's task, worker, or
        ticket identifier to become a committed example.
        """
        tracked = subprocess.run(
            ["git", "ls-files"], text=True, stdout=subprocess.PIPE, check=True
        ).stdout.splitlines()
        surfaces = [
            path
            for path in tracked
            if path in {"README.md", "AGENTS.md"}
            or path.startswith(("helm/", "domains/", "docs/"))
        ]
        # Helm's own generated identifiers: a real one in a tracked file is a
        # managed root's state that escaped into the product.
        identifiers = re.compile(r"\b[twmisag]-[0-9a-f]{8,}\b")
        offenders = []
        for path in surfaces:
            try:
                text = Path(path).read_text(encoding="utf-8")
            except UnicodeDecodeError:
                continue
            offenders.extend(f"{path}: {hit}" for hit in identifiers.findall(text))
        self.assertEqual(offenders, [])
        for text in (
            DELIVERY_DECISION_TASK_TEXT,
            DELIVERY_DECISION_PROJECT_TEXT,
        ):
            self.assertFalse(identifiers.search(text))
            self.assertLessEqual(len(text), Coordinator.SITUATION_LINE_LIMIT)

    def test_the_delivery_decision_is_documented_where_agents_read_it(self) -> None:
        readme = Path("README.md").read_text(encoding="utf-8")
        agents = Path("AGENTS.md").read_text(encoding="utf-8")
        for required in (
            "**delivery decision**",
            "no driver\nis left",
            "never auto-closed",
            "before any of that runs",
            "only copy",
        ):
            self.assertIn(required, readme)
        for required in (
            "worker → foreman → Helm",
            "delivery decision",
            "declined one",
            "routes it before",
            "only copy",
        ):
            self.assertIn(required, agents)
        # A foreman has to know its final report is the handover, or it stops
        # at "done" and the decision is never raised.
        self.assertIn("handover", FOREMAN_RULES)
        # Delivered is not finalized: both surfaces have to say cleanup is a
        # decision a human still owes, or a coordinator calls a merge the end.
        for required in (
            "Delivery is not finalization",
            "helm task cleanup <task>",
            "not finalized until",
        ):
            self.assertIn(required, readme)
        for required in (
            "Delivered is not finalized",
            "helm task cleanup <task>",
            "approved cleanup decision is resolved",
        ):
            self.assertIn(required, agents)

    def test_repository_native_agent_path_requires_no_worker_configuration(self) -> None:
        readme = Path("README.md").read_text(encoding="utf-8")
        agents = Path("AGENTS.md").read_text(encoding="utf-8")
        native = readme.split("## Optional Helm CLI", 1)[0]
        quickstart = readme.split("## Helm root layout", 1)[0]

        self.assertIn("start any supported agent", native)
        self.assertIn("does not require Python", native)
        self.assertNotIn("HELM_WORKER_COMMAND", quickstart)
        self.assertNotIn("agents.json", quickstart)
        self.assertNotIn("helm run", quickstart)
        for required in (
            "direct children of `projects/`",
            "committed Git repository",
            "helm init",
            "assigned task worktree",
            "domain defaults",
            "merge, publish, push",
            "HELM_WORKER_COMMAND",
            "HERDR_ENV=1",
            "another project",
        ):
            self.assertIn(required, agents)
        self.assertIn("docs/agent-adapters.md", readme)

    def test_runtime_selection_rules_are_documented_where_agents_read_them(self) -> None:
        readme = Path("README.md").read_text(encoding="utf-8")
        agents = Path("AGENTS.md").read_text(encoding="utf-8")
        for required in (
            "Choosing the worker's agent runtime",
            '"agent": "codex"',
            "HELM_AGENT",
            "same runtime the",
            "never invented",
        ):
            self.assertIn(required, agents)
        for required in (
            "## Agent runtimes",
            "helm/runtimes.py",
            "--agent pi",
            "interactive form inside a Herdr pane",
        ):
            self.assertIn(required, readme)
        # The built-in table is the documented list, so the docs must not
        # advertise a runtime Helm cannot actually start.
        from helm import runtimes

        for runtime_id in runtimes.builtin_runtime_ids():
            self.assertIn(f"`{runtime_id}`", readme)
