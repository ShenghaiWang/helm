"""The commander can throw away work nobody wants, and only the commander."""

from __future__ import annotations

import contextlib
import io
import os
import subprocess
import sys
from pathlib import Path
from unittest import mock

from helm import archive, cli
from helm.errors import HelmError, SafetyError
from tests.support import HelmTestCase


class DiscardTests(HelmTestCase):
    def _approved_task(self, name: str):
        root, project, task = self._completed_task_awaiting_approval(name)
        self.coordinator.approve_task(task["id"], "reviewed")
        self.assertEqual(self.state.load()["tasks"][task["id"]]["status"], "approved")
        return root, project, task

    def _branches(self, root: Path) -> list[str]:
        return self._run_git(root, "branch", "--format=%(refname:short)").split()

    def _pending_text(self) -> str:
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            cli.main(["--state-dir", str(self.state.directory), "pending"])
        return out.getvalue()

    def test_an_approved_task_with_unpushed_commits_is_discarded_and_archived(self) -> None:
        root, project, task = self._approved_task("unwanted")
        tip = self._run_git(root, "rev-parse", task["branch"])
        # Cleanup will not touch it: it is approved, and its branch is the only copy.
        with self.assertRaisesRegex(SafetyError, "preserve work awaiting approval"):
            self.coordinator.cleanup_task(task["id"], delete_branch=True)
        self.coordinator.record_delivery_decision(project["id"], task_id=task["id"])
        before = self.coordinator.project_status(project["id"])
        self.assertTrue(
            [item for item in before["action_items"] if item.get("task_id") == task["id"]]
        )

        discarded = self.coordinator.discard_task(task["id"], note="superseded by another approach")

        self.assertEqual(discarded["status"], "discarded")
        record = discarded["discard"]
        self.assertEqual(record["from_status"], "approved")
        self.assertEqual(record["branch"], task["branch"])
        self.assertEqual(record["tip"], tip)
        self.assertEqual(record["unpushed_commits"], 1)
        self.assertEqual(record["note"], "superseded by another approach")
        self.assertNotIn(task["branch"], self._branches(root))
        self.assertFalse(Path(task["workspace"]).exists())
        # The recorded tip still recovers the work.
        self._run_git(root, "branch", "recovered", record["tip"])
        self.assertEqual(self._run_git(root, "rev-parse", "recovered"), tip)
        data = self.state.load()
        self.assertEqual(self.coordinator.task_retained_resources(data["tasks"][task["id"]], data), [])
        self.assertTrue(self.coordinator.task_delivery_resolved(data["tasks"][task["id"]]))
        self.assertIn(task["id"], self.coordinator.archivable_task_ids())
        self.assertEqual(self.coordinator.archive_tasks([task["id"]])["archived"], [task["id"]])
        self.assertTrue(archive.task_file(self.state.directory, task["id"]).is_file())
        self.assertEqual(self.coordinator.inspect_task(task["id"])["task"]["status"], "discarded")
        status = self.coordinator.project_status(project["id"])
        self.assertEqual(
            [item for item in status["action_items"] if item.get("task_id") == task["id"]], []
        )
        self.assertNotIn(task["id"], self._pending_text())
        # A discarded task is finished: it cannot be reopened, continued or discarded again.
        with self.assertRaises(HelmError):
            self.coordinator.reopen_task(task["id"], "look again")

    def test_the_cli_requires_confirm_and_prints_the_recovery_sha(self) -> None:
        root, _project, task = self._approved_task("by-hand")
        tip = self._run_git(root, "rev-parse", task["branch"])
        base = ["--state-dir", str(self.state.directory), "task", "discard", task["id"]]
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            cli.main([*base, "--note", "unwanted"])
        self.assertIn(task["branch"], self._branches(root))
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(cli.main([*base, "--confirm", "--note", "unwanted"]), 0)
        text = out.getvalue()
        self.assertIn(tip, text)
        self.assertIn(f"git branch {task['branch']} {tip}", text)
        self.assertIn("record archived", text)
        self.assertNotIn(task["id"], self.state.load()["tasks"])

    def test_delivered_and_pr_open_work_is_refused(self) -> None:
        root, project, task = self._approved_task("landed")
        self.coordinator.merge_task(task["id"])
        with self.assertRaisesRegex(SafetyError, "already delivered"):
            self.coordinator.discard_task(task["id"], note="x")

        pr = self.coordinator.create_task(project["id"], "open a PR", delivery_policy="pr")
        worker = self.coordinator.prepare_external_worker(
            pr["id"], [sys.executable, "-c", ""], execution="external"
        )
        self.commit_on_task_branch(pr, "under review")
        self.coordinator.stop_worker(worker["id"], "stood down")
        with self.coordinator.store.locked() as data:
            data["tasks"][pr["id"]]["status"] = "pr-open"
            data["tasks"][pr["id"]]["delivery"] = {
                "policy": "pr", "state": "pr-open", "url": "https://forge.invalid/pr/7", "events": [],
            }
        with self.assertRaisesRegex(SafetyError, "Close the PR first"):
            self.coordinator.discard_task(pr["id"], note="x")
        self.assertIn(pr["branch"], self._branches(root))

    def test_a_live_worker_is_refused_until_it_is_stopped(self) -> None:
        root = self.repo("live")
        project = self.coordinator.register_project("Live", str(root), project_id="live")
        task = self.coordinator.create_task(project["id"], "still going")
        worker = self.coordinator.prepare_external_worker(
            task["id"], [sys.executable, "-c", ""], execution="external"
        )
        self.commit_on_task_branch(task, "half done")
        with self.assertRaisesRegex(SafetyError, f"helm worker stop {worker['id']}"):
            self.coordinator.discard_task(task["id"], note="x")
        self.assertIn(task["branch"], self._branches(root))
        self.coordinator.stop_worker(worker["id"], "stood down")
        self.assertEqual(
            self.coordinator.discard_task(task["id"], note="not needed")["status"], "discarded"
        )
        self.assertNotIn(task["branch"], self._branches(root))

    def test_an_open_approval_hold_is_withdrawn(self) -> None:
        root = self.repo("held")
        project = self.coordinator.register_project("Held", str(root), project_id="held")
        task = self.coordinator.create_task(project["id"], "publish it", shape="small")
        worker = self.coordinator.prepare_external_worker(
            task["id"], [sys.executable, "-c", ""], execution="external"
        )
        self.commit_on_task_branch(task, "to publish")
        self.coordinator.record_worker_message(
            worker["id"], "approval-needed", "ready", payload={"action": "publish"}
        )
        self.coordinator.stop_worker(worker["id"], "stood down")
        self.assertIsNotNone(self.coordinator.task_hold(self.state.load()["tasks"][task["id"]]))

        discarded = self.coordinator.discard_task(task["id"], note="will not publish")

        self.assertEqual(discarded["status"], "discarded")
        self.assertIsNone(self.coordinator.task_hold(self.state.load()["tasks"][task["id"]]))
        self.assertEqual(
            [e for e in self.coordinator.open_escalations() if e.get("task_id") == task["id"]], []
        )
        self.assertIn(task["id"], self.coordinator.archivable_task_ids())

    def test_a_dirty_worktree_is_refused_without_force_dirty(self) -> None:
        root, _project, task = self._approved_task("dirty")
        (Path(task["workspace"]) / "scratch.txt").write_text("not committed", encoding="utf-8")
        with self.assertRaisesRegex(SafetyError, r"uncommitted changes.*scratch\.txt.*--force-dirty"):
            self.coordinator.discard_task(task["id"], note="x")
        self.assertTrue(Path(task["workspace"]).exists())
        self.assertIn(task["branch"], self._branches(root))
        self.assertEqual(self.state.load()["tasks"][task["id"]]["status"], "approved")

        discarded = self.coordinator.discard_task(task["id"], note="all of it", force_dirty=True)

        self.assertEqual(discarded["status"], "discarded")
        self.assertTrue(any("scratch.txt" in line for line in discarded["discard"]["dirty_discarded"]))
        self.assertFalse(Path(task["workspace"]).exists())

    def test_an_agent_cannot_discard(self) -> None:
        root, project, task = self._approved_task("guarded")
        other = self.coordinator.create_task(project["id"], "another")
        agent = self.coordinator.prepare_external_worker(
            other["id"], [sys.executable, "-c", ""], execution="external"
        )
        with mock.patch.dict(os.environ, {"HELM_WORKER_ID": agent["id"]}):
            with self.assertRaisesRegex(SafetyError, "discarding a task's work is the human's"):
                self.coordinator.discard_task(task["id"], note="x")
            err = io.StringIO()
            with contextlib.redirect_stderr(err), contextlib.redirect_stdout(io.StringIO()):
                code = cli.main([
                    "--state-dir", str(self.state.directory),
                    "task", "discard", task["id"], "--confirm", "--note", "x",
                ])
            self.assertNotEqual(code, 0)
        self.assertIn(task["branch"], self._branches(root))
        self.assertEqual(self.state.load()["tasks"][task["id"]]["status"], "approved")

    def _open_items(self, project_id: str, task_id: str) -> list[dict]:
        status = self.coordinator.project_status(project_id)
        return [item for item in status["action_items"] if item.get("task_id") == task_id]

    def test_a_branch_checked_out_elsewhere_stays_named_until_a_rerun_finishes(self) -> None:
        root, project, task = self._approved_task("checked-out")
        base = task["base_branch"]
        # The commander has the task branch checked out in the project itself.
        self._run_git(root, "checkout", "-q", "--ignore-other-worktrees", task["branch"])

        discarded = self.coordinator.discard_task(task["id"], note="not wanted")

        self.assertEqual(discarded["status"], "discarded")
        self.assertFalse(discarded["branch_removed"])
        self.assertIn("could not be deleted", discarded["discard"]["incomplete"])
        self.assertIn(task["branch"], self._branches(root))
        self.assertFalse(Path(task["workspace"]).exists())
        # It is not silent residue: a decision names the branch and why.
        items = self._open_items(project["id"], task["id"])
        self.assertEqual(len(items), 1)
        self.assertIn("Discard incomplete", items[0]["text"])
        self.assertIn(task["branch"], items[0]["text"])
        self.assertIn("could not be deleted", items[0]["text"])
        self.assertNotIn(task["id"], self.coordinator.archivable_task_ids())
        released = self.coordinator.release_project(project["id"])
        self.assertNotIn(task["id"], released["released"])
        kept = {entry["task_id"]: entry["reason"] for entry in released["kept"]}
        self.assertIn("still holds", kept[task["id"]])
        out = io.StringIO()
        self._run_git(root, "checkout", "-q", base)
        self._run_git(root, "checkout", "-q", "--ignore-other-worktrees", task["branch"])
        with contextlib.redirect_stdout(out):
            cli.main([
                "--state-dir", str(self.state.directory), "task", "discard", task["id"],
                "--confirm", "--note", "again",
            ])
        self.assertIn("NOT finished", out.getvalue())

        # Once the checkout moves away, running it again finishes the job.
        self._run_git(root, "checkout", "-q", base)
        finished = self.coordinator.discard_task(task["id"], note="finish it")

        self.assertTrue(finished["branch_removed"])
        self.assertIsNone(finished["discard"]["incomplete"])
        self.assertEqual(finished["discard"]["note"], "not wanted")
        self.assertEqual(len(finished["discard"]["finishes"]), 2)
        self.assertNotIn(task["branch"], self._branches(root))
        self.assertEqual(self._open_items(project["id"], task["id"]), [])
        self.assertIn(task["id"], self.coordinator.archivable_task_ids())
        with self.assertRaisesRegex(HelmError, "already discarded and holds nothing"):
            self.coordinator.discard_task(task["id"], note="once more")

    def test_cleanup_also_finishes_a_discard_whose_branch_was_kept(self) -> None:
        root, _project, task = self._approved_task("cleanup-finishes")
        self._run_git(root, "checkout", "-q", "--ignore-other-worktrees", task["branch"])
        self.coordinator.discard_task(task["id"], note="not wanted")
        self._run_git(root, "checkout", "-q", task["base_branch"])
        # The branch is the only copy of its commit, which cleanup would
        # normally keep; the recorded discard is the commander's decision.
        cleaned = self.coordinator.cleanup_task(task["id"])
        self.assertTrue(cleaned["branch_removed"])
        self.assertNotIn(task["branch"], self._branches(root))

    def test_a_late_report_does_not_reopen_a_discarded_task(self) -> None:
        root = self.repo("late")
        project = self.coordinator.register_project("Late", str(root), project_id="late")
        task = self.coordinator.create_task(project["id"], "goes quiet")
        # Settled by observing its exit, so a late terminal word is admitted.
        worker = self.coordinator.launch_worker(task["id"], [sys.executable, "-c", ""])
        self.coordinator.discard_task(task["id"], note="unwanted")
        before = len(self.state.load()["messages"])
        self.coordinator.record_worker_message(worker["id"], "failure", "late failure")
        data = self.state.load()
        self.assertGreater(len(data["messages"]), before)
        self.assertEqual(data["tasks"][task["id"]]["status"], "discarded")
        for kind, status in (("blocker", None), ("status", "failed"), ("result", None)):
            with contextlib.suppress(HelmError, SafetyError):
                self.coordinator.record_worker_message(worker["id"], kind, "late word", requested_status=status)
            self.assertEqual(self.state.load()["tasks"][task["id"]]["status"], "discarded", kind)
        # And directly, whatever the late-delivery policy lets through.
        record = {"status": "discarded"}
        for kind, status in (("failure", None), ("blocker", None), ("status", "blocked"), ("status", "failed")):
            self.coordinator._transition_from_message(record, kind, status)
            self.assertEqual(record["status"], "discarded")
        with self.assertRaisesRegex(SafetyError, "discarded"):
            self.coordinator.record_worker_message(
                worker["id"], "approval-needed", "may I?", payload={"action": "publish"}
            )

    def test_a_failed_worktree_removal_still_lands_the_discard_and_a_rerun_finishes(self) -> None:
        root = self.repo("stuck")
        project = self.coordinator.register_project("Stuck", str(root), project_id="stuck")
        task = self.coordinator.create_task(project["id"], "publish it", shape="small")
        worker = self.coordinator.prepare_external_worker(
            task["id"], [sys.executable, "-c", ""], execution="external"
        )
        self.commit_on_task_branch(task, "to publish")
        self.coordinator.record_worker_message(
            worker["id"], "approval-needed", "ready", payload={"action": "publish"}
        )
        self.coordinator.stop_worker(worker["id"], "stood down")
        from helm.coordinator import protection

        real = protection._git

        def failing(cwd, *args, check=True):
            if args[:2] == ("worktree", "remove"):
                raise HelmError("simulated: worktree could not be removed")
            return real(cwd, *args, check=check)

        with mock.patch.object(protection, "_git", failing):
            discarded = self.coordinator.discard_task(task["id"], note="will not publish")

        self.assertEqual(discarded["status"], "discarded")
        self.assertIn("simulated", discarded["discard"]["incomplete"])
        self.assertFalse(discarded.get("workspace_removed"))
        self.assertIsNone(self.coordinator.task_hold(self.state.load()["tasks"][task["id"]]))
        items = self._open_items(project["id"], task["id"])
        self.assertTrue(any("Discard incomplete" in item["text"] for item in items))
        # Leave it half-removed, the way a failed removal can: git no longer
        # calls it a worktree, so verification alone would refuse it forever.
        (Path(task["workspace"]) / ".git").unlink()

        finished = self.coordinator.discard_task(task["id"], note="finish it")

        self.assertIsNone(finished["discard"]["incomplete"])
        self.assertTrue(finished["workspace_removed"])
        self.assertTrue(finished["branch_removed"])
        self.assertFalse(Path(task["workspace"]).exists())
        self.assertNotIn(task["branch"], self._branches(root))
        self.assertEqual(
            [i for i in self._open_items(project["id"], task["id"]) if "Discard" in i["text"]], []
        )
        self.assertIn(task["id"], self.coordinator.archivable_task_ids())

    def test_a_task_that_never_ran_can_be_discarded(self) -> None:
        root = self.repo("never")
        project = self.coordinator.register_project("Never", str(root), project_id="never")
        task = self.coordinator.create_task(project["id"], "never launched")
        discarded = self.coordinator.discard_task(task["id"], note="not needed after all")
        self.assertEqual(discarded["status"], "discarded")
        self.assertNotIn(task["branch"], self._branches(root))
        self.assertIn(task["id"], self.coordinator.archivable_task_ids())

    def test_a_note_is_required(self) -> None:
        _root, _project, task = self._approved_task("silent")
        with self.assertRaisesRegex(HelmError, "--note"):
            self.coordinator.discard_task(task["id"], note="  ")


if __name__ == "__main__":  # pragma: no cover
    import unittest

    unittest.main()
