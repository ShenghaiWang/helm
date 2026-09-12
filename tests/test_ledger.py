"""The ledger lays out what each task cost and what came of it."""

from __future__ import annotations

import contextlib
import io
import json
import sys

from helm import cli
from tests.support import HelmTestCase


class LedgerTests(HelmTestCase):
    def test_live_and_archived_tasks_appear_with_their_reviews_asks_and_totals(self) -> None:
        root = self.repo("ledgered")
        project = self.coordinator.register_project("Ledgered", str(root), project_id="ledgered")
        first = self.coordinator.create_task(project["id"], "first change", shape="small", ticket="T-1")
        worker = self.coordinator.launch_worker(first["id"], [sys.executable, "-c", ""])
        review = self.coordinator.create_task(
            project["id"], "review", role="reviewer", reviews=first["id"], read_only=True
        )
        reviewer = self.coordinator.launch_worker(review["id"], [sys.executable, "-c", ""])
        self.coordinator.record_worker_message(reviewer["id"], "result", "CHANGES-REQUESTED: missing test")
        self.coordinator.cleanup_task(review["id"])
        self.coordinator.cleanup_task(first["id"], delete_branch=True)
        self.assertEqual(self.coordinator.archive_tasks()["archived"], sorted([first["id"], review["id"]]))
        second = self.coordinator.create_task(project["id"], "second change")
        self.coordinator.launch_worker(second["id"], [sys.executable, "-c", ""])
        investigation = self.coordinator.create_task(project["id"], "look only", read_only=True)

        report = self.coordinator.ledger(days=1)

        ids = [row["task_id"] for row in report["rows"]]
        self.assertEqual(ids, [first["id"], second["id"]])
        self.assertNotIn(investigation["id"], ids)
        archived_row = report["rows"][0]
        self.assertTrue(archived_row["archived"])
        self.assertEqual(archived_row["shape"], "small")
        self.assertEqual(archived_row["ticket"], "T-1")
        self.assertEqual(archived_row["review_rounds"], 1)
        self.assertEqual(archived_row["review_catches"], 1)
        self.assertIsNotNone(archived_row["minutes_to_result"])
        self.assertEqual(report["totals"]["tasks"], 2)
        self.assertEqual(report["totals"]["review_catches"], 1)
        self.assertEqual(self.coordinator.ledger(days=1, project_id="other")["rows"], [])

        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(cli.main(["--state-dir", str(self.state.directory), "ledger", "--days", "1"]), 0)
            self.assertEqual(cli.main(["--state-dir", str(self.state.directory), "ledger", "--json"]), 0)
        text = out.getvalue()
        self.assertIn("2 worker task(s)", text)
        self.assertIn(f"| {first['id']} | ledgered | T-1 | small |", text)
        self.assertIn("totals:", text)
        self.assertEqual(json.loads(text[text.index("{"):])["totals"]["tasks"], 2)

    def test_a_merged_task_reads_as_delivered_even_from_a_record_that_never_advanced(self) -> None:
        root = self.repo("delivered")
        project = self.coordinator.register_project("Delivered", str(root), project_id="delivered")
        task = self.coordinator.create_task(project["id"], "ship it")
        code = (
            "from pathlib import Path; import subprocess; "
            "Path('shipped.txt').write_text('w'); subprocess.run(['git','add','shipped.txt'],check=True); "
            "subprocess.run(['git','commit','-qm','w'],check=True)"
        )
        self.coordinator.launch_worker(task["id"], [sys.executable, "-c", code])
        self.coordinator.approve_task(task["id"], "reviewed")
        merged = self.coordinator.merge_task(task["id"])
        self.assertEqual(merged["delivery"]["state"], "merged")
        report = self.coordinator.ledger(days=1, project_id="delivered")
        self.assertEqual(report["rows"][0]["delivery"], "merged")
        self.assertEqual(report["totals"]["delivered"], 1)
        # A record from before the merge path advanced its delivery state.
        with self.coordinator.store.locked() as data:
            data["tasks"][task["id"]]["delivery"]["state"] = "worktree"
        report = self.coordinator.ledger(days=1, project_id="delivered")
        self.assertEqual(report["rows"][0]["delivery"], "merged")
        self.assertEqual(report["totals"]["delivered"], 1)
