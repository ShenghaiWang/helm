"""A standing cleanup grant sheds delivered and stale residue on its own."""

from __future__ import annotations

import contextlib
import io
import subprocess
import sys
import time

from helm import cli
from helm.errors import HelmError
from tests.support import HelmTestCase


class CleanupGrantTests(HelmTestCase):
    def _merged_task(self, project, name: str):
        task = self.coordinator.create_task(project["id"], f"change {name}")
        code = (
            "from pathlib import Path; import subprocess; "
            f"Path('{name}.txt').write_text('w'); subprocess.run(['git','add','{name}.txt'],check=True); "
            "subprocess.run(['git','commit','-qm','w'],check=True)"
        )
        self.coordinator.launch_worker(task["id"], [sys.executable, "-c", code])
        self.coordinator.approve_task(task["id"], "reviewed")
        return self.coordinator.merge_task(task["id"])

    def test_nothing_is_shed_without_a_grant_and_delivered_residue_goes_under_one(self) -> None:
        root = self.repo("granted")
        project = self.coordinator.register_project("Granted", str(root), project_id="granted")
        merged = self._merged_task(project, "one")
        self.assertEqual(merged["status"], "merged")
        self.assertTrue(self.coordinator.task_retained_resources(merged, self.state.load()))
        swept = self.coordinator.sweep_residue_under_grants()
        self.assertEqual(swept["cleaned"], [])
        self.assertEqual(swept["without_grant"], 1)
        with self.assertRaisesRegex(HelmError, "protected action must be one of"):
            self.coordinator.grant_approval("tidy", note="x")
        with self.assertRaisesRegex(HelmError, "cleanup grant only"):
            self.coordinator.grant_approval("merge", note="x", stale_days=3)
        grant = self.coordinator.grant_approval("cleanup", project_id=project["id"], note="delivered work holds nothing")

        swept = self.coordinator.sweep_residue_under_grants()

        self.assertEqual([entry["task_id"] for entry in swept["cleaned"]], [merged["id"]])
        self.assertEqual(swept["cleaned"][0]["grant_id"], grant["id"])
        after = self.state.load()["tasks"][merged["id"]]
        self.assertTrue(after["workspace_removed"])
        self.assertTrue(after["branch_removed"])
        self.assertEqual(after["cleaned_under_grant"], grant["id"])
        self.assertEqual(self.coordinator.task_retained_resources(after, self.state.load()), [])
        # Another project's residue is outside this grant's scope.
        other_root = self.repo("elsewhere")
        other = self.coordinator.register_project("Elsewhere", str(other_root), project_id="elsewhere")
        other_merged = self._merged_task(other, "two")
        swept = self.coordinator.sweep_residue_under_grants()
        self.assertEqual(swept["cleaned"], [])
        self.assertEqual(swept["without_grant"], 1)
        self.assertTrue(self.coordinator.task_retained_resources(
            self.state.load()["tasks"][other_merged["id"]], self.state.load()))

    def test_stale_days_sheds_failed_work_and_keeps_an_undelivered_branch(self) -> None:
        root = self.repo("stale")
        project = self.coordinator.register_project("Stale", str(root), project_id="stale")
        failed = self.coordinator.create_task(project["id"], "will fail")
        self.coordinator.launch_worker(failed["id"], [sys.executable, "-c", "raise SystemExit(3)"])
        done = self.coordinator.create_task(project["id"], "done but never merged")
        code = (
            "from pathlib import Path; import subprocess; Path('d.txt').write_text('w'); "
            "subprocess.run(['git','add','d.txt'],check=True); subprocess.run(['git','commit','-qm','d'],check=True)"
        )
        self.coordinator.launch_worker(done["id"], [sys.executable, "-c", code])
        self.assertEqual(self.state.load()["tasks"][failed["id"]]["status"], "failed")
        self.coordinator.grant_approval("cleanup", note="sweep old residue", stale_days=7)
        # Nothing is old enough yet.
        self.assertEqual(self.coordinator.sweep_residue_under_grants()["cleaned"], [])
        later = time.time() + 8 * 86400

        swept = self.coordinator.sweep_residue_under_grants(now_epoch=later)

        by_task = {entry["task_id"]: entry["reason"] for entry in swept["cleaned"]}
        self.assertIn("failed for 8 days", by_task[failed["id"]])
        self.assertIn("completed but undelivered", by_task[done["id"]])
        data = self.state.load()
        self.assertTrue(data["tasks"][failed["id"]]["branch_removed"])
        # Finished work nobody decided on keeps its branch, and the record says so.
        kept = data["tasks"][done["id"]]
        self.assertTrue(kept["workspace_removed"])
        self.assertFalse(kept["branch_removed"])
        self.assertIn("its task branch", " ".join(self.coordinator.task_retained_resources(kept, data)))

    def test_watch_runs_the_sweep_and_archives_what_it_shed(self) -> None:
        root = self.repo("watched")
        project = self.coordinator.register_project("Watched", str(root), project_id="watched")
        merged = self._merged_task(project, "three")
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(cli.main([
                "--state-dir", str(self.state.directory), "approval", "grant", "cleanup",
                "--stale-days", "30", "--note", "delivered work holds nothing",
            ]), 0)
            cli.main(["--state-dir", str(self.state.directory), "watch"])
            cli.main(["--state-dir", str(self.state.directory), "approval", "list"])
        text = out.getvalue()
        self.assertIn("Cleanup under standing grant: 1 task(s)", text)
        self.assertIn(f"{merged['id']}: delivered (merged)", text)
        self.assertIn("stale after 30 days", text)
        self.assertNotIn(merged["id"], self.state.load()["tasks"])
        self.assertIsNotNone(self.coordinator.archived_task(merged["id"]))
