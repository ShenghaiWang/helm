"""Merge, PR delivery, decisions, finalization and cleanup."""

from __future__ import annotations

import contextlib
import io
import json
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path
from unittest import mock

from helm import cli
from helm.core import (
    DELIVERY_DECISION_KIND,
    FINALIZATION_ACTION_KIND,
    task_owns_branch,
    FOLLOW_UP_ACTION_KIND,
    Coordinator,
    HelmError,
    SafetyError,
    inside,
)
from helm.herdr import HerdrAdapter

from tests.support import FakeHerdr, HelmTestCase, REPO_ROOT, SHIPPED_DOMAINS


class DeliveryTests(HelmTestCase):
    def _settled_task(self, name: str) -> tuple[Path, dict]:
        root = self.repo(name)
        project = self.coordinator.register_project(name, str(root), project_id=name)
        task = self.coordinator.create_task(project["id"], "write something")
        worker = self.coordinator.launch_worker(
            task["id"], [sys.executable, "-c", "import time; time.sleep(2)"], wait=False
        )
        self.coordinator.record_worker_message(worker["id"], "result", "done")
        # Cleanup removes the directory the session sits in, so this waits for
        # the session, which is not what settling the worker means.
        self.await_session_exit(worker)
        return root, task

    def _branches(self, root: Path) -> list[str]:
        listed = subprocess.run(
            ["git", "-C", str(root), "branch", "--format=%(refname:short)"],
            check=True, text=True, stdout=subprocess.PIPE,
        )
        return listed.stdout.split()

    def _decisions(self, project_id: str) -> list[dict]:
        return [
            item
            for item in self.coordinator.project_status(project_id)["action_items"]
            if item["kind"] == DELIVERY_DECISION_KIND
        ]

    def _finalizations(self, project_id: str) -> list[dict]:
        return [
            item
            for item in self.coordinator.project_status(project_id)["action_items"]
            if item["kind"] == FINALIZATION_ACTION_KIND
        ]

    def test_delivery_policy_selection_is_persisted(self) -> None:
        root = self.repo("policy")
        project = self.coordinator.register_project("Policy", str(root), project_id="policy", delivery_policy="pr")
        inherited = self.coordinator.create_task(project["id"], "pr task")
        local = self.coordinator.create_task(project["id"], "local exception", delivery_policy="local")
        self.assertEqual(inherited["delivery_policy"], "pr")
        self.assertEqual(local["delivery_policy"], "local")
        self.assertEqual(self.coordinator.store.load()["projects"]["policy"]["delivery_policy"], "pr")

    def test_dirty_cleanup_is_refused(self) -> None:
        root = self.repo("cleanup")
        project = self.coordinator.register_project("Cleanup", str(root), project_id="cleanup")
        task = self.coordinator.create_task(project["id"], "make a cleanable change")
        command = [sys.executable, "-c", "from pathlib import Path; Path('dirty.txt').write_text('uncommitted')"]
        worker = self.coordinator.launch_worker(task["id"], command)
        self.assertEqual(worker["status"], "completed")
        with self.assertRaises(SafetyError):
            self.coordinator.cleanup_task(task["id"])
        workspace = Path(self.coordinator.inspect_task(task["id"])["task"]["workspace"])
        (workspace / "dirty.txt").unlink()
        cleaned = self.coordinator.cleanup_task(task["id"])
        self.assertTrue(cleaned["workspace_removed"])
        self.assertFalse(workspace.exists())

    def test_build_outputs_reach_the_project_instead_of_dying_in_the_worktree(self) -> None:
        root = self.repo("deliver")
        (root / ".helm").mkdir()
        (root / ".helm" / "project.json").write_text(json.dumps({"deliver": ["renders"]}))
        (root / ".gitignore").write_text("renders/\n")
        subprocess.run(["git", "-C", str(root), "add", "-A"], check=True)
        subprocess.run(["git", "-C", str(root), "commit", "-qm", "settings"], check=True)
        project = self.coordinator.register_project("Deliver", str(root), project_id="deliver")
        task = self.coordinator.create_task(project["id"], "render something")
        code = (
            "from pathlib import Path; "
            "Path('renders').mkdir(exist_ok=True); "
            "Path('renders/video.mp4').write_text('rendered'); "
            "Path('notes.txt').write_text('reported artifact')"
        )
        worker = self.coordinator.launch_worker(task["id"], [sys.executable, "-c", code])
        self.coordinator.record_worker_message(
            worker["id"], "artifact", "notes", payload={"path": "notes.txt"}
        ) if worker["status"] == "running" else None

        delivered = {
            entry["path"]: entry["status"]
            for entry in self.coordinator.deliver_task_artifacts(task["id"])
        }
        # A gitignored render can never arrive via a merge, so delivery is the
        # only way the actual product reaches the project.
        self.assertEqual(delivered.get("renders/video.mp4"), "delivered")
        self.assertEqual((root / "renders" / "video.mp4").read_text(), "rendered")

        # Delivering twice is a no-op, not a duplicate or a clobber.
        again = {
            entry["path"]: entry["status"]
            for entry in self.coordinator.deliver_task_artifacts(task["id"])
        }
        self.assertEqual(again.get("renders/video.mp4"), "identical")

        # A different existing file is never silently replaced.
        (root / "renders" / "video.mp4").write_text("the human's own cut")
        guarded = {
            entry["path"]: entry["status"]
            for entry in self.coordinator.deliver_task_artifacts(task["id"])
        }
        self.assertEqual(guarded.get("renders/video.mp4"), "exists")
        self.assertEqual((root / "renders" / "video.mp4").read_text(), "the human's own cut")
        forced = {
            entry["path"]: entry["status"]
            for entry in self.coordinator.deliver_task_artifacts(task["id"], force=True)
        }
        self.assertEqual(forced.get("renders/video.mp4"), "delivered")

    def test_cleanup_removes_the_worker_directory_it_promises_to(self) -> None:
        """Its own message says the log is removed by cleanup. It was not.

        A worker directory is scratch space as well as a log -- one spike had
        pointed Xcode's derivedDataPath at it and left 15 GB behind -- and 110
        of 126 belonged to tasks whose worktree had already been cleaned.

        Prepared as an external worker rather than launched as a real
        `wait=False` process: a real runner keeps writing into its own worker
        directory (log rotation, config touches) for a moment after the exit
        file and result are recorded, which raced this assertion when the
        full suite ran fast enough for cleanup to land inside that window.
        `prepare_external_worker` gives the same worker-directory layout
        (config/log/exit files) with no process behind it to race, so the
        test is deterministic without weakening what cleanup itself asserts.
        """
        root = self.repo("workerdirs")
        project = self.coordinator.register_project(
            "WorkerDirs", str(root), project_id="workerdirs"
        )
        task = self.coordinator.create_task(project["id"], "do it")
        worker = self.coordinator.prepare_external_worker(
            task["id"], [sys.executable, "-c", ""], execution="external"
        )
        worker_dir = Path(worker["config_file"]).parent
        # Scratch an agent left behind, not just its log.
        (worker_dir / "derived-data").mkdir(exist_ok=True)
        (worker_dir / "derived-data" / "big.bin").write_text("x" * 1024)
        self.assertTrue(worker_dir.is_dir())
        Path(worker["exit_file"]).write_text(
            json.dumps({"returncode": 0}) + "\n", encoding="utf-8"
        )
        self.coordinator.record_worker_message(
            worker["id"], "result", "done", requested_status="completed"
        )

        self.coordinator.cleanup_task(task["id"])

        self.assertFalse(worker_dir.exists())

    def test_a_stood_down_escalated_foreman_can_still_be_cleaned_up(self) -> None:
        """Escalating no longer ends a foreman, so standing it down comes first.

        The status gate protects a checkout holding unreviewed work. A foreman
        has neither -- its workspace is empty and its evidence is the worker
        log, which cleanup never touches -- so the gate only left one empty
        directory per escalation that nothing could shed.

        What changed underneath this test: a foreman's `blocker` used to settle
        it, so an escalated foreman was already terminal and cleanup just
        worked. Now the blocker PAUSES it and the session stays live and
        addressable, which is the whole point -- the escalation must not
        destroy the escalator. The consequence is that cleanup must be
        preceded by a deliberate stand-down, and that is correct rather than
        awkward: shedding the workspace of an agent somebody may still be
        answering should be a decision, not a side effect.
        """
        root = self.repo("escalated")
        project = self.coordinator.register_project(
            "Escalated", str(root), project_id="escalated"
        )
        task = self.coordinator.create_task(project["id"], "drive it", role="foreman")
        worker = self.coordinator.launch_worker(
            task["id"], [sys.executable, "-c", ""], wait=False
        )
        self.coordinator.record_worker_message(
            worker["id"], "blocker", "needs a human", requested_status="blocked"
        )
        workspace = Path(task["workspace"])
        self.assertTrue(workspace.is_dir())
        log = Path(worker["log_file"])
        # The pause keeps it live on purpose, so cleanup refuses until someone
        # decides it is done. That refusal is the new behaviour working.
        with self.assertRaises(SafetyError):
            self.coordinator.cleanup_task(task["id"])
        # Its session is over; the separate session gate is not what this test
        # is about.
        Path(worker["exit_file"]).write_text(
            json.dumps({"returncode": 0}) + "\n", encoding="utf-8"
        )
        self.coordinator.stop_worker(worker["id"], "stood down before cleanup")

        self.coordinator.cleanup_task(task["id"])

        self.assertFalse(workspace.exists())
        # Cleanup takes the log too -- `helm worker stop` says so. What has to
        # outlive it is the record of why the foreman stopped, which is its
        # blocker message in state.
        self.assertFalse(log.exists())
        inspected = self.coordinator.inspect_task(task["id"])
        # `failed`, not `blocked`: the stand-down is what ended this foreman,
        # and the blocker only paused it. The status names how it ended; the
        # REASON it stopped is the blocker message below, which is what has to
        # survive cleanup and does.
        self.assertEqual(inspected["task"]["status"], "failed")
        self.assertTrue(
            any(
                m["kind"] == "blocker" and "needs a human" in (m.get("text") or "")
                for m in inspected["messages"]
            )
        )

    def test_a_blocked_worker_still_keeps_its_checkout(self) -> None:
        """The gate still holds where it was meant to: a task with a worktree."""
        root = self.repo("blockedwork")
        project = self.coordinator.register_project(
            "BlockedWork", str(root), project_id="blockedwork"
        )
        task = self.coordinator.create_task(project["id"], "write it")
        worker = self.coordinator.launch_worker(
            task["id"], [sys.executable, "-c", ""], wait=False
        )
        self.coordinator.record_worker_message(
            worker["id"], "blocker", "needs a human", requested_status="blocked"
        )

        with self.assertRaisesRegex(SafetyError, r"completed, failed, or merged"):
            self.coordinator.cleanup_task(task["id"])

    def test_releasing_a_project_keeps_the_work_and_says_what_it_kept(self) -> None:
        """Closing a space releases the pane and nothing else.

        Every decision behaved correctly and no step ever said "this project is
        done, let go of what it holds", which is how tens of gigabytes
        accumulated behind projects Helm considered finished.
        """
        root = self.repo("release")
        project = self.coordinator.register_project(
            "Release", str(root), project_id="release"
        )

        # Holds the change: completed, with commits nobody has reviewed yet.
        work = self.coordinator.create_task(project["id"], "write it")
        w1 = self.coordinator.launch_worker(work["id"], [sys.executable, "-c", ""], wait=False)
        self.commit_on_task_branch(work)
        Path(w1["exit_file"]).write_text(json.dumps({"returncode": 0}) + "\n", encoding="utf-8")
        self.coordinator.record_worker_message(
            w1["id"], "result", "done", requested_status="completed"
        )

        # Holds nothing: a foreman, which never edits.
        boss = self.coordinator.create_task(project["id"], "drive it", role="foreman")
        w2 = self.coordinator.launch_worker(boss["id"], [sys.executable, "-c", ""], wait=False)
        Path(w2["exit_file"]).write_text(json.dumps({"returncode": 0}) + "\n", encoding="utf-8")
        self.coordinator.record_worker_message(
            w2["id"], "result", "done", requested_status="completed"
        )

        outcome = self.coordinator.release_project(project["id"])

        self.assertIn(boss["id"], outcome["released"])
        kept = {entry["task_id"]: entry["reason"] for entry in outcome["kept"]}
        self.assertIn(work["id"], kept)
        self.assertIn("unmerged commit", kept[work["id"]])
        # The change is still there to be reviewed.
        self.assertTrue(Path(work["workspace"]).is_dir())
        self.assertFalse(Path(boss["workspace"]).exists())

    def test_releasing_a_project_refuses_while_anything_runs(self) -> None:
        """Letting go of a project that is still working would take it away."""
        root = self.repo("releasebusy")
        project = self.coordinator.register_project(
            "ReleaseBusy", str(root), project_id="releasebusy"
        )
        task = self.coordinator.create_task(project["id"], "write it")
        self.coordinator.launch_worker(task["id"], [sys.executable, "-c", ""], wait=False)

        with self.assertRaisesRegex(SafetyError, r"still has running worker"):
            self.coordinator.release_project(project["id"])

    def test_a_push_for_review_needs_authorization_and_a_clean_branch(self) -> None:
        root = self.repo("pushable")
        bare = Path(self.temp.name) / "pushable-remote.git"
        subprocess.run(["git", "init", "-q", "--bare", str(bare)], check=True)
        subprocess.run(["git", "-C", str(root), "remote", "add", "origin", str(bare)], check=True)
        # A remote with nothing pushed yet has no discoverable default; give
        # it one so registration resolves the base the same way it did
        # before a remote was ever named `origin` here.
        current_branch = self._run_git(root, "symbolic-ref", "--short", "HEAD")
        self._run_git(root, "push", "-q", "origin", current_branch)
        project = self.coordinator.register_project("Push", str(root), project_id="pushable")
        task = self.coordinator.create_task(project["id"], "make a change worth reviewing")
        code = (
            "from pathlib import Path; import subprocess; "
            "Path('change.txt').write_text('worker'); "
            "subprocess.run(['git','add','change.txt'],check=True); "
            "subprocess.run(['git','commit','-m','worker change'],check=True)"
        )
        self.coordinator.launch_worker(task["id"], [sys.executable, "-c", code])

        # Pushing leaves the machine, so it never happens by default.
        with self.assertRaisesRegex(SafetyError, r"needs explicit authorization"):
            self.coordinator.publish_task_branch(task["id"])

        pushed = self.coordinator.publish_task_branch(task["id"], confirm=True)
        self.assertEqual(pushed["branch"], task["branch"])
        self.assertEqual(pushed["authorized_by"], "explicit --confirm")
        remote_branches = subprocess.run(
            ["git", "-C", str(bare), "branch", "--list", task["branch"]],
            text=True, stdout=subprocess.PIPE, check=True,
        ).stdout
        self.assertIn(task["branch"], remote_branches)

        # A standing push grant authorizes it without a per-push flag.
        second = self.coordinator.create_task(project["id"], "another change")
        self.coordinator.launch_worker(second["id"], [sys.executable, "-c", code])
        self.coordinator.grant_approval("push", project_id=project["id"], note="review on the remote")
        self.assertTrue(self.coordinator.publish_task_branch(second["id"])["authorized_by"].startswith("g-"))

        # Uncommitted work would be missing from the PR, so pushing refuses.
        third = self.coordinator.create_task(project["id"], "dirty change")
        self.coordinator.launch_worker(third["id"], [sys.executable, "-c", code])
        (Path(self.coordinator.inspect_task(third["id"])["task"]["workspace"]) / "stray.txt").write_text("x")
        with self.assertRaisesRegex(SafetyError, r"uncommitted changes"):
            self.coordinator.publish_task_branch(third["id"], confirm=True)

    def test_a_read_only_task_cannot_be_published(self) -> None:
        """Same invariant as approve_task/merge_task: a read-only task was
        never a candidate for delivery, and publish is the one delivery path
        that leaves the machine, so --confirm must not reach the push."""
        root = self.repo("readonlypublish")
        bare = Path(self.temp.name) / "readonlypublish-remote.git"
        subprocess.run(["git", "init", "-q", "--bare", str(bare)], check=True)
        subprocess.run(["git", "-C", str(root), "remote", "add", "origin", str(bare)], check=True)
        current_branch = self._run_git(root, "symbolic-ref", "--short", "HEAD")
        self._run_git(root, "push", "-q", "origin", current_branch)
        project = self.coordinator.register_project(
            "ReadOnlyPublish", str(root), project_id="readonlypublish"
        )
        task = self.coordinator.create_task(
            project["id"], "just look around", read_only=True
        )
        self.coordinator.allocate_task(task["id"])

        with self.assertRaisesRegex(SafetyError, "read-only.*cannot be published"):
            self.coordinator.publish_task_branch(task["id"], confirm=True)

    def test_pr_delivery_stays_open_until_the_pr_is_merged(self) -> None:
        root = self.repo("prflow")
        project = self.coordinator.register_project(
            "PR Flow", str(root), project_id="prflow", delivery_policy="pr"
        )
        task = self.coordinator.create_task(project["id"], "make a PR change")
        code = (
            "from pathlib import Path; import subprocess; "
            "Path('change.txt').write_text('worker'); "
            "subprocess.run(['git','add','change.txt'],check=True); "
            "subprocess.run(['git','commit','-m','worker change'],check=True)"
        )
        self.coordinator.launch_worker(task["id"], [sys.executable, "-c", code])

        opened = self.coordinator.record_pr_status(
            task["id"], state="open", url="https://example.invalid/pull/1", comments=2
        )

        self.assertEqual(opened["status"], "pr-open")
        self.assertEqual(opened["delivery"]["state"], "pr-open")
        self.assertEqual(opened["delivery"]["comments"], 2)
        status = self.coordinator.project_status(project["id"])
        self.assertIn(task["id"], [entry["task_id"] for entry in status["unmerged"]])
        with self.assertRaisesRegex(SafetyError, r"cleanup is allowed only"):
            self.coordinator.cleanup_task(task["id"])

        merged = self.coordinator.record_pr_status(
            task["id"],
            state="merged",
            url="https://example.invalid/pull/1",
            checks="green",
            review_decision="APPROVED",
            merge_commit="abc1234",
        )

        self.assertEqual(merged["status"], "pr-merged")
        self.assertEqual(merged["delivery"]["state"], "pr-merged")
        self.assertEqual(merged["delivery"]["merge_commit"], "abc1234")
        status = self.coordinator.project_status(project["id"])
        self.assertNotIn(task["id"], [entry["task_id"] for entry in status["unmerged"]])
        cleaned = self.coordinator.cleanup_task(task["id"])
        self.assertTrue(cleaned["workspace_removed"])

    def test_pushing_a_branch_is_recorded_but_is_not_pr_delivery(self) -> None:
        root = self.repo("push-state")
        bare = Path(self.temp.name) / "push-state-remote.git"
        subprocess.run(["git", "init", "-q", "--bare", str(bare)], check=True)
        subprocess.run(["git", "-C", str(root), "remote", "add", "origin", str(bare)], check=True)
        # A remote with nothing pushed yet has no discoverable default; give
        # it one so registration resolves the base the same way it did
        # before a remote was ever named `origin` here.
        current_branch = self._run_git(root, "symbolic-ref", "--short", "HEAD")
        self._run_git(root, "push", "-q", "origin", current_branch)
        project = self.coordinator.register_project(
            "Push State", str(root), project_id="push-state", delivery_policy="pr"
        )
        task = self.coordinator.create_task(project["id"], "push but no PR yet")
        code = (
            "from pathlib import Path; import subprocess; "
            "Path('change.txt').write_text('worker'); "
            "subprocess.run(['git','add','change.txt'],check=True); "
            "subprocess.run(['git','commit','-m','worker change'],check=True)"
        )
        self.coordinator.launch_worker(task["id"], [sys.executable, "-c", code])

        self.coordinator.publish_task_branch(task["id"], confirm=True)
        pushed = self.coordinator.inspect_task(task["id"])["task"]

        self.assertEqual(pushed["status"], "completed")
        self.assertEqual(pushed["delivery"]["events"][-1]["state"], "branch-pushed")
        self.assertNotEqual(pushed["delivery"].get("state"), "pr-open")

    def test_pr_sync_records_comments_checks_and_merge_from_gh(self) -> None:
        root = self.repo("prsync")
        project = self.coordinator.register_project(
            "PR Sync", str(root), project_id="prsync", delivery_policy="pr"
        )
        task = self.coordinator.create_task(project["id"], "sync a PR")
        self.coordinator.launch_worker(task["id"], [sys.executable, "-c", ""])
        self.coordinator.record_pr_status(
            task["id"], state="open", url="https://example.invalid/pull/2"
        )
        payload = {
            "url": "https://example.invalid/pull/2",
            "state": "MERGED",
            "reviewDecision": "APPROVED",
            "mergeStateStatus": "CLEAN",
            "mergeCommit": {"oid": "def5678"},
            "comments": [{"body": "done"}, {"body": "thanks"}],
        }
        with mock.patch.object(cli.shutil, "which", return_value="/usr/bin/gh"), \
             mock.patch.object(
                 cli.subprocess,
                 "run",
                 return_value=subprocess.CompletedProcess(
                     ["gh"], 0, stdout=json.dumps(payload), stderr=""
                 ),
             ):
            synced = cli._sync_pull_request_status(self.coordinator, task["id"])

        self.assertEqual(synced["status"], "pr-merged")
        self.assertEqual(synced["delivery"]["comments"], 2)
        self.assertEqual(synced["delivery"]["checks"], "CLEAN")
        self.assertEqual(synced["delivery"]["review_decision"], "APPROVED")
        self.assertEqual(synced["delivery"]["merge_commit"], "def5678")

    def test_cleanup_still_refuses_a_dirty_workspace_it_could_now_force(self) -> None:
        root = self.repo("forcing")
        project = self.coordinator.register_project(
            "Force", str(root), project_id="forcing"
        )
        task = self.coordinator.create_task(project["id"], "write something")
        worker = self.coordinator.launch_worker(
            task["id"], [sys.executable, "-c", "import time; time.sleep(2)"], wait=False
        )
        self.coordinator.record_worker_message(worker["id"], "result", "done")
        # Cleanup removes the directory the session sits in, so this waits for
        # the session, which is not what settling the worker means.
        self.await_session_exit(worker)
        workspace = Path(task["workspace"])
        (workspace / "unsaved.txt").write_text("work nobody committed", encoding="utf-8")

        # Removal now passes --force, because git refuses outright to remove a
        # worktree containing submodules and that made cleanup impossible for
        # any project with one. The dirty check that --force would override is
        # Helm's own, made first -- so it must still bite.
        with self.assertRaises(SafetyError):
            self.coordinator.cleanup_task(task["id"])
        self.assertTrue((workspace / "unsaved.txt").exists())

        (workspace / "unsaved.txt").unlink()
        cleaned = self.coordinator.cleanup_task(task["id"])
        self.assertTrue(cleaned["workspace_removed"])
        self.assertFalse(workspace.exists())

    def test_cleanup_reconciles_a_worktree_that_was_removed_outside_helm(self) -> None:
        root = self.repo("reconciling")
        project = self.coordinator.register_project(
            "Reconcile", str(root), project_id="reconciling"
        )
        task = self.coordinator.create_task(project["id"], "write something")
        worker = self.coordinator.launch_worker(
            task["id"], [sys.executable, "-c", "import time; time.sleep(2)"], wait=False
        )
        self.coordinator.record_worker_message(worker["id"], "result", "done")
        # Cleanup removes the directory the session sits in, so this waits for
        # the session, which is not what settling the worker means.
        self.await_session_exit(worker)

        # Removed by hand, or by a tool that got there first. The record then
        # claimed a worktree that was gone, and cleanup -- the only command
        # that could correct it -- refused because the directory was missing.
        subprocess.run(
            ["git", "-C", str(root), "worktree", "remove", "--force", task["workspace"]],
            check=True,
        )
        cleaned = self.coordinator.cleanup_task(task["id"])
        self.assertTrue(cleaned["workspace_removed"])
        # Idempotent, so it is safe to run over a whole project's tasks.
        self.assertTrue(self.coordinator.cleanup_task(task["id"])["workspace_removed"])

    def test_cleanup_deletes_a_task_branch_that_holds_no_unmerged_work(self) -> None:
        # Cleanup removed the worktree and left the branch behind forever, so
        # every cleaned task leaked a ref nobody could account for.
        root, task = self._settled_task("shedding")
        self.assertIn(task["branch"], self._branches(root))

        cleaned = self.coordinator.cleanup_task(task["id"])
        self.assertTrue(cleaned["workspace_removed"])
        self.assertTrue(cleaned["branch_removed"])
        self.assertNotIn(task["branch"], self._branches(root))
        # Idempotent, like the workspace half of cleanup.
        self.assertTrue(self.coordinator.cleanup_task(task["id"])["branch_removed"])

    def test_cleanup_removes_a_foreman_workspace_that_is_not_a_worktree(self) -> None:
        root = self.repo("shedding-foreman")
        project = self.coordinator.register_project(
            "Shed", str(root), project_id="shedding-foreman"
        )
        foreman = self.coordinator.create_foreman_task(project["id"])
        worker = self.coordinator.launch_worker(
            foreman["id"], [sys.executable, "-c", ""], wait=False
        )
        self.coordinator.record_worker_message(worker["id"], "result", "driven")
        # Cleanup removes the directory the session sits in, so this waits for
        # the session, which is not what settling the worker means.
        self.await_session_exit(worker)
        workspace = Path(self.coordinator.inspect_task(foreman["id"])["task"]["workspace"])
        self.assertTrue(workspace.is_dir())

        cleaned = self.coordinator.cleanup_task(foreman["id"])
        self.assertTrue(cleaned["workspace_removed"])
        self.assertFalse(workspace.exists())

    def test_cleanup_refuses_while_a_settled_worker_session_is_still_open(self) -> None:
        # Settling a worker on its terminal result makes the work reviewable
        # without waiting for the provider to exit -- an interactive agent
        # reports and keeps its session open. Cleanup ends in `worktree remove
        # --force`, so it must not take the directory out from under a session
        # that is still sitting in it, however settled the record looks.
        root = self.repo("still-open")
        project = self.coordinator.register_project("Open", str(root), project_id="still-open")
        task = self.coordinator.create_task(project["id"], "report and keep the pane open")
        herdr = FakeHerdr()
        adapter = HerdrAdapter(self.coordinator, herdr)
        worker = adapter.launch_task(task["id"], [sys.executable, "-c", ""], wait=False)
        Path(worker["log_file"]).write_text(
            json.dumps({"helm": 1, "type": "result", "text": "ready"}) + "\n",
            encoding="utf-8",
        )
        adapter.poll_worker(worker["id"])

        report = self.coordinator.inspect_task(task["id"])
        self.assertEqual(report["task"]["status"], "completed")
        self.assertEqual(report["workers"][0]["status"], "completed")
        workspace = Path(task["workspace"])
        with self.assertRaises(SafetyError) as refused:
            self.coordinator.cleanup_task(task["id"])
        self.assertIn("session has not ended", str(refused.exception))
        self.assertTrue(workspace.exists())

        # The runner records the exit when the session really ends, and that
        # is what stopping the worker produces too.
        Path(worker["exit_file"]).write_text(
            json.dumps({"returncode": 0}) + "\n", encoding="utf-8"
        )
        cleaned = self.coordinator.cleanup_task(task["id"])
        self.assertTrue(cleaned["workspace_removed"])
        self.assertFalse(workspace.exists())

    def test_cleanup_keeps_a_task_branch_that_still_holds_unmerged_commits(self) -> None:
        # A branch the base does not have is the same work the dirty-workspace
        # refusal protects, one commit further along -- cleanup must not
        # silently discard it just because the worktree is going away.
        root, task = self._settled_task("preserving")
        workspace = Path(task["workspace"])
        (workspace / "kept.txt").write_text("committed work", encoding="utf-8")
        subprocess.run(["git", "-C", str(workspace), "add", "kept.txt"], check=True)
        subprocess.run(["git", "-C", str(workspace), "commit", "-qm", "work"], check=True)

        cleaned = self.coordinator.cleanup_task(task["id"])
        self.assertTrue(cleaned["workspace_removed"])
        self.assertFalse(cleaned["branch_removed"])
        self.assertIn(task["branch"], self._branches(root))
        messages = self.coordinator.inspect_task(task["id"])["messages"]
        kept = [m for m in messages if "kept" in m["text"]]
        self.assertTrue(kept and "--delete-branch" in kept[-1]["text"])

        # Discarding it stays possible, but only when asked for by name.
        discarded = self.coordinator.cleanup_task(task["id"], delete_branch=True)
        self.assertTrue(discarded["branch_removed"])
        self.assertNotIn(task["branch"], self._branches(root))

    def test_cleanup_sheds_the_branch_of_a_worktree_removed_outside_helm(self) -> None:
        # The reconcile path returned early, so a record corrected by hand kept
        # its branch even though the task was finished with.
        root, task = self._settled_task("reconciled-branch")
        subprocess.run(
            ["git", "-C", str(root), "worktree", "remove", "--force", task["workspace"]],
            check=True,
        )
        cleaned = self.coordinator.cleanup_task(task["id"])
        self.assertTrue(cleaned["workspace_removed"])
        self.assertTrue(cleaned["branch_removed"])
        self.assertNotIn(task["branch"], self._branches(root))

    def test_cleanup_never_deletes_a_branch_helm_did_not_name_for_the_task(self) -> None:
        # A record carrying a base or user branch must not make cleanup a way
        # to delete it. On the normal path _verify_workspace_record already
        # refuses the mismatch; the reconcile path returns before that check,
        # so the branch guard is what stands there.
        root, task = self._settled_task("guarded")
        subprocess.run(
            ["git", "-C", str(root), "worktree", "remove", "--force", task["workspace"]],
            check=True,
        )
        with self.coordinator.store.locked() as data:
            record = data["tasks"][task["id"]]
            record["branch"] = record["base_branch"]
        cleaned = self.coordinator.cleanup_task(task["id"], delete_branch=True)
        self.assertTrue(cleaned["workspace_removed"])
        self.assertIn(cleaned["base_branch"], self._branches(root))

    def test_a_worker_result_keeps_the_space_and_leaves_a_decision_behind(self) -> None:
        """The exact order `helm worker report` runs, on the case it broke.

        Record the result, release the finished tab, then check whether the
        project's space can close. The close check read a task with no pane as
        nothing left to show -- and releasing the tab is precisely what a clean
        result does -- so the worker's own success closed the space over a
        change nobody had reviewed, merged, or cleaned up.
        """
        root = self.repo("gate")
        project = self.coordinator.register_project("Gate", str(root), project_id="gate")
        task = self.coordinator.create_task(project["id"], "write the change")
        herdr = FakeHerdr()
        adapter = HerdrAdapter(self.coordinator, herdr)
        worker = adapter.launch_task(task["id"], [sys.executable, "-c", ""], wait=False)

        output = io.StringIO()
        with contextlib.redirect_stdout(output), mock.patch.object(
            cli, "HerdrAdapter", return_value=adapter
        ):
            self.assertEqual(
                cli.main([
                    "--state-dir", str(self.state.directory),
                    "worker", "message", worker["id"],
                    "--type", "result", "--text", "implemented and committed",
                ]),
                0,
            )

        # The tab goes -- it has nothing left to show -- and the space stays.
        self.assertEqual(len(herdr.closed_tabs), 1)
        self.assertEqual(herdr.closed_workspaces, [])
        status = self.coordinator.project_status(project["id"])
        self.assertTrue(
            any(
                "Worker result:" in entry["text"]
                and "implemented and committed" in entry["text"]
                for entry in status["situation"]
            )
        )
        self.assertEqual([item["task_id"] for item in self._decisions(project["id"])], [task["id"]])
        self.assertIn("Commander decision pending", output.getvalue())

    def test_the_outcome_is_routed_before_anything_that_closes_a_pane(self) -> None:
        """Durable storage is not delivery, and the report runs in the pane.

        A worker reports by running a Helm command inside its own tab, so the
        confirmation is printed onto the exact surface the next two calls
        remove. Recording the result correctly, releasing the tab and closing
        the space then leaves a completed, unmerged change that the session
        driving the project never heard about -- it was found by inspecting the
        task by hand after the pane had gone. So the outcome and the decision
        must reach a surface that outlives the pane BEFORE either call runs.
        """
        root = self.repo("routed")
        project = self.coordinator.register_project(
            "Routed", str(root), project_id="routed"
        )
        task = self.coordinator.create_task(project["id"], "write the change")
        herdr = FakeHerdr()
        adapter = HerdrAdapter(self.coordinator, herdr)
        worker = adapter.launch_task(task["id"], [sys.executable, "-c", ""], wait=False)
        overview = self.state.load()["integrations"]["herdr"]["projects"][
            project["id"]
        ]["overview_pane_id"]

        # Watch the order, and what had already been delivered at each step.
        order: list[str] = []
        seen: dict[str, dict] = {}

        def snapshot(name: str) -> dict:
            live = self.state.load()
            return {
                "handoff": (live["workers"][worker["id"]].get("outcome_handoff") or {}),
                "pane": [text for pane, text in herdr.runs if pane == overview],
                "decisions": self._decisions(project["id"]),
                "step": name,
            }

        def watched(name: str, call):
            def wrapper(*a, **kw):
                order.append(name)
                seen.setdefault(name, snapshot(name))
                return call(*a, **kw)
            return wrapper

        with contextlib.redirect_stdout(io.StringIO()), mock.patch.object(
            cli, "HerdrAdapter", return_value=adapter
        ), mock.patch.object(
            adapter, "notify_coordinator",
            side_effect=watched("notify", adapter.notify_coordinator),
        ), mock.patch.object(
            adapter, "release_finished_tabs",
            side_effect=watched("release", adapter.release_finished_tabs),
        ), mock.patch.object(
            adapter, "close_project_space_if_finished",
            side_effect=watched("close", adapter.close_project_space_if_finished),
        ):
            self.assertEqual(
                cli.main([
                    "--state-dir", str(self.state.directory),
                    "worker", "message", worker["id"],
                    "--type", "result", "--text", "implemented and committed",
                ]),
                0,
            )

        self.assertEqual(order, ["notify", "release", "close"])
        # By the time the first pane-closing call runs, the outcome had already
        # been delivered somewhere that outlives the pane -- and the decision
        # had already been raised.
        at_release = seen["release"]
        self.assertIn("status-record", at_release["handoff"]["channels"])
        self.assertIn("project-pane", at_release["handoff"]["channels"])
        self.assertEqual(
            [item["task_id"] for item in at_release["decisions"]], [task["id"]]
        )
        # Delivered to the project's own overview pane, which is not the tab
        # about to be released.
        routed = "\n".join(at_release["pane"])
        self.assertIn("FINAL OUTCOME", routed)
        self.assertIn("implemented and committed", routed)
        self.assertIn("DECISION NEEDED", routed)
        self.assertIn(task["id"], routed)
        # And the space is still standing after all three calls.
        self.assertEqual(herdr.closed_workspaces, [])
        self.assertEqual(len(herdr.closed_tabs), 1)

    def test_the_outcome_route_does_not_wait_for_a_foreman_to_exist(self) -> None:
        """The no-driver case is the one that needed telling, not the exception."""
        root = self.repo("nodriverroute")
        project = self.coordinator.register_project(
            "NoDriverRoute", str(root), project_id="nodriverroute"
        )
        task = self.coordinator.create_task(project["id"], "write the change")
        herdr = FakeHerdr()
        adapter = HerdrAdapter(self.coordinator, herdr)
        worker = adapter.launch_task(task["id"], [sys.executable, "-c", ""], wait=False)
        self.assertIsNone(self.coordinator.foreman_for(project["id"]))
        self.coordinator.record_worker_message(
            worker["id"], "result", "implemented and committed"
        )

        routed = adapter.notify_coordinator(worker["id"])

        self.assertNotIn("foreman", routed["channels"])
        self.assertIn("status-record", routed["channels"])
        self.assertIn("project-pane", routed["channels"])
        self.assertIn("DECISION NEEDED", routed["notice"]["text"])

        # With a driver, it is told as well -- the route widens, it does not
        # move.
        other = self.repo("drivenroute")
        second = self.coordinator.register_project(
            "DrivenRoute", str(other), project_id="drivenroute"
        )
        foreman_task = self.coordinator.create_foreman_task(second["id"])
        foreman = adapter.launch_task(
            foreman_task["id"], [sys.executable, "-c", ""], wait=False
        )
        driven = self.coordinator.create_task(second["id"], "write more")
        coder = adapter.launch_task(driven["id"], [sys.executable, "-c", ""], wait=False)
        self.coordinator.record_worker_message(coder["id"], "result", "done")

        with_driver = adapter.notify_coordinator(coder["id"])

        self.assertIn("foreman", with_driver["channels"])
        self.assertIn("status-record", with_driver["channels"])
        foreman_pane = self.state.load()["integrations"]["herdr"]["workers"][
            foreman["id"]
        ]["pane_id"]
        told = [text for pane, text in herdr.sent_text if pane == foreman_pane]
        self.assertTrue(any(driven["id"] in text for text in told))

    def test_a_tab_is_never_released_while_its_outcome_has_reached_nothing(self) -> None:
        """The pane is the last copy, so it is not the thing to throw away."""
        root = self.repo("unroutable")
        project = self.coordinator.register_project(
            "Unroutable", str(root), project_id="unroutable"
        )
        task = self.coordinator.create_task(project["id"], "write the change")
        herdr = FakeHerdr()
        adapter = HerdrAdapter(self.coordinator, herdr)
        worker = adapter.launch_task(task["id"], [sys.executable, "-c", ""], wait=False)
        self.coordinator.record_worker_message(worker["id"], "result", "done")

        with mock.patch.object(
            adapter, "notify_coordinator",
            return_value={"channels": [], "notice": {"task_id": task["id"]}},
        ):
            self.assertEqual(adapter.release_finished_tabs(), [])
        self.assertEqual(herdr.closed_tabs, [])

        # Once it has landed somewhere, the tab is free to go.
        self.assertEqual(adapter.release_finished_tabs(), [worker["id"]])
        self.assertEqual(len(herdr.closed_tabs), 1)

    def test_the_durable_channel_is_claimed_only_when_the_record_really_has_it(
        self,
    ) -> None:
        """A channel name is not a delivery.

        The durable write runs in the unlocked effects pass, where a failure is
        suppressed so it cannot cost the worker its message. Asserting
        `status-record` because the notice had text would therefore claim a
        delivery nobody made -- and that claim is what releases the tab and
        closes the space, so the outcome would vanish with its only copy.
        """
        root = self.repo("unwritten")
        project = self.coordinator.register_project(
            "Unwritten", str(root), project_id="unwritten"
        )
        task = self.coordinator.create_task(project["id"], "write the change")
        herdr = FakeHerdr()
        adapter = HerdrAdapter(self.coordinator, herdr)
        worker = adapter.launch_task(task["id"], [sys.executable, "-c", ""], wait=False)

        # The record refuses every write, exactly as a full disk or a
        # permission change would, and the effects pass swallows it.
        def refuse(*args: Any, **kwargs: Any) -> None:
            raise HelmError("the record could not be written")

        with mock.patch.object(Coordinator, "record_situation", refuse), \
                mock.patch.object(Coordinator, "record_project_action_item", refuse):
            self.coordinator.record_worker_message(worker["id"], "result", "done")
            notice = self.coordinator.compose_outcome_handoff(worker["id"])
            self.assertIsNotNone(notice)
            self.assertFalse(self.coordinator.outcome_reached_the_record(notice))
            routed = adapter.notify_coordinator(worker["id"])
            # Another surface may still have taken it -- that is a real
            # delivery and is allowed to release the pane. What must never
            # happen is the durable channel claiming an outcome it never got.
            self.assertNotIn("status-record", routed["channels"])

        # With the record writable, the same outcome lands and is claimed.
        self.coordinator.record_delivery_decision(project["id"], task_id=task["id"])
        self.assertTrue(
            self.coordinator.outcome_reached_the_record(
                self.coordinator.compose_outcome_handoff(worker["id"])
            )
        )
        self.assertIn(
            "status-record", adapter.notify_coordinator(worker["id"])["channels"]
        )

    def test_a_long_final_summary_is_kept_rather_than_dropped_for_length(self) -> None:
        """A refused situation line loses the one record of the outcome.

        `record_situation` refuses an over-long note instead of cutting it,
        which is right for a note somebody wrote. A generated mirror of a
        worker's result is different: the full text is already durable in the
        message record, so refusing it just means the project's record has
        nothing at all about how the work ended.
        """
        root = self.repo("longresult")
        project = self.coordinator.register_project(
            "Long", str(root), project_id="longresult"
        )
        task = self.coordinator.create_task(project["id"], "write a lot")
        worker = self.coordinator.launch_worker(
            task["id"], [sys.executable, "-c", ""], wait=False
        )

        self.coordinator.record_worker_message(worker["id"], "result", "x" * 900)

        situation = self.coordinator.project_status(project["id"])["situation"]
        kept = [entry for entry in situation if "Worker result:" in entry["text"]]
        self.assertEqual(len(kept), 1)
        self.assertLessEqual(len(kept[0]["text"]), Coordinator.SITUATION_LINE_LIMIT)
        self.assertIn(task["id"], kept[0]["text"])

    def test_a_live_foreman_keeps_the_decision_off_the_commanders_desk(self) -> None:
        """A driver is still driving; asking the commander now is premature."""
        root = self.repo("driven")
        project = self.coordinator.register_project(
            "Driven", str(root), project_id="driven"
        )
        foreman_task = self.coordinator.create_foreman_task(project["id"])
        foreman = self.coordinator.prepare_external_worker(
            foreman_task["id"], [sys.executable, "-c", ""]
        )
        task = self.coordinator.create_task(project["id"], "write the code")
        coder = self.coordinator.prepare_external_worker(
            task["id"], [sys.executable, "-c", ""]
        )

        self.coordinator.record_worker_message(coder["id"], "result", "done and committed")

        # Recorded durably, and handed to the driver rather than to a human.
        status = self.coordinator.project_status(project["id"])
        self.assertTrue(any("Worker result:" in e["text"] for e in status["situation"]))
        self.assertEqual(self._decisions(project["id"]), [])
        adapter = HerdrAdapter(self.coordinator, FakeHerdr())
        with mock.patch.object(adapter, "answer_worker", return_value=True) as told:
            self.assertTrue(adapter.notify_foreman(coder["id"]))
        self.assertEqual(told.call_args[0][0], foreman["id"])

        # When the driver finishes, the work it leaves behind becomes the
        # commander's -- with no structured payload field anywhere in sight.
        self.coordinator.record_worker_message(foreman["id"], "result", "project driven")

        decisions = self._decisions(project["id"])
        self.assertEqual([item["task_id"] for item in decisions], [task["id"]])
        self.assertTrue(
            any("Foreman report:" in e["text"] for e in
                self.coordinator.project_status(project["id"])["situation"])
        )

    def test_a_foreman_handover_stays_project_scoped_and_is_raised_once(self) -> None:
        root = self.repo("handover")
        project = self.coordinator.register_project(
            "Handover", str(root), project_id="handover"
        )
        first = self.coordinator.create_task(project["id"], "first change")
        second = self.coordinator.create_task(project["id"], "second change")
        foreman_task = self.coordinator.create_foreman_task(project["id"])
        foreman = self.coordinator.prepare_external_worker(
            foreman_task["id"], [sys.executable, "-c", ""]
        )

        self.coordinator.record_worker_message(foreman["id"], "result", "handing back")

        decisions = self._decisions(project["id"])
        self.assertEqual(len(decisions), 1)
        # Two candidates, so it names neither rather than picking one.
        self.assertIsNone(decisions[0]["task_id"])
        self.assertIn("unresolved task work", decisions[0]["text"])

        # A second report -- or a second path raising the same gate -- must not
        # print the same decision twice.
        self.coordinator.raise_delivery_decision_for_project(project["id"])
        self.coordinator.record_delivery_decision(project["id"], source="somewhere else")
        self.assertEqual(len(self._decisions(project["id"])), 1)

        # Resolving one task is not resolving the project's work.
        with self.coordinator.store.locked() as data:
            data["tasks"][first["id"]]["status"] = "merged"
        self.assertEqual(len(self._decisions(project["id"])), 1)
        with self.coordinator.store.locked() as data:
            data["tasks"][second["id"]]["status"] = "pr-merged"
        self.assertEqual(self._decisions(project["id"]), [])

    def test_a_project_that_declines_a_foreman_still_gets_the_decision(self) -> None:
        """Opting out of a driver must not opt out of being told."""
        root = self.repo("nodriver")
        (root / ".helm").mkdir()
        (root / ".helm" / "project.json").write_text(json.dumps({"foreman": False}))
        project = self.coordinator.register_project(
            "NoDriver", str(root), project_id="nodriver"
        )
        self.assertFalse(self.coordinator.project_wants_foreman(project["id"]))
        task = self.coordinator.create_task(project["id"], "write the change")
        worker = self.coordinator.launch_worker(
            task["id"], [sys.executable, "-c", ""], wait=False
        )

        self.coordinator.record_worker_message(worker["id"], "result", "done")

        self.assertEqual([i["task_id"] for i in self._decisions(project["id"])], [task["id"]])

    def test_a_foreman_standing_down_hands_over_what_it_was_driving(self) -> None:
        """Nothing left to drive is not the same as nothing left to decide."""
        root = self.repo("standdown")
        project = self.coordinator.register_project(
            "StandDown", str(root), project_id="standdown"
        )
        task = self.coordinator.create_task(project["id"], "write the change")
        worker = self.coordinator.launch_worker(
            task["id"], [sys.executable, "-c", ""], wait=False
        )
        foreman_task = self.coordinator.create_foreman_task(project["id"])
        foreman = self.coordinator.prepare_external_worker(
            foreman_task["id"], [sys.executable, "-c", ""]
        )
        # The worker's result went to the foreman, so no gate was raised.
        self.coordinator.record_worker_message(worker["id"], "result", "done")
        self.coordinator.stop_worker(worker["id"], "settled")
        self.assertEqual(self._decisions(project["id"]), [])

        stood_down = self.coordinator.stand_down_idle_foreman(project["id"])

        self.assertIsNotNone(stood_down)
        self.assertEqual(stood_down["id"], foreman["id"])
        self.assertEqual([i["task_id"] for i in self._decisions(project["id"])], [task["id"]])

    def test_a_stillborn_launch_does_not_shadow_delivered_work(self) -> None:
        """A failed worker that never wrote a byte is not the task's latest
        round; the delivered round before it still approves and merges."""
        root, project, task = self._completed_task_awaiting_approval("stillborn")
        dead_log = Path(self.temp.name) / "stillborn.log"
        dead_log.write_text("", encoding="utf-8")
        with self.coordinator.store.locked() as data:
            data["workers"]["w-stillborn"] = {
                "id": "w-stillborn",
                "task_id": task["id"],
                "project_id": project["id"],
                "status": "failed",
                "started_at": "9999-01-01T00:00:00Z",
                "log_file": str(dead_log),
            }
        self.coordinator.approve_task(task["id"], "reviewed")
        self.coordinator.merge_task(task["id"])
        merged = self.coordinator.store.load()["tasks"][task["id"]]
        self.assertEqual(merged["status"], "merged")

    def test_a_read_only_verification_round_keeps_the_task_deliverable(self) -> None:
        """`read_only` reflects the latest round; a delivery task closed by a
        read-only verification round is still a delivery candidate."""
        root, project, task = self._completed_task_awaiting_approval("verifyround")
        self.coordinator.continue_task(task["id"], "verify the tip", read_only=True)
        self.coordinator.launch_worker(task["id"], [sys.executable, "-c", ""])
        self.coordinator.approve_task(task["id"], "reviewed")
        self.coordinator.merge_task(task["id"])
        merged = self.coordinator.store.load()["tasks"][task["id"]]
        self.assertEqual(merged["status"], "merged")

    def test_a_delivery_decision_resolves_when_the_work_is_merged(self) -> None:
        root, project, task = self._completed_task_awaiting_approval("mergegate")
        self.coordinator.record_delivery_decision(
            project["id"], task_id=task["id"], source="Worker result"
        )
        self.assertEqual(len(self._decisions(project["id"])), 1)

        self.coordinator.approve_task(task["id"], "reviewed")
        self.coordinator.merge_task(task["id"])

        self.assertEqual(self._decisions(project["id"]), [])
        status = self.coordinator._load_status(project["id"])
        closed = [i for i in status["action_items"] if i["status"] == "resolved"]
        self.assertEqual(closed[0]["resolved_reason"], "merged")

    def test_a_delivery_decision_resolves_on_pr_merge_continue_and_cleanup(self) -> None:
        root = self.repo("prgate")
        project = self.coordinator.register_project(
            "PRGate", str(root), project_id="prgate", delivery_policy="pr"
        )
        code = (
            "from pathlib import Path; import subprocess; "
            "Path('change.txt').write_text('worker'); "
            "subprocess.run(['git','add','change.txt'],check=True); "
            "subprocess.run(['git','commit','-m','worker change'],check=True)"
        )
        task = self.coordinator.create_task(project["id"], "make a PR change")
        self.coordinator.launch_worker(task["id"], [sys.executable, "-c", code])
        self.coordinator.record_delivery_decision(project["id"], task_id=task["id"])

        # An open PR is not delivery, so the decision stays.
        self.coordinator.record_pr_status(
            task["id"], state="open", url="https://example.invalid/pull/1"
        )
        self.assertEqual(len(self._decisions(project["id"])), 1)
        self.coordinator.record_pr_status(
            task["id"], state="merged", url="https://example.invalid/pull/1"
        )
        self.assertEqual(self._decisions(project["id"]), [])

        # Continuing IS the decision, even though it reopens the task.
        other = self.repo("continuegate")
        second = self.coordinator.register_project(
            "ContinueGate", str(other), project_id="continuegate"
        )
        rounds = self.coordinator.create_task(second["id"], "another round")
        worker = self.coordinator.launch_worker(
            rounds["id"], [sys.executable, "-c", ""], wait=False
        )
        self.coordinator.record_worker_message(worker["id"], "result", "round one done")
        self.assertEqual(len(self._decisions(second["id"])), 1)
        self.coordinator.continue_task(rounds["id"], "fix the finding")
        self.assertEqual(self._decisions(second["id"]), [])

        # And cleanup resolves it for work that is simply thrown away.
        third = self.repo("cleanupgate")
        dropped = self.coordinator.register_project(
            "CleanupGate", str(third), project_id="cleanupgate"
        )
        spike = self.coordinator.create_task(dropped["id"], "a spike")
        spiker = self.coordinator.launch_worker(
            spike["id"], [sys.executable, "-c", ""], wait=False
        )
        self.coordinator.record_worker_message(spiker["id"], "result", "spike done")
        Path(spiker["exit_file"]).write_text(
            json.dumps({"returncode": 0}) + "\n", encoding="utf-8"
        )
        self.assertEqual(len(self._decisions(dropped["id"])), 1)
        self.coordinator.cleanup_task(spike["id"])
        self.assertEqual(self._decisions(dropped["id"]), [])

    def test_delivered_work_still_holding_disk_raises_a_cleanup_gate(self) -> None:
        """Merged closes the delivery gate and opens the finalization one."""
        root, project, task = self._completed_task_awaiting_approval("finalize")
        self.coordinator.record_delivery_decision(project["id"], task_id=task["id"])
        self.coordinator.approve_task(task["id"], "reviewed")
        self.coordinator.merge_task(task["id"])

        self.assertEqual(self._decisions(project["id"]), [])
        items = self._finalizations(project["id"])
        self.assertEqual([item["task_id"] for item in items], [task["id"]])
        text = items[0]["text"]
        # It has to say what is retained and the one safe command that sheds it.
        self.assertIn("its task worktree", text)
        self.assertIn(f"helm/{project['id']}/{task['id']}", text)
        self.assertIn(f"helm task cleanup {task['id']}", text)
        self.assertLessEqual(len(text), Coordinator.SITUATION_LINE_LIMIT)

        # Derived, so recomputing it never produces a second copy.
        for _ in range(3):
            self.coordinator.refresh_finalization_decisions(project["id"])
        self.assertEqual(len(self._finalizations(project["id"])), 1)

        # And it is a gate, so `helm watch` keeps showing it rather than
        # surfacing it once and going quiet.
        self.assertTrue(
            any("Cleanup decision" in u["text"] for u in self.coordinator.project_updates_for_watch())
        )
        self.assertTrue(
            any("Cleanup decision" in u["text"] for u in self.coordinator.project_updates_for_watch())
        )

        # Cleanup is what answers it, and answering is idempotent.
        self.coordinator.cleanup_task(task["id"])
        self.assertEqual(self._finalizations(project["id"]), [])
        self.coordinator.refresh_finalization_decisions(project["id"])
        self.assertEqual(self._finalizations(project["id"]), [])

    def test_retained_state_is_read_from_the_record_not_probed_from_disk(self) -> None:
        """Status and watch must not scan git or the filesystem for this.

        A probe per delivered task is a cost on every refresh, and every way
        of failing to see a resource -- a moved root, an unreadable checkout,
        a git that errors -- looks exactly like the resource being gone, which
        would close this gate on work that is still there.
        """
        root, project, task = self._completed_task_awaiting_approval("noprobe")
        self.coordinator.approve_task(task["id"], "reviewed")
        self.coordinator.merge_task(task["id"])
        self.assertEqual(len(self._finalizations(project["id"])), 1)

        # No git anywhere in the refresh the attention surfaces run.
        with mock.patch("helm.core._git", side_effect=AssertionError("probed git")):
            items = self.coordinator.project_status(project["id"])["action_items"]
            self.coordinator.project_updates_for_watch(project["id"])
        self.assertEqual(
            [i["kind"] for i in items if i["kind"] == FINALIZATION_ACTION_KIND],
            [FINALIZATION_ACTION_KIND],
        )
        # And no existence check either: the record alone answers this. (The
        # store's own file reads are outside the scope being asserted, so the
        # detection is exercised directly rather than through them.)
        data = self.coordinator.store.load()
        with mock.patch.object(Path, "exists", side_effect=AssertionError("probed disk")), \
                mock.patch.object(Path, "is_dir", side_effect=AssertionError("probed disk")):
            retained = self.coordinator.task_retained_resources(
                data["tasks"][task["id"]], data
            )
        self.assertIn("its task worktree", retained)

    def test_resources_gone_outside_helm_hold_the_gate_until_cleanup(self) -> None:
        """Being wrong costs one line; the other direction loses the work."""
        root, project, task = self._completed_task_awaiting_approval("movedroot")
        self.coordinator.approve_task(task["id"], "reviewed")
        merged = self.coordinator.merge_task(task["id"])
        self.assertEqual(len(self._finalizations(project["id"])), 1)

        # The project root moves and the worktree is removed by hand. Neither
        # is Helm recording that it let go, so the gate stands.
        shutil.rmtree(Path(merged["workspace"]), ignore_errors=True)
        with self.coordinator.store.locked() as data:
            data["projects"][project["id"]]["root"] = str(root / "gone-elsewhere")
        self.assertEqual(len(self._finalizations(project["id"])), 1)

        # Cleanup is the reconciliation point: it marks the absent worktree
        # removed even though the directory was taken from under it.
        with self.coordinator.store.locked() as data:
            data["projects"][project["id"]]["root"] = str(root)
        self.coordinator.cleanup_task(task["id"])
        self.assertEqual(self._finalizations(project["id"]), [])

    def test_a_changed_retained_set_keeps_the_item_and_stamps_it(self) -> None:
        """Same decision, said more accurately -- not a new one to re-read."""
        root, project, task = self._completed_task_awaiting_approval("restamp")
        self.coordinator.approve_task(task["id"], "reviewed")
        self.coordinator.merge_task(task["id"])
        first = self._finalizations(project["id"])[0]
        self.assertIn("its task worktree", first["text"])
        self.assertIsNone(first.get("updated_at"))

        # One piece is shed; the rest are still held.
        with self.coordinator.store.locked() as data:
            data["tasks"][task["id"]]["workspace_removed"] = True
        second = self._finalizations(project["id"])[0]

        self.assertEqual(second["id"], first["id"])
        self.assertEqual(second["at"], first["at"])
        self.assertNotIn("its task worktree", second["text"])
        self.assertTrue(second["updated_at"])
        self.assertGreaterEqual(second["updated_at"], first["at"])

    def test_a_ticketed_task_branch_is_counted_and_shed_like_any_other(self) -> None:
        """The ticket is in the branch name, and both sides must know it.

        `helm/<project>/<ticket>-<task>` matched neither the cleanup
        predicate nor the retained-resource check, so a ticketed task's branch
        survived cleanup forever and nothing ever said it was still there.
        """
        root = self.repo("ticketfinal")
        project = self.coordinator.register_project(
            "TicketFinal", str(root), project_id="ticketfinal"
        )
        code = (
            "from pathlib import Path; import subprocess; "
            "Path('change.txt').write_text('worker'); "
            "subprocess.run(['git','add','change.txt'],check=True); "
            "subprocess.run(['git','commit','-m','worker change'],check=True)"
        )
        task = self.coordinator.create_task(
            project["id"], "commit a change", ticket="TICKET-192"
        )
        self.assertEqual(task["branch"], f"helm/{project['id']}/TICKET-192-{task['id']}")
        self.assertTrue(task_owns_branch(task))
        self.coordinator.launch_worker(task["id"], [sys.executable, "-c", code])
        self.coordinator.approve_task(task["id"], "reviewed")
        self.coordinator.merge_task(task["id"])

        items = self._finalizations(project["id"])
        self.assertEqual(len(items), 1)
        self.assertIn(task["branch"], items[0]["text"])

        self.coordinator.cleanup_task(task["id"])
        # Merged, so plain cleanup sheds it: the ref is gone and so is the gate.
        self.assertNotEqual(
            subprocess.run(
                ["git", "show-ref", "--verify", f"refs/heads/{task['branch']}"],
                cwd=root, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            ).returncode,
            0,
        )
        self.assertEqual(self._finalizations(project["id"]), [])

    def test_a_branch_helm_did_not_name_is_never_this_tasks_to_delete(self) -> None:
        """The predicate widens nothing: it admits one exact name."""
        base = {"id": "t-1", "project_id": "p", "ticket": None, "branch": "helm/p/t-1"}
        self.assertTrue(task_owns_branch(base))
        self.assertTrue(
            task_owns_branch({**base, "ticket": "TICKET-9", "branch": "helm/p/TICKET-9-t-1"})
        )
        for branch in (
            "main",
            "helm/p/t-2",
            "helm/other/t-1",
            "helm/p/TICKET-9-t-1",     # ticket in the name, none on the record
            "helm/p/t-1-extra",
            "",
            None,
        ):
            self.assertFalse(task_owns_branch({**base, "branch": branch}), branch)
        # And a ticket on the record with the unticketed name is not it either.
        self.assertFalse(task_owns_branch({**base, "ticket": "TICKET-9"}))

    def test_nothing_retained_and_undelivered_work_raise_no_cleanup_gate(self) -> None:
        """A gate that fires on healthy work is what teaches a reader to skip."""
        root = self.repo("noresidue")
        project = self.coordinator.register_project(
            "NoResidue", str(root), project_id="noresidue"
        )
        # Undelivered work: the delivery decision covers it, not this.
        task = self.coordinator.create_task(project["id"], "still going")
        worker = self.coordinator.launch_worker(
            task["id"], [sys.executable, "-c", ""], wait=False
        )
        self.coordinator.record_worker_message(worker["id"], "result", "done")
        self.assertEqual(self._finalizations(project["id"]), [])
        self.assertEqual(len(self._decisions(project["id"])), 1)

        # Blocked and approval-needed work is likewise not this gate's business.
        for state in ("blocked", "failed", "approval-needed", "pr-open"):
            with self.coordinator.store.locked() as data:
                data["tasks"][task["id"]]["status"] = state
            self.assertEqual(self._finalizations(project["id"]), [])

    def test_a_kept_branch_leaves_the_cleanup_gate_open_and_says_so(self) -> None:
        """Cleanup answers this gate only for what it actually shed."""
        root = self.repo("prfinalize")
        project = self.coordinator.register_project(
            "PRFinalize", str(root), project_id="prfinalize", delivery_policy="pr"
        )
        code = (
            "from pathlib import Path; import subprocess; "
            "Path('change.txt').write_text('worker'); "
            "subprocess.run(['git','add','change.txt'],check=True); "
            "subprocess.run(['git','commit','-m','worker change'],check=True)"
        )
        task = self.coordinator.create_task(project["id"], "make a PR change")
        self.coordinator.launch_worker(task["id"], [sys.executable, "-c", code])
        self.coordinator.record_pr_status(
            task["id"], state="merged", url="https://example.invalid/pull/1"
        )
        self.assertEqual(len(self._finalizations(project["id"])), 1)

        # The remote merged it; this base worktree has not, so the branch is
        # unmerged locally and cleanup keeps it -- and says which piece is left.
        self.coordinator.cleanup_task(task["id"])
        remaining = self._finalizations(project["id"])
        self.assertEqual(len(remaining), 1)
        self.assertNotIn("its task worktree", remaining[0]["text"])
        self.assertIn(f"helm/{project['id']}/{task['id']}", remaining[0]["text"])

        self.coordinator.cleanup_task(task["id"], delete_branch=True)
        self.assertEqual(self._finalizations(project["id"]), [])

    def test_a_dirty_delivered_workspace_keeps_its_gate_and_its_work(self) -> None:
        """The gate never weakens a cleanup refusal; it reports the standoff."""
        root, project, task = self._completed_task_awaiting_approval("dirtyfinal")
        self.coordinator.approve_task(task["id"], "reviewed")
        merged = self.coordinator.merge_task(task["id"])
        (Path(merged["workspace"]) / "scratch.txt").write_text("uncommitted")

        with self.assertRaises(SafetyError):
            self.coordinator.cleanup_task(task["id"])
        items = self._finalizations(project["id"])
        self.assertEqual(len(items), 1)
        self.assertIn("its task worktree", items[0]["text"])
        self.assertTrue(Path(merged["workspace"]).exists())

    def test_resolution_never_closes_somebody_elses_follow_up(self) -> None:
        """Helm knows when a delivery decision was taken. It cannot know that."""
        root, project, task = self._completed_task_awaiting_approval("followupkept")
        self.coordinator.record_delivery_decision(project["id"], task_id=task["id"])
        self.coordinator.record_project_action_item(
            project["id"],
            "rotation watcher follow-up needed before the next release",
            source="Review loop",
            task_id=task["id"],
        )

        self.coordinator.approve_task(task["id"], "reviewed")
        self.coordinator.merge_task(task["id"])

        remaining = self.coordinator.project_status(project["id"])["action_items"]
        # The merged task still holds its worktree, so its own cleanup gate is
        # open alongside; what must survive is the note Helm cannot judge.
        self.assertNotIn(DELIVERY_DECISION_KIND, [item["kind"] for item in remaining])
        self.assertIn(FOLLOW_UP_ACTION_KIND, [item["kind"] for item in remaining])


class ApprovalAfterAFailedRoundTests(HelmTestCase):
    """A task reviewed over many rounds must stay approvable when an early
    round's worker died and a later one finished the work."""

    def test_a_failed_earlier_round_does_not_block_approving_the_latest(self) -> None:
        root, project, task = self._completed_task_awaiting_approval("manyrounds")
        data = self.coordinator.store.load()
        workers = [w for w in data["workers"].values() if w.get("task_id") == task["id"]]
        self.assertEqual(len(workers), 1)
        finished = workers[0]
        # An EARLIER round that died, recorded before the one that succeeded.
        data["workers"]["w-deadround"] = {
            **finished,
            "id": "w-deadround",
            "status": "failed",
            "started_at": "2000-01-01T00:00:00Z",
        }
        self.coordinator.store.save(data)

        approved = self.coordinator.approve_task(task["id"], "reviewed")

        self.assertEqual(approved["status"], "approved")

    def test_a_failed_latest_round_still_refuses(self) -> None:
        root, project, task = self._completed_task_awaiting_approval("lastroundfailed")
        data = self.coordinator.store.load()
        workers = [w for w in data["workers"].values() if w.get("task_id") == task["id"]]
        data["workers"]["w-lastround"] = {
            **workers[0],
            "id": "w-lastround",
            "status": "failed",
            "started_at": "2099-01-01T00:00:00Z",
        }
        self.coordinator.store.save(data)

        with self.assertRaisesRegex(SafetyError, "w-lastround"):
            self.coordinator.approve_task(task["id"], "reviewed")


class ReplacedForemanTabIsReleasedTests(HelmTestCase):
    """A stopped foreman's pane is evidence until somebody acts on it.

    A foreman that failed or blocked keeps its tab because a human has to
    diagnose why it stopped. But once the project has a NEWER foreman running,
    that diagnosis has happened: somebody looked, decided the driver was gone,
    and started another. Without an expiry these panes accumulate one per
    replacement and never leave -- and a retained `foreman (blocked)` tab looks
    exactly like a live driver in the panel, which is how one project comes to
    look as though it has several. Six of them had built up before this was
    noticed.
    """

    def _stopped_foreman(self, adapter, project):
        task = self.coordinator.create_foreman_task(project["id"])
        worker = adapter.launch_task(task["id"], [sys.executable, "-c", ""], wait=False)
        self.coordinator.record_worker_message(worker["id"], "blocker", "the checkout is dirty")
        # A foreman's blocker PAUSES it -- the session stays live and
        # addressable, and Helm never closes a live pane. The panes that
        # actually accumulated were stopped foremen: replaced because they were
        # wedged or blocked, their worker settled, their tab left behind.
        self.coordinator.stop_worker(worker["id"], "replaced")
        return task, worker

    def test_a_replaced_foreman_stops_holding_its_tab(self) -> None:
        root = self.repo("replaced-foreman")
        project = self.coordinator.register_project(
            "Replaced", str(root), project_id="replaced-foreman"
        )
        herdr = FakeHerdr()
        adapter = HerdrAdapter(self.coordinator, herdr)
        _, worker = self._stopped_foreman(adapter, project)

        # Alone, it is still the thing a human has to look at.
        self.assertEqual(adapter.release_finished_tabs(), [])

        # A newer foreman for the same project is the evidence that somebody
        # looked, decided, and acted. The old pane now holds a question nobody
        # is still asking.
        self.coordinator.create_foreman_task(project["id"])
        self.assertEqual(adapter.release_finished_tabs(), [worker["id"]])

    def test_a_blocked_foreman_with_no_successor_keeps_its_tab(self) -> None:
        # The half that must not break: only a NEWER foreman supersedes. A
        # project whose only driver is blocked still needs that pane, and
        # quieting it would hide the very fault the retention exists for.
        root = self.repo("lone-foreman")
        project = self.coordinator.register_project(
            "Lone", str(root), project_id="lone-foreman"
        )
        herdr = FakeHerdr()
        adapter = HerdrAdapter(self.coordinator, herdr)
        self._stopped_foreman(adapter, project)
        self.assertEqual(adapter.release_finished_tabs(), [])
        self.assertEqual(herdr.closed_tabs, [])


class ADirtyParentRepoDoesNotMakeAWorkspaceDirtyTests(HelmTestCase):
    """`git status` ascends when its directory is not a repository.

    A worktreeless role's directory lives under the Helm state tree and has no
    boundary of its own, so "is this workspace dirty" was answered by the HELM
    repository. An empty reviewer directory read as dirty because Helm's own
    checkout had one untracked file, and cleanup refused for three days.

    The direction of the wrong answer is incidental: the same read would have
    called a directory CLEAN because the parent happened to be.
    """

    def _dirty_repo_with_a_bare_subdir(self):
        import subprocess
        root = Path(self.temp.name) / "parent-repo"
        (root / "state" / "reviewers" / "t-x").mkdir(parents=True)
        def g(*a):
            subprocess.run(["git", "-C", str(root), *a], check=True,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        g("init", "-q", "-b", "main")
        g("config", "user.email", "t@example.invalid")
        g("config", "user.name", "T")
        (root / "tracked.txt").write_text("x", encoding="utf-8")
        g("add", "tracked.txt")
        g("commit", "-q", "-m", "seed")
        # The parent is now dirty, exactly as the Helm root was with uv.lock.
        (root / "untracked.txt").write_text("y", encoding="utf-8")
        return root, root / "state" / "reviewers" / "t-x"

    def test_an_empty_directory_under_a_dirty_repo_is_clean(self) -> None:
        root, workspace = self._dirty_repo_with_a_bare_subdir()
        # Sanity: git really does ascend from there to the dirty parent.
        import subprocess
        top = subprocess.run(["git", "-C", str(workspace), "rev-parse", "--show-toplevel"],
                             capture_output=True, text=True).stdout.strip()
        self.assertEqual(Path(top).resolve(), root.resolve(),
                         "precondition: git must ascend, or this test proves nothing")
        self.assertTrue(
            self.coordinator._workspace_clean(workspace),
            "an empty directory was called dirty because its PARENT repo was",
        )

    def test_a_directory_holding_files_is_still_not_clean(self) -> None:
        """Fail-closed: the fix must not turn every non-repo path into 'clean'."""
        _root, workspace = self._dirty_repo_with_a_bare_subdir()
        (workspace / "left-behind.txt").write_text("z", encoding="utf-8")
        self.assertFalse(self.coordinator._workspace_clean(workspace))


class HelmsOwnOutputDoesNotBlockCleanupTests(HelmTestCase):
    """The read-only lane writes its deliverable into `.helm-out/`.

    That directory is Helm's, not the worker's, and the clean check could not
    tell the difference: four finished tasks were held open by exactly one
    untracked file under it. No operator action cleared them either, because
    deleting the file by hand is the one move that discards the report.
    """

    def _repo_with(self, *relative_paths):
        import subprocess
        root = Path(self.temp.name) / "worktree"
        root.mkdir(parents=True)
        def g(*a):
            subprocess.run(["git", "-C", str(root), *a], check=True,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        g("init", "-q", "-b", "main")
        g("config", "user.email", "t@example.invalid")
        g("config", "user.name", "T")
        (root / "tracked.txt").write_text("x", encoding="utf-8")
        g("add", "tracked.txt")
        g("commit", "-q", "-m", "seed")
        for rel in relative_paths:
            target = root / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text("y", encoding="utf-8")
        return root

    def test_an_untracked_report_under_helm_out_is_clean(self) -> None:
        workspace = self._repo_with(".helm-out/layer-2-design.md")
        self.assertTrue(
            self.coordinator._workspace_clean(workspace),
            "Helm's own output directory blocked cleanup of a finished task",
        )

    def test_a_whole_tree_of_helm_output_is_still_clean(self) -> None:
        workspace = self._repo_with(
            ".helm-out/findings.md", ".helm-out/nested/contact-sheet.txt",
        )
        self.assertTrue(self.coordinator._workspace_clean(workspace))

    def test_real_work_beside_helm_output_is_still_dirty(self) -> None:
        """Fail-closed: the exemption is one directory, not a general amnesty."""
        workspace = self._repo_with(".helm-out/findings.md", "src/feature.py")
        self.assertFalse(
            self.coordinator._workspace_clean(workspace),
            "uncommitted work was excused because .helm-out sat beside it",
        )

    def test_a_modified_tracked_file_is_still_dirty(self) -> None:
        workspace = self._repo_with(".helm-out/findings.md")
        (workspace / "tracked.txt").write_text("changed", encoding="utf-8")
        self.assertFalse(self.coordinator._workspace_clean(workspace))

    def test_a_path_merely_starting_with_the_name_is_still_dirty(self) -> None:
        """`.helm-output/` is not `.helm-out/`, and prefix matching would take it."""
        workspace = self._repo_with(".helm-outside/notes.md")
        self.assertFalse(self.coordinator._workspace_clean(workspace))
