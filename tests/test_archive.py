"""Settled records leave the live document, and can still be read."""

from __future__ import annotations

import contextlib
import io
import json
import os
import shutil
import sys
from pathlib import Path

from helm import archive, cli
from helm.core import Coordinator
from helm.errors import HelmError, SafetyError
from helm.state import StateStore
from tests.support import HelmTestCase


class ArchiveTests(HelmTestCase):
    def _finished_task(self, name: str, *, cleanup: bool = True, coordinator=None):
        coordinator = coordinator or self.coordinator
        if coordinator is self.coordinator:
            root = self.repo(name)
            project = coordinator.register_project(name.title(), str(root), project_id=name)
        else:
            project = coordinator.get_project(name)
        task = coordinator.create_task(project["id"], "do the thing")
        worker = coordinator.launch_worker(task["id"], [sys.executable, "-c", ""])
        self.assertEqual(worker["status"], "completed")
        if cleanup:
            coordinator.cleanup_task(task["id"], delete_branch=True)
        return project, task, worker

    def test_a_cleaned_up_task_is_eligible_and_an_uncleaned_one_is_not(self) -> None:
        _, cleaned, _ = self._finished_task("tidy")
        _, live, _ = self._finished_task("busy", cleanup=False)
        eligible = self.coordinator.archivable_task_ids()
        self.assertIn(cleaned["id"], eligible)
        self.assertNotIn(live["id"], eligible)
        stats = self.coordinator.state_stats()
        self.assertEqual(stats["archivable"], 1)
        self.assertEqual(stats["archive_files"], 0)
        self.assertEqual(stats["tasks"], 2)

    def test_archiving_moves_the_task_with_its_workers_and_messages_and_keeps_it_readable(self) -> None:
        project, task, worker = self._finished_task("moved")
        before = self.state.load()
        self.assertIn(task["id"], before["tasks"])
        message_count = sum(1 for m in before["messages"] if m.get("task_id") == task["id"])
        other_messages = len(before["messages"]) - message_count

        result = self.coordinator.archive_tasks()

        self.assertEqual(result["archived"], [task["id"]])
        after = self.state.load()
        self.assertNotIn(task["id"], after["tasks"])
        self.assertNotIn(worker["id"], after["workers"])
        self.assertEqual(len(after["messages"]), other_messages)
        path = archive.task_file(self.state.directory, task["id"])
        self.assertTrue(path.is_file())
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        record = json.loads(path.read_text())
        self.assertEqual(record["task"]["id"], task["id"])
        self.assertEqual(len(record["messages"]), message_count)
        self.assertIn(worker["id"], record["workers"])
        # Still readable where history is read.
        inspected = self.coordinator.inspect_task(task["id"])
        self.assertEqual(inspected["task"]["id"], task["id"])
        self.assertEqual(len(inspected["messages"]), message_count)
        self.assertIsNotNone(inspected["archived_at"])
        usage = self.coordinator.task_usage(task["id"])
        self.assertTrue(usage["archived"])
        self.assertEqual([w["worker_id"] for w in usage["workers"]], [worker["id"]])
        outcome = self.coordinator.task_outcome(task["id"])
        self.assertFalse(outcome["workspace_exists"])
        evidence = self.coordinator.reflection_evidence(since_hours=1)
        self.assertGreaterEqual(evidence["tasks_created"], 1)
        # Archiving again moves nothing, and an unknown id is still unknown.
        self.assertEqual(self.coordinator.archive_tasks()["archived"], [])
        with self.assertRaisesRegex(HelmError, "unknown task"):
            self.coordinator.inspect_task("t-000000000000")

    def test_a_reviewer_leaves_only_with_the_task_it_reviewed(self) -> None:
        project, task, worker = self._finished_task("reviewed", cleanup=False)
        review = self.coordinator.create_task(
            project["id"], "review it", role="reviewer", reviews=task["id"], read_only=True
        )
        reviewer = self.coordinator.launch_worker(review["id"], [sys.executable, "-c", ""])
        self.coordinator.cleanup_task(review["id"])
        # The reviewed task is not cleaned up, so the reviewer stays with it.
        self.assertEqual(self.coordinator.archivable_task_ids(), [])
        self.coordinator.cleanup_task(task["id"], delete_branch=True)
        self.assertEqual(sorted(self.coordinator.archivable_task_ids()), sorted([task["id"], review["id"]]))
        self.coordinator.archive_tasks()
        record = self.coordinator.archived_task(task["id"])
        self.assertEqual(record["reviewer_task_ids"], [review["id"]])
        usage = self.coordinator.task_usage(task["id"])
        self.assertEqual(
            sorted(w["worker_id"] for w in usage["workers"]), sorted([worker["id"], reviewer["id"]])
        )

    def test_the_cli_archives_after_cleanup_and_reports_stats(self) -> None:
        project, task, worker = self._finished_task("clied", cleanup=False)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(cli.main([
                "--state-dir", str(self.state.directory), "task", "cleanup", task["id"], "--delete-branch",
            ]), 0)
            self.assertEqual(cli.main(["--state-dir", str(self.state.directory), "state", "stats"]), 0)
            self.assertEqual(cli.main(["--state-dir", str(self.state.directory), "state", "archive", "--dry-run"]), 0)
        text = out.getvalue()
        self.assertIn("record archived", text)
        self.assertNotIn(task["id"], self.state.load()["tasks"])
        self.assertIn("archive: 1 file(s)", text)
        self.assertIn("would archive 0", text)

    def test_a_project_is_removed_only_when_nothing_of_it_can_change(self) -> None:
        helm_root = self._helm_root("removal-root")
        name = "gone"
        shutil.move(str(self.repo(name)), str(helm_root / "projects" / name))
        coordinator = Coordinator(StateStore(helm_root / "state", helm_root=helm_root))
        project = coordinator.discover_project(helm_root, name)
        _, task, _ = self._finished_task(name, cleanup=False, coordinator=coordinator)
        with self.assertRaisesRegex(SafetyError, "still exists"):
            coordinator.remove_project(name)
        # Its directory leaves projects/, but its task is still uncleaned.
        root = helm_root / "projects" / name
        moved = helm_root / f"{name}-moved"
        os.rename(root, moved)
        with self.assertRaisesRegex(SafetyError, "cannot be archived"):
            coordinator.remove_project(name)
        os.rename(moved, root)
        coordinator.cleanup_task(task["id"], delete_branch=True)
        os.rename(root, moved)

        removed = coordinator.remove_project(name)

        self.assertEqual(removed["archived_tasks"], [task["id"]])
        data = coordinator.store.load()
        self.assertNotIn(name, data["projects"])
        self.assertFalse(any(m.get("project_id") == name for m in data["messages"]))
        record = json.loads(archive.project_file(coordinator.store.directory, name).read_text())
        self.assertEqual(record["project"]["id"], name)
        self.assertIsNotNone(coordinator.inspect_task(task["id"])["archived_at"])
        with self.assertRaisesRegex(HelmError, "unknown project"):
            coordinator.remove_project(name)

    def test_a_project_whose_repository_is_gone_is_removed_with_its_records_reconciled(self) -> None:
        helm_root = self._helm_root("vanished-root")
        name = "vanished"
        shutil.move(str(self.repo(name)), str(helm_root / "projects" / name))
        coordinator = Coordinator(StateStore(helm_root / "state", helm_root=helm_root))
        coordinator.discover_project(helm_root, name)
        _, task, _ = self._finished_task(name, cleanup=False, coordinator=coordinator)
        # The whole repository disappears, task worktree included; the record
        # still claims a worktree and a branch it can no longer shed.
        shutil.rmtree(helm_root / "projects" / name)
        shutil.rmtree(Path(task["workspace"]), ignore_errors=True)

        removed = coordinator.remove_project(name)

        self.assertEqual(removed["archived_tasks"], [task["id"]])
        record = coordinator.archived_task(task["id"])
        self.assertTrue(record["task"]["workspace_removed"])
        self.assertTrue(record["task"]["branch_removed"])
        self.assertIn("root missing", record["task"]["reconciled_by"])
        self.assertNotIn(name, coordinator.store.load()["projects"])
