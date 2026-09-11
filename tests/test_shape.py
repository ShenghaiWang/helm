"""A task's shape sizes its ceremony: rounds, effort floor, evidence gate."""

from __future__ import annotations

import contextlib
import io
import subprocess
import sys
from unittest import mock

from helm import cli
from helm.errors import HelmError, SafetyError
from helm.herdr import HerdrAdapter
from helm.values import SHAPE_POLICY, TASK_SHAPES, shape_policy
from tests.support import FakeHerdr, HelmTestCase


class ShapeTests(HelmTestCase):
    def _project(self, name: str):
        root = self.repo(name)
        return self.coordinator.register_project(name.title(), str(root), project_id=name)

    def test_a_task_records_its_shape_and_reason_and_defaults_to_standard(self) -> None:
        project = self._project("shaped")
        plain = self.coordinator.create_task(project["id"], "fix a typo")
        self.assertEqual(plain["shape"], "standard")
        self.assertEqual(shape_policy(plain)["review_rounds"], 2)
        small = self.coordinator.create_task(
            project["id"], "swap the icon", shape="small", shape_reason="one asset, visible at a glance"
        )
        self.assertEqual(small["shape"], "small")
        self.assertEqual(small["shape_reason"], "one asset, visible at a glance")
        with self.assertRaisesRegex(HelmError, "unknown task shape"):
            self.coordinator.create_task(project["id"], "x", shape="huge")
        self.assertEqual(TASK_SHAPES, ("small", "standard", "critical"))
        for name in TASK_SHAPES:
            self.assertIn("means", SHAPE_POLICY[name])

    def test_the_shape_sets_the_effort_floor_unless_the_task_names_one(self) -> None:
        project = self._project("effortful")
        small = self.coordinator.create_task(project["id"], "swap the icon", shape="small")
        effort, reason, source = self.coordinator._resolve_effort(project, small)
        self.assertEqual((effort, source), ("low", "shape"))
        self.assertIn("small", reason)
        critical = self.coordinator.create_task(project["id"], "rotate the tokens", shape="critical")
        self.assertEqual(self.coordinator._resolve_effort(project, critical)[0], "high")
        named = self.coordinator.create_task(project["id"], "rotate", shape="critical", effort="medium")
        self.assertEqual(self.coordinator._resolve_effort(project, named)[0], "medium")
        standard = self.coordinator.create_task(project["id"], "a feature")
        self.assertEqual(self.coordinator._resolve_effort(project, standard), (None, "", ""))

    def test_the_review_budget_and_reviewer_effort_follow_the_shape(self) -> None:
        project = self._project("reviewed")
        seen: list[dict] = []

        def capture(project_id, brief, **kwargs):
            seen.append(kwargs)
            raise HelmError("stop here; the reviewer task's effort is what this test is about")

        adapter = HerdrAdapter(self.coordinator, FakeHerdr())
        for shape, rounds_expected, effort_expected in (
            ("small", 1, "low"), ("critical", 3, "high"), ("standard", 2, None),
        ):
            task = self.coordinator.create_task(project["id"], "the change", shape=shape)
            self.coordinator.prepare_external_worker(task["id"], [sys.executable, "-c", ""])
            self.commit_on_task_branch(task)
            with mock.patch.object(self.coordinator, "create_task", side_effect=capture), \
                 mock.patch.object(self.coordinator, "pick_reviewer_agent", return_value={
                     "agent": "codex", "command": None, "independence": "different-runtime", "reason": "test",
                 }), \
                 self.assertRaises(HelmError):
                adapter.run_review_cycle(task["id"], timeout=0.01)
            self.assertEqual(seen[-1].get("effort"), effort_expected, shape)
            self.assertEqual(shape_policy(task)["review_rounds"], rounds_expected)

    def test_a_critical_task_is_approved_only_on_recorded_suite_evidence(self) -> None:
        project = self._project("guarded")
        task = self.coordinator.create_task(project["id"], "change the auth flow", shape="critical")
        code = (
            "from pathlib import Path; import subprocess; "
            "Path('change.txt').write_text('worker'); "
            "subprocess.run(['git','add','change.txt'],check=True); "
            "subprocess.run(['git','commit','-qm','worker change'],check=True)"
        )
        self.coordinator.launch_worker(task["id"], [sys.executable, "-c", code])
        with self.assertRaisesRegex(SafetyError, "shaped critical"):
            self.coordinator.approve_task(task["id"], "looks fine")
        tip = subprocess.run(
            ["git", "-C", task["workspace"], "rev-parse", "HEAD"], text=True, capture_output=True, check=True
        ).stdout.strip()
        self.coordinator.record_task_evidence(task["id"], tip=tip[:10], command="make test", exit_code=1)
        with self.assertRaisesRegex(SafetyError, "shaped critical"):
            self.coordinator.approve_task(task["id"], "still red")
        self.coordinator.record_task_evidence(task["id"], tip=tip[:10], command="make test", exit_code=0)
        approved = self.coordinator.approve_task(task["id"], "green on the tip")
        self.assertEqual(approved["status"], "approved")
        # A standard task never needed evidence to be approved.
        plain = self.coordinator.create_task(project["id"], "a feature")
        self.coordinator.launch_worker(plain["id"], [sys.executable, "-c", code])
        self.assertEqual(self.coordinator.approve_task(plain["id"], "ok")["status"], "approved")

    def test_the_cli_takes_a_shape_and_inspect_shows_it(self) -> None:
        project = self._project("clishape")
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(cli.main([
                "--state-dir", str(self.state.directory), "task", "create", "--project", project["id"],
                "--brief", "swap the icon", "--shape", "small", "--shape-reason", "one asset",
            ]), 0)
        task_id = next(iter(self.state.load()["tasks"]))
        with contextlib.redirect_stdout(out):
            self.assertEqual(cli.main(["--state-dir", str(self.state.directory), "inspect", task_id]), 0)
        self.assertIn("shape: small -- one asset", out.getvalue())
