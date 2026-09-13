"""A project's declared checks are the worker's to run and Helm's to judge."""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path

from helm.core import Coordinator
from helm.errors import HelmError, SafetyError
from helm.state import StateStore
from tests.support import HelmTestCase


class DeclaredChecksTests(HelmTestCase):
    def _rooted(self, name: str, checks) -> tuple[Path, Coordinator, dict]:
        helm_root = self._helm_root(f"{name}-root")
        shutil.move(str(self.repo(name)), str(helm_root / "projects" / name))
        (helm_root / "projects" / name / ".helm").mkdir()
        (helm_root / "projects" / name / ".helm" / "project.json").write_text(
            json.dumps({"checks": checks}), encoding="utf-8"
        )
        coordinator = Coordinator(StateStore(helm_root / "state", helm_root=helm_root))
        coordinator.discover_project(helm_root, name)
        return helm_root, coordinator, coordinator.get_project(name)

    def _worked(self, coordinator: Coordinator, project: dict, brief: str, **kwargs) -> tuple[dict, str]:
        task = coordinator.create_task(project["id"], brief, **kwargs)
        code = (
            "from pathlib import Path; import subprocess; "
            "Path('change.txt').write_text('worker'); "
            "subprocess.run(['git','add','change.txt'],check=True); "
            "subprocess.run(['git','commit','-qm','worker change'],check=True)"
        )
        worker = coordinator.launch_worker(task["id"], [sys.executable, "-c", code])
        tip = subprocess.run(
            ["git", "-C", task["workspace"], "rev-parse", "HEAD"], text=True, capture_output=True, check=True
        ).stdout.strip()
        return task, tip[:10], worker

    def test_the_declaration_is_read_handed_to_the_worker_and_gates_approval(self) -> None:
        helm_root, coordinator, project = self._rooted("checked", [
            {"name": "unit", "command": "make test", "cases": True},
            "make lint",
        ])
        self.assertEqual(
            project["checks"],
            [
                {"name": "unit", "command": "make test", "cases": True},
                {"name": "make", "command": "make lint", "cases": False},
            ],
        )
        task, tip, worker = self._worked(coordinator, project, "a feature")
        context = json.loads(Path(worker["context_file"]).read_text(encoding="utf-8"))
        handed = context["task"]["checks"]
        self.assertEqual([c["name"] for c in handed], ["unit", "make"])
        self.assertIn(f"helm task evidence {task['id']} --tip <sha> --check unit", handed[0]["record_with"])
        self.assertIn("--cases <n>", handed[0]["record_with"])
        self.assertTrue(handed[0]["gates_approval"])

        with self.assertRaisesRegex(SafetyError, "no green record.*unit.*make"):
            coordinator.approve_task(task["id"], "ok")
        # A green run of the suite that says nothing about what ran is not the unit check.
        coordinator.record_task_evidence(task["id"], tip=tip, command="make test", exit_code=0, check="unit")
        coordinator.record_task_evidence(task["id"], tip=tip, command="make lint", exit_code=0)
        with self.assertRaisesRegex(SafetyError, "unit \\(`make test`, with --cases\\)"):
            coordinator.approve_task(task["id"], "ok")
        coordinator.record_task_evidence(task["id"], tip=tip, command="make test", exit_code=0, check="unit", cases=12)
        self.assertEqual(coordinator.approve_task(task["id"], "ok")["status"], "approved")

    def test_a_small_task_runs_the_checks_but_is_not_gated_on_them(self) -> None:
        helm_root, coordinator, project = self._rooted("smallcheck", ["make test"])
        task, tip, worker = self._worked(coordinator, project, "rename a label", shape="small")
        context = json.loads(Path(worker["context_file"]).read_text(encoding="utf-8"))
        self.assertFalse(context["task"]["checks"][0]["gates_approval"])
        self.assertEqual(coordinator.approve_task(task["id"], "ok")["status"], "approved")

    def test_a_bad_declaration_is_refused_at_discovery(self) -> None:
        for index, (bad, message) in enumerate((
            ("make test", "must be a JSON list"),
            ([{"name": "unit"}], "needs a one-line command"),
            ([{"name": "bad name!", "command": "x"}], "needs a short name"),
            ([{"name": "unit", "command": "x"}, {"name": "unit", "command": "y"}], "twice"),
            ([{"name": "unit", "command": "x", "cases": "yes"}], "cases must be true or false"),
        )):
            name = f"badcheck{index}"
            helm_root = self._helm_root(f"{name}-root")
            shutil.move(str(self.repo(name)), str(helm_root / "projects" / name))
            (helm_root / "projects" / name / ".helm").mkdir()
            (helm_root / "projects" / name / ".helm" / "project.json").write_text(
                json.dumps({"checks": bad}), encoding="utf-8"
            )
            coordinator = Coordinator(StateStore(helm_root / "state", helm_root=helm_root))
            with self.assertRaisesRegex(HelmError, message, msg=str(bad)):
                coordinator.discover_project(helm_root, name)
