"""A blocked task is not launched until what it builds on has landed."""

from __future__ import annotations

import contextlib
import io
import sys

from helm import cli
from helm.errors import HelmError
from tests.support import HelmTestCase


class BlockedByTests(HelmTestCase):
    def _shipping_worker(self, task: dict, name: str) -> None:
        code = (
            "from pathlib import Path; import subprocess; "
            f"Path('{name}.txt').write_text('w'); subprocess.run(['git','add','{name}.txt'],check=True); "
            "subprocess.run(['git','commit','-qm','w'],check=True)"
        )
        self.coordinator.launch_worker(task["id"], [sys.executable, "-c", code])

    def test_a_task_launches_only_once_its_blockers_are_delivered(self) -> None:
        root = self.repo("chain")
        project = self.coordinator.register_project("Chain", str(root), project_id="chain")
        first = self.coordinator.create_task(project["id"], "the schema and the service")
        second = self.coordinator.create_task(project["id"], "the dashboard on top", blocked_by=[first["id"]])
        self.assertEqual(second["blocked_by"], [first["id"]])
        with self.assertRaisesRegex(HelmError, f"blocked by {first['id']}"):
            self.coordinator.launch_worker(second["id"], [sys.executable, "-c", ""])
        self.assertEqual(self.state.load()["tasks"][second["id"]]["status"], "created")
        self._shipping_worker(first, "first")
        # Completed is a milestone, not delivery: the base does not carry it yet.
        with self.assertRaisesRegex(HelmError, "deliver those first"):
            self.coordinator.launch_worker(second["id"], [sys.executable, "-c", ""])
        self.coordinator.approve_task(first["id"], "reviewed")
        self.coordinator.merge_task(first["id"])
        self.assertEqual(self.coordinator.open_blockers(self.state.load(), second), [])
        worker = self.coordinator.launch_worker(second["id"], [sys.executable, "-c", ""])
        self.assertEqual(worker["status"], "completed")
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(cli.main(["--state-dir", str(self.state.directory), "task", "inspect", second["id"]]), 0)
        self.assertIn(f"blocked by: {first['id']}", out.getvalue())

    def test_a_blocker_must_be_a_known_task_of_the_same_project(self) -> None:
        root = self.repo("one")
        other_root = self.repo("two")
        one = self.coordinator.register_project("One", str(root), project_id="one")
        two = self.coordinator.register_project("Two", str(other_root), project_id="two")
        foreign = self.coordinator.create_task(two["id"], "elsewhere")
        with self.assertRaisesRegex(HelmError, "unknown task to be blocked by"):
            self.coordinator.create_task(one["id"], "x", blocked_by=["t-000000000000"])
        with self.assertRaisesRegex(HelmError, "own project"):
            self.coordinator.create_task(one["id"], "x", blocked_by=[foreign["id"]])
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
            code = cli.main([
                "--state-dir", str(self.state.directory), "task", "create", "--project", one["id"],
                "--brief", "y", "--read-only", "--blocked-by", foreign["id"],
            ])
        self.assertNotEqual(code, 0)
        self.assertIn("own project", out.getvalue())
