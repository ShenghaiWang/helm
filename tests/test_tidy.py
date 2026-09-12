"""Decisions nothing can act on close; the ones a human can still act on stay."""

from __future__ import annotations

import contextlib
import datetime as _dt
import io
import os
import sys
from unittest import mock

from helm import cli
from helm.coordinator.tidy import ARCHIVED_REASON, STALE_FOLLOW_UP_DAYS
from helm.errors import HelmError, SafetyError
from helm.values import FAILURE_ACTION_KIND, FOLLOW_UP_ACTION_KIND
from tests.support import HelmTestCase


class TidyDecisionsTests(HelmTestCase):
    def _open_items(self, project_id: str) -> list[dict]:
        status = self.coordinator._load_status(project_id)
        return [i for i in status["action_items"] if i.get("status", "open") == "open"]

    def _finished_task(self, project: dict, brief: str) -> dict:
        task = self.coordinator.create_task(project["id"], brief)
        worker = self.coordinator.launch_worker(task["id"], [sys.executable, "-c", ""])
        self.assertEqual(worker["status"], "completed")
        return task

    def test_an_item_about_an_archived_task_closes_and_one_about_live_work_stays(self) -> None:
        root = self.repo("hygiene")
        project = self.coordinator.register_project("Hygiene", str(root), project_id="hygiene")
        gone = self._finished_task(project, "the settled one")
        live = self._finished_task(project, "the live one")
        for task in (gone, live):
            self.coordinator.record_project_action_item(
                project["id"], f"check the caveat on {task['id']}", task_id=task["id"]
            )
        self.coordinator.record_project_action_item(project["id"], "a note about nothing in particular")
        self.coordinator.cleanup_task(gone["id"], delete_branch=True)
        self.assertEqual(self.coordinator.archive_tasks([gone["id"]])["archived"], [gone["id"]])

        preview = self.coordinator.tidy_decisions(dry_run=True)
        self.assertTrue(preview["dry_run"])
        self.assertEqual([e["task_id"] for e in preview["resolved"]], [gone["id"]])
        # A dry run writes nothing: every item is still open.
        self.assertEqual(len(self._open_items(project["id"])), 3)

        tidied = self.coordinator.tidy_decisions()
        self.assertEqual([e["task_id"] for e in tidied["resolved"]], [gone["id"]])
        self.assertEqual(tidied["resolved"][0]["reason"], ARCHIVED_REASON)
        self.assertEqual(tidied["resolved"][0]["kind"], FOLLOW_UP_ACTION_KIND)
        self.assertEqual(tidied["kept"], 2)
        remaining = self._open_items(project["id"])
        self.assertEqual({i.get("task_id") for i in remaining}, {live["id"], None})
        closed = [
            i for i in self.coordinator._load_status(project["id"])["action_items"]
            if i.get("status") == "resolved"
        ]
        self.assertEqual(closed[0]["resolved_reason"], ARCHIVED_REASON)
        # Running it again finds nothing more to do.
        self.assertEqual(self.coordinator.tidy_decisions()["resolved"], [])

    def test_the_pass_is_scoped_to_one_project_when_asked(self) -> None:
        projects = []
        for name in ("alpha", "beta"):
            root = self.repo(name)
            project = self.coordinator.register_project(name.title(), str(root), project_id=name)
            task = self._finished_task(project, "done")
            self.coordinator.record_project_action_item(project["id"], "caveat", task_id=task["id"])
            self.coordinator.cleanup_task(task["id"], delete_branch=True)
            self.coordinator.archive_tasks([task["id"]])
            projects.append(project)
        tidied = self.coordinator.tidy_decisions("alpha")
        self.assertEqual({e["project_id"] for e in tidied["resolved"]}, {"alpha"})
        self.assertEqual(len(self._open_items("beta")), 1)
        with self.assertRaisesRegex(HelmError, "unknown project"):
            self.coordinator.tidy_decisions("gamma")

    def test_a_failed_foreman_or_reviewer_raises_no_failure_decision(self) -> None:
        """Retry, continue and cleanup are about a worktree; those roles own none."""
        root = self.repo("roles")
        project = self.coordinator.register_project("Roles", str(root), project_id="roles")
        task = self.coordinator.create_task(project["id"], "review it")
        self.coordinator.launch_worker(task["id"], [sys.executable, "-c", ""])
        for role in ("reviewer", "foreman"):
            with self.coordinator.store.locked() as data:
                data["tasks"][task["id"]]["status"] = "failed"
                data["tasks"][task["id"]]["role"] = role
            refreshed = self.coordinator.refresh_failure_decisions(project["id"])
            self.assertEqual(refreshed["raised"], [], role)
            self.assertEqual(
                [i for i in self._open_items(project["id"]) if i["kind"] == FAILURE_ACTION_KIND], []
            )
        with self.coordinator.store.locked() as data:
            data["tasks"][task["id"]]["role"] = "worker"
        refreshed = self.coordinator.refresh_failure_decisions(project["id"])
        self.assertEqual(len(refreshed["raised"]), 1)
        # And a worker failure that later becomes a reviewer's -- the role is
        # not rewritten in practice, but the gate closes for the same reason.
        with self.coordinator.store.locked() as data:
            data["tasks"][task["id"]]["role"] = "reviewer"
        refreshed = self.coordinator.refresh_failure_decisions(project["id"])
        self.assertEqual(len(refreshed["resolved"]), 1)

    def test_the_cli_reports_what_it_closed_and_what_it_would(self) -> None:
        root = self.repo("clitidy")
        project = self.coordinator.register_project("CliTidy", str(root), project_id="clitidy")
        task = self._finished_task(project, "done")
        self.coordinator.record_project_action_item(project["id"], "caveat", task_id=task["id"])
        self.coordinator.cleanup_task(task["id"], delete_branch=True)
        self.coordinator.archive_tasks([task["id"]])

        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = cli.main(["--state-dir", str(self.state.directory), "state", "tidy", "--dry-run"])
        self.assertEqual(code, 0)
        self.assertIn("would close 1 decision(s); 0 still open", out.getvalue())
        self.assertIn("clitidy: 1 follow-up", out.getvalue())
        self.assertEqual(len(self._open_items(project["id"])), 1)

        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = cli.main(["--state-dir", str(self.state.directory), "state", "tidy", "--project", "clitidy"])
        self.assertEqual(code, 0)
        self.assertIn("closed 1 decision(s); 0 still open", out.getvalue())
        self.assertEqual(self._open_items(project["id"]), [])

    def test_a_stale_follow_up_is_shown_to_the_commander_and_closed_on_their_word(self) -> None:
        """Helm never decides a caveat was dealt with; it makes sure somebody looks."""
        root = self.repo("stale")
        project = self.coordinator.register_project("Stale", str(root), project_id="stale")
        task = self._finished_task(project, "done")
        fresh = self.coordinator.record_project_action_item(project["id"], "a fresh caveat", task_id=task["id"])
        old = self.coordinator.record_project_action_item(project["id"], "an old caveat nobody looked at")
        long_ago = (_dt.datetime.now(_dt.timezone.utc) - _dt.timedelta(days=STALE_FOLLOW_UP_DAYS + 5)).strftime("%Y-%m-%dT%H:%M:%SZ")
        with self.coordinator._status_transaction(project["id"]) as status:
            for item in status["action_items"]:
                if item["id"] == old["id"]:
                    item["at"] = long_ago

        tidied = self.coordinator.tidy_decisions(project["id"])
        self.assertEqual(tidied["resolved"], [])
        self.assertEqual([e["id"] for e in tidied["for_your_eye"]], [old["id"]])
        self.assertGreaterEqual(tidied["for_your_eye"][0]["age_days"], STALE_FOLLOW_UP_DAYS)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(cli.main(["--state-dir", str(self.state.directory), "state", "tidy"]), 0)
        self.assertIn("For your eye, commander: 1 follow-up(s)", out.getvalue())
        self.assertIn(old["id"], out.getvalue())
        self.assertNotIn(fresh["id"], out.getvalue())

        # An agent cannot close it; the commander can, once.
        with mock.patch.dict(os.environ, {"HELM_WORKER_ID": "w-someworker"}):
            with self.assertRaises(SafetyError):
                self.coordinator.resolve_action_item(project["id"], old["id"], note="not mine to close")
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = cli.main([
                "--state-dir", str(self.state.directory), "project", "resolve", project["id"], old["id"],
                "--note", "looked, nothing left to do",
            ])
        self.assertEqual(code, 0)
        self.assertIn(f"Closed {old['id']}", out.getvalue())
        closed = next(
            i for i in self.coordinator._load_status(project["id"])["action_items"] if i["id"] == old["id"]
        )
        self.assertEqual(closed["status"], "resolved")
        self.assertEqual(closed["resolved_reason"], "looked, nothing left to do")
        self.assertEqual(closed["resolved_by"], "commander")
        with self.assertRaisesRegex(HelmError, "already resolved"):
            self.coordinator.resolve_action_item(project["id"], old["id"])
        with self.assertRaisesRegex(HelmError, "unknown action item"):
            self.coordinator.resolve_action_item(project["id"], "i-000000000000")
        self.assertEqual(self.coordinator.tidy_decisions(project["id"])["for_your_eye"], [])
