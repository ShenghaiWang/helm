"""Approval holds, standing grants, the authority boundary and safety rules."""

from __future__ import annotations

import contextlib
import io
import os
import json
import re
import signal
import subprocess
import sys
from pathlib import Path
from unittest import mock

from helm import cli
from helm.core import (
    CORE_SAFETY_RULES,
    PROTECTED_ACTIONS,
    Coordinator,
    HelmError,
    SafetyError,
    StateStore,
    inside,
    worker_environment,
)
from helm.herdr import HerdrAdapter

from tests.support import FakeHerdr, HelmTestCase, REPO_ROOT, SHIPPED_DOMAINS


class ApprovalTests(HelmTestCase):
    def _paused_on_approval(
        self,
        name: str,
        action: str = "publish",
        *,
        execution: str = "external",
        artifact: str = "",
    ) -> tuple[dict, dict, dict]:
        """A live worker paused on a protected action, mid-task."""
        root = self.repo(name)
        project = self.coordinator.register_project(name, str(root), project_id=name)
        task = self.coordinator.create_task(project["id"], "produce and publish it")
        worker = self.coordinator.prepare_external_worker(
            task["id"], [sys.executable, "-c", ""], execution=execution
        )
        self.commit_on_task_branch(task, "the thing to publish")
        if artifact:
            (Path(task["workspace"]) / artifact).write_bytes(b"approved bytes")
            self.coordinator.record_worker_message(
                worker["id"], "artifact", "render", payload={"path": artifact}
            )
        self.coordinator.record_worker_message(
            worker["id"],
            "approval-needed",
            f"ready to {action} the rendered file",
            payload={"action": action},
        )
        return project, task, worker

    def _hold(self, task_id: str) -> dict:
        record = self.coordinator.inspect_task(task_id)["task"]
        return self.coordinator.latest_hold(record) or {}

    def _stage(self, workspace: Path, name: str, text: str) -> None:
        (workspace / name).write_text(text, encoding="utf-8")
        subprocess.run(["git", "-C", str(workspace), "add", name], check=True)

    def _write_delivered(self, workspace: Path, text: str) -> None:
        out = workspace / "out"
        out.mkdir(exist_ok=True)
        (out / "render.bin").write_text(text, encoding="utf-8")

    def test_core_safety_rules_require_delegation_and_project_isolation(self) -> None:
        rules = CORE_SAFETY_RULES.lower()
        self.assertIn("delegated worker", rules)
        self.assertIn("do not delegate it onward", rules)
        self.assertIn("keep this project's knowledge isolated", rules)
        self.assertIn("never import another project's", rules)

    def test_deleting_inside_the_assigned_worktree_is_work_not_a_protected_action(
        self,
    ) -> None:
        """Unqualified "do not delete" stalls ordinary file edits.

        A worker replacing a file, or clearing a temporary one it made for this
        task, read the protected list and asked -- which is a silent stall,
        because nobody reads a worker's session. The boundary is *where* the
        deletion reaches, not the word.
        """
        root = self.repo("deletescope")
        project = self.coordinator.register_project(
            "Delete scope", str(root), project_id="deletescope"
        )
        task = self.coordinator.create_task(project["id"], "replace a module")
        worker = self.coordinator.prepare_external_worker(
            task["id"], [sys.executable, "-c", ""]
        )
        composed = json.loads(Path(worker["context_file"]).read_text(encoding="utf-8"))
        rules = self._flat(composed["safety_rules"]["content"])
        self.assertEqual(composed["safety_rules"]["content"], CORE_SAFETY_RULES)

        # In scope: the work itself, stated so a worker does not ask.
        self.assertIn("removing files inside your assigned worktree", rules)
        self.assertIn("is ordinary implementation work", rules)
        self.assertIn("a temporary file you made for this task", rules)
        # Out of scope: unchanged, and still explicitly a human's.
        self.assertIn("Protected deletion is deletion that reaches outside", rules)
        for external in (
            "an external or remote resource",
            "a worktree",
            "coordinator or user state",
            "another project",
        ):
            self.assertIn(external, rules, external)
        self.assertIn("Those still require a human", rules)
        # The protected set itself is untouched by any of this wording.
        self.assertIn("delete", PROTECTED_ACTIONS)

    def test_agents_md_states_the_same_deletion_boundary_as_the_safety_rules(
        self,
    ) -> None:
        """Two documents, one boundary: drift here is how a rule stops meaning one thing."""
        agents = self._flat((REPO_ROOT / "AGENTS.md").read_text(encoding="utf-8"))
        self.assertIn("removing or renaming a file *inside* that worktree", agents)
        self.assertIn(
            "Protected deletion means deletion reaching outside the assigned task", agents
        )
        # The escalation list keeps deletion on it.
        self.assertIn("merge, push, publish, delete", agents)

    def test_local_merge_requires_approval_and_fast_forwards(self) -> None:
        root = self.repo("merge")
        project = self.coordinator.register_project("Merge", str(root), project_id="merge")
        task = self.coordinator.create_task(project["id"], "commit a change")
        code = (
            "from pathlib import Path; import subprocess; "
            "Path('change.txt').write_text('worker'); "
            "subprocess.run(['git','add','change.txt'],check=True); "
            "subprocess.run(['git','commit','-m','worker change'],check=True)"
        )
        worker = self.coordinator.launch_worker(task["id"], [sys.executable, "-c", code])
        self.assertEqual(worker["status"], "completed")
        with self.assertRaises(SafetyError):
            self.coordinator.merge_task(task["id"])
        self.coordinator.approve_task(task["id"], "reviewed")
        merged = self.coordinator.merge_task(task["id"])
        self.assertEqual(merged["status"], "merged")
        self.assertEqual((root / "change.txt").read_text(), "worker")

    def test_a_standing_grant_approves_in_advance_and_records_its_authority(self) -> None:
        root, project, task = self._completed_task_awaiting_approval("granted")
        grant = self.coordinator.grant_approval(
            "merge", project_id=project["id"], note="routine task-branch merges for this project"
        )
        self.assertEqual(
            self.coordinator.approval_grant_for("merge", project["id"])["id"], grant["id"]
        )
        approved = self.coordinator.approve_task(task["id"], "", grant_id=grant["id"])
        # The grant is the recorded authority, and the binding to an immutable
        # revision is exactly the same as a person approving in the moment.
        self.assertEqual(approved["approval"]["grant_id"], grant["id"])
        self.assertTrue(approved["approval"]["tree"])
        merged = self.coordinator.merge_task(task["id"])
        self.assertEqual(merged["status"], "merged")
        self.assertEqual((root / "change.txt").read_text(), "worker")

    def test_a_grant_never_widens_beyond_the_scope_a_human_wrote(self) -> None:
        _, project, task = self._completed_task_awaiting_approval("scoped")
        other = self.repo("elsewhere")
        self.coordinator.register_project("Elsewhere", str(other), project_id="elsewhere")
        # Scoped to a different project: says nothing about this one.
        elsewhere = self.coordinator.grant_approval(
            "merge", project_id="elsewhere", note="only that project"
        )
        self.assertIsNone(self.coordinator.approval_grant_for("merge", project["id"]))
        with self.assertRaisesRegex(SafetyError, r"scoped to project elsewhere"):
            self.coordinator.approve_task(task["id"], grant_id=elsewhere["id"])
        # Granting one action never grants another.
        publish = self.coordinator.grant_approval("publish", note="channel uploads are fine")
        self.assertIsNone(self.coordinator.approval_grant_for("merge", project["id"]))
        with self.assertRaisesRegex(SafetyError, r"covers publish, not merge"):
            self.coordinator.approve_task(task["id"], grant_id=publish["id"])
        # An invented grant id is an error, not an absent grant quietly
        # falling back to approving anyway.
        with self.assertRaisesRegex(HelmError, r"unknown approval grant"):
            self.coordinator.approve_task(task["id"], grant_id="g-invented")
        # A grant must say why it exists, and cannot name an unknown action.
        with self.assertRaisesRegex(HelmError, r"requires --note"):
            self.coordinator.grant_approval("merge", note="   ")
        with self.assertRaisesRegex(HelmError, r"protected action must be one of"):
            self.coordinator.grant_approval("anything", note="everything")

    def test_revoking_a_grant_stops_it_approving_anything_further(self) -> None:
        _, project, task = self._completed_task_awaiting_approval("revoked")
        grant = self.coordinator.grant_approval("merge", note="temporary while I am away")
        self.coordinator.revoke_approval_grant(grant["id"], "back now")
        self.assertIsNone(self.coordinator.approval_grant_for("merge", project["id"]))
        self.assertEqual(self.coordinator.list_approval_grants(), [])
        # Revocation is the point of a standing grant being revocable: a
        # withdrawn one must not still approve.
        with self.assertRaisesRegex(SafetyError, r"was revoked"):
            self.coordinator.approve_task(task["id"], grant_id=grant["id"])
        # It stays visible as provenance for what was once permitted.
        history = self.coordinator.list_approval_grants(include_revoked=True)
        self.assertEqual([entry["id"] for entry in history], [grant["id"]])
        self.assertEqual(history[0]["revoked_note"], "back now")

    def test_a_worker_and_a_project_file_can_never_create_a_grant(self) -> None:
        root = self.repo("no-self-grant")
        settings = root / ".helm"
        settings.mkdir()
        # A project file is guidance; it has no path to authority.
        (settings / "project.json").write_text(
            json.dumps({"approval_grants": [{"action": "merge"}], "approvals": "all"})
        )
        project = self.coordinator.register_project("NoSelf", str(root), project_id="no-self-grant")
        task = self.coordinator.create_task(project["id"], "try to self-approve")
        worker = self.coordinator.launch_worker(
            task["id"], [sys.executable, "-c", ""], wait=False
        )
        # The strongest thing a worker can say about approval is still only a
        # message. It cannot ask for a merge at all, and a request it may make
        # pauses the task and stops there.
        with self.assertRaisesRegex(HelmError, r"merging is Helm's own operation"):
            self.coordinator.record_worker_message(
                worker["id"], "approval-needed", "please grant merge for everything",
                payload={"action": "merge"},
            )
        self.coordinator.record_worker_message(
            worker["id"], "approval-needed", "publish the build",
            payload={"action": "publish"},
        )
        self.assertEqual(
            self.coordinator.inspect_task(task["id"])["task"]["status"], "approval-needed"
        )
        self.assertEqual(self.coordinator.list_approval_grants(), [])
        self.assertIsNone(self.coordinator.approval_grant_for("merge", project["id"]))

    def test_an_approval_pause_keeps_its_worker_live_and_finishes_after_release(self) -> None:
        """The whole loop: pause, decision, delivery, pre-action gate, outcome."""
        project, task, worker = self._paused_on_approval("paused")
        hold = self._hold(task["id"])
        self.assertEqual(hold["status"], "waiting")
        self.assertEqual(hold["action"], "publish")
        self.assertEqual(hold["worker_id"], worker["id"])
        # A pause is not a failure. The session is sitting there waiting.
        live = self.coordinator.store.load()["workers"][worker["id"]]
        self.assertEqual(live["status"], "running")
        self.assertIsNone(live["ended_at"])
        health = {e["worker_id"]: e for e in self.coordinator.worker_health()}
        self.assertEqual(health[worker["id"]]["verdict"], "awaiting-approval")
        # And it stays addressable, exactly as it was before it asked.
        self.coordinator.record_worker_message(worker["id"], "answer", "looking at it")
        still = self.coordinator.record_worker_message(
            worker["id"], "status", "waiting on the commander"
        )
        self.assertEqual(still["status"], "approval-needed")

        released = self.coordinator.release_task_hold(
            task["id"], action="publish", note="channel upload agreed", confirm=True
        )
        # A decision is not a delivery: the task stays paused until the session
        # itself spends the authorization.
        self.assertEqual(released["status"], "approval-needed")
        self.assertEqual(released["hold"]["status"], "authorized-pending-delivery")
        authorization = released["hold"]["authorization"]
        self.assertTrue(authorization["ticket"])
        self.assertIsNone(authorization["ticket_consumed_at"])
        self.assertEqual(authorization["snapshot"]["branch"], task["branch"])

        started = self.coordinator.start_authorized_action(worker["id"])
        self.assertEqual(started["action"], "publish")
        self.assertEqual(started["status"], "in-flight")
        self.assertEqual(
            self.coordinator.inspect_task(task["id"])["task"]["status"], "running"
        )
        # One use only.
        with self.assertRaisesRegex(SafetyError, r"already been used"):
            self.coordinator.start_authorized_action(worker["id"])

        finished = self.coordinator.record_worker_message(
            worker["id"], "result", "published it",
            payload={"receipt": "remote-object-1"},
        )
        self.assertEqual(finished["status"], "completed")
        closed = self._hold(task["id"])
        self.assertEqual(closed["status"], "closed")
        self.assertEqual(closed["outcome"]["receipts"], ["remote-object-1"])
        self.assertEqual(
            self.coordinator.store.load()["workers"][worker["id"]]["status"], "completed"
        )
        situation = " ".join(
            entry["text"]
            for entry in self.coordinator.project_status(project["id"])["situation"]
        )
        self.assertIn("authorized publish", situation)
        self.assertIn("published it", situation)

    def test_changed_content_breaks_the_binding_even_when_git_status_does_not(self) -> None:
        """DEFECT 1: the binding hashed status text, so bytes could change freely.

        Rewriting an already-untracked file leaves `git status --porcelain`
        byte-identical. The probe published different bytes than the ones the
        commander approved and the hold closed as valid.
        """
        project, task, worker = self._paused_on_approval("bytes", artifact="render.bin")
        self.coordinator.release_task_hold(task["id"], action="publish", confirm=True)
        approved = self._hold(task["id"])["authorization"]["snapshot"]
        self.assertTrue(approved["artifacts"])
        self.assertTrue(approved["untracked"])

        artifact = Path(task["workspace"]) / "render.bin"
        before = subprocess.run(
            ["git", "-C", str(task["workspace"]), "status", "--porcelain=v1",
             "--untracked-files=all"],
            check=True, text=True, capture_output=True,
        ).stdout
        artifact.write_bytes(b"different unapproved bytes!!")
        after = subprocess.run(
            ["git", "-C", str(task["workspace"]), "status", "--porcelain=v1",
             "--untracked-files=all"],
            check=True, text=True, capture_output=True,
        ).stdout
        # The old signal genuinely cannot see this change.
        self.assertEqual(before, after)

        # The pre-action gate can, and it refuses before anything is published.
        with self.assertRaisesRegex(SafetyError, r"do not act"):
            self.coordinator.start_authorized_action(worker["id"])
        stopped = self._hold(task["id"])
        self.assertEqual(stopped["status"], "invalidated")
        self.assertIsNone(stopped["authorization"]["ticket_consumed_at"])
        self.assertEqual(
            self.coordinator.inspect_task(task["id"])["task"]["status"], "approval-needed"
        )

    def test_every_kind_of_content_change_is_bound(self) -> None:
        """Dirty-to-dirty, staged-to-staged, ignored delivery output, artifact set."""
        cases = {
            "dirty": lambda w: (w / "change.txt").write_text("edited", encoding="utf-8"),
            "staged": lambda w: self._stage(w, "staged.txt", "one"),
            "ignored": lambda w: self._write_delivered(w, "second"),
        }
        for name, mutate in cases.items():
            with self.subTest(change=name):
                project, task, worker = self._paused_on_approval(f"bound-{name}")
                workspace = Path(task["workspace"])
                if name == "staged":
                    self._stage(workspace, "staged.txt", "zero")
                if name == "ignored":
                    (workspace / ".gitignore").write_text("out/\n", encoding="utf-8")
                    subprocess.run(["git", "-C", str(workspace), "add", ".gitignore"], check=True)
                    subprocess.run(
                        ["git", "-C", str(workspace), "commit", "-qm", "ignore out"], check=True
                    )
                    self._write_delivered(workspace, "first")
                    (workspace / ".helm").mkdir(exist_ok=True)
                    (workspace / ".helm" / "project.json").write_text(
                        json.dumps({"deliver": ["out"]}), encoding="utf-8"
                    )
                    settings = Path(project["root"]) / ".helm"
                    settings.mkdir(exist_ok=True)
                    (settings / "project.json").write_text(
                        json.dumps({"deliver": ["out"]}), encoding="utf-8"
                    )
                # Re-request so the snapshot covers the pre-mutation state.
                self.coordinator.record_worker_message(
                    worker["id"], "approval-needed", "ready", payload={"action": "publish"}
                )
                self.coordinator.release_task_hold(
                    task["id"], action="publish", confirm=True
                )
                mutate(workspace)
                with self.assertRaisesRegex(SafetyError, r"do not act"):
                    self.coordinator.start_authorized_action(worker["id"])

    def test_a_change_between_request_and_release_is_never_silently_rebound(self) -> None:
        """DEFECT 2: release built a fresh binding and authorized a newer revision."""
        project, task, worker = self._paused_on_approval("rebind")
        requested = self._hold(task["id"])["snapshot"]["revision"]
        self.commit_on_task_branch(task, "changed while the commander was deciding")

        with self.assertRaisesRegex(SafetyError, r"changed after it asked"):
            self.coordinator.release_task_hold(
                task["id"], action="publish", confirm=True
            )
        hold = self._hold(task["id"])
        # Nothing authorized, and the stale request is not left waiting either.
        self.assertEqual(hold["status"], "abandoned")
        self.assertIsNone(hold["authorization"])
        self.assertEqual(hold["snapshot"]["revision"], requested)
        kinds = [m["kind"] for m in self.coordinator.inspect_task(task["id"])["messages"]]
        self.assertIn("approval-invalidated", kinds)
        # And the worker can ask again for the state that now exists.
        self.coordinator.record_worker_message(
            worker["id"], "approval-needed", "ready now", payload={"action": "publish"}
        )
        self.assertNotEqual(self._hold(task["id"])["snapshot"]["revision"], requested)

    def test_an_approval_request_must_name_one_exact_action(self) -> None:
        """DEFECT 3: an unspecified request let the commander authorize anything."""
        root = self.repo("exact")
        project = self.coordinator.register_project("Exact", str(root), project_id="exact")
        task = self.coordinator.create_task(project["id"], "ask for something")
        worker = self.coordinator.prepare_external_worker(
            task["id"], [sys.executable, "-c", ""]
        )
        with self.assertRaisesRegex(HelmError, r"must name the exact protected action"):
            self.coordinator.record_worker_message(
                worker["id"], "approval-needed", "unspecified protected request"
            )
        with self.assertRaisesRegex(HelmError, r"must name the exact protected action"):
            self.coordinator.record_worker_message(
                worker["id"], "approval-needed", "vague", payload={"action": "anything"}
            )
        # No hold, so nothing is releasable in the first place.
        self.assertEqual(
            self.coordinator.inspect_task(task["id"])["task"]["status"], "running"
        )
        with self.assertRaisesRegex(HelmError, r"not waiting on an approval"):
            self.coordinator.release_task_hold(
                task["id"], action="delete", confirm=True
            )
        # `merge` is refused where it is asked for, not left as a dead hold.
        with self.assertRaisesRegex(HelmError, r"merging is Helm's own operation"):
            self.coordinator.record_worker_message(
                worker["id"], "approval-needed", "merge it", payload={"action": "merge"}
            )
        self.assertIsNone(
            self.coordinator.task_hold(self.coordinator.inspect_task(task["id"])["task"])
        )
        # The status route cannot open a hold either: it can never name an action.
        with self.assertRaisesRegex(HelmError, r"--type approval-needed --action"):
            self.coordinator.record_worker_message(
                worker["id"], "status", "pausing", requested_status="approval-needed"
            )

    def test_a_process_worker_is_not_resumable_and_repair_makes_it_cleanable(self) -> None:
        """DEFECT 4: the no-Herdr fallback stranded a task nothing could release."""
        root = self.repo("fallback")
        project = self.coordinator.register_project(
            "Fallback", str(root), project_id="fallback"
        )
        task = self.coordinator.create_task(project["id"], "publish from a process")
        code = (
            "import json; print(json.dumps({'helm': 1, 'type': 'approval-needed', "
            "'text': 'publish now', 'payload': {'action': 'publish'}}))"
        )
        worker = self.coordinator.launch_worker(
            task["id"], [sys.executable, "-c", code], wait=True
        )
        # The stdout protocol path is the same intake as a direct push, so the
        # request reached the project's own record instead of vanishing.
        situation = " ".join(
            entry["text"]
            for entry in self.coordinator.project_status(project["id"])["situation"]
        )
        self.assertIn("publish now", situation)
        current = self.coordinator.inspect_task(task["id"])["task"]
        # Its session ended, so no hold survives it and the task is not parked
        # in permanent approval-needed residue.
        self.assertEqual(worker["status"], "completed")
        self.assertEqual(current["status"], "failed")
        self.assertEqual(self._hold(task["id"])["status"], "abandoned")
        with self.assertRaisesRegex(HelmError, r"not waiting on an approval"):
            self.coordinator.release_task_hold(
                task["id"], action="publish", confirm=True
            )
        # Cleanup is possible, which is what "no residue" has to mean.
        cleaned = self.coordinator.cleanup_task(task["id"], delete_branch=True)
        self.assertTrue(cleaned["workspace_removed"])

    def test_a_live_process_worker_refuses_release_without_spending_it(self) -> None:
        """A print-mode session cannot be told, so nothing is authorized into it."""
        project, task, worker = self._paused_on_approval("noinput", execution="process")
        with self.assertRaisesRegex(SafetyError, r"no input channel"):
            self.coordinator.release_task_hold(
                task["id"], action="publish", confirm=True
            )
        # Untouched: an authorization nobody can deliver must not be spent.
        self.assertEqual(self._hold(task["id"])["status"], "waiting")
        repaired = self.coordinator.repair_task_hold(task["id"], session_live=False)
        self.assertEqual(repaired["outcome"], "abandoned")
        self.assertEqual(
            self.coordinator.inspect_task(task["id"])["task"]["status"], "failed"
        )

    def test_an_undelivered_authorization_stays_pending_and_is_retryable(self) -> None:
        """DEFECT 5: a failed delivery resumed the task and closed the escalation."""
        project, task, worker = self._paused_on_approval("undelivered")
        argv = [
            "--state-dir", str(self.state.directory), "approval", "release",
            task["id"], "--action", "publish", "--confirm",
        ]
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            # No Herdr in the suite, so delivery cannot succeed.
            self.assertEqual(cli.main(argv), 1)
        self.assertIn("NOT delivered", output.getvalue())
        hold = self._hold(task["id"])
        self.assertEqual(hold["status"], "authorized-pending-delivery")
        self.assertIsNone(hold["delivery"]["delivered_at"])
        self.assertIsNone(hold["delivery"]["acknowledged_at"])
        # The task is still paused and the escalation is still open, because
        # nobody has been told anything.
        self.assertEqual(
            self.coordinator.inspect_task(task["id"])["task"]["status"], "approval-needed"
        )
        self.assertTrue(
            [e for e in self.coordinator.open_escalations() if e["task_id"] == task["id"]]
        )
        # Retrying is a delivery attempt, not a second decision.
        first = hold["authorization"]["ticket"]
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(cli.main(argv), 1)
        retried = self._hold(task["id"])
        self.assertEqual(retried["authorization"]["ticket"], first)
        self.assertEqual(retried["delivery"]["attempts"], 2)
        self.assertEqual(
            len([
                m for m in self.coordinator.inspect_task(task["id"])["messages"]
                if m["kind"] == "approval"
            ]),
            1,
        )
        # It is still usable by the session it was meant for.
        self.coordinator.start_authorized_action(worker["id"])
        self.assertEqual(self._hold(task["id"])["status"], "in-flight")

    def test_a_finished_authorized_action_leaves_no_stale_attention(self) -> None:
        """DEFECT 6: the approval item and its pane evidence stayed open forever."""
        project, task, worker = self._paused_on_approval("attention")
        opened = self.coordinator.project_status(project["id"])
        self.assertTrue(
            [i for i in opened["action_items"] if "Authorize or refuse" in i["text"]]
        )
        self.assertTrue(opened["evidence"])

        self.coordinator.release_task_hold(task["id"], action="publish", confirm=True)
        self.coordinator.start_authorized_action(worker["id"])
        self.coordinator.record_worker_message(
            worker["id"], "result", "published", payload={"receipt": "r-1"}
        )
        settled = self.coordinator.project_status(project["id"])
        self.assertEqual(
            [i for i in settled["action_items"] if "Authorize or refuse" in i["text"]], []
        )
        self.assertEqual(settled["evidence"], [])

    def test_a_failed_task_keeps_no_releasable_hold(self) -> None:
        """DEFECT 7: release resurrected a failed task back into `running`."""
        for spelling in ("failed", "blocked"):
            with self.subTest(status=spelling):
                project, task, worker = self._paused_on_approval(f"ended-{spelling}")
                ended = self.coordinator.record_worker_message(
                    worker["id"], "status", spelling, requested_status=spelling
                )
                self.assertEqual(ended["status"], spelling)
                self.assertEqual(self._hold(task["id"])["status"], "abandoned")
                with self.assertRaisesRegex(HelmError, r"not waiting on an approval"):
                    self.coordinator.release_task_hold(
                        task["id"], action="publish", confirm=True
                    )
                self.assertEqual(
                    self.coordinator.inspect_task(task["id"])["task"]["status"], spelling
                )

    def test_a_second_request_never_overwrites_a_live_hold(self) -> None:
        """DEFECT 8: a new request replaced an authorized hold and lost its state."""
        project, task, worker = self._paused_on_approval("repeat")
        first = self._hold(task["id"])["id"]
        # The same unanswered request restated is one thing to decide, not two.
        self.coordinator.record_worker_message(
            worker["id"], "approval-needed", "still ready", payload={"action": "publish"}
        )
        self.assertEqual(self._hold(task["id"])["id"], first)
        self.assertEqual(
            len(self.coordinator.inspect_task(task["id"])["task"]["holds"]), 1
        )
        # A different action while one is open is refused.
        with self.assertRaisesRegex(HelmError, r"already has a waiting hold"):
            self.coordinator.record_worker_message(
                worker["id"], "approval-needed", "delete staging",
                payload={"action": "delete"},
            )
        self.coordinator.release_task_hold(task["id"], action="publish", confirm=True)
        with self.assertRaisesRegex(HelmError, r"already has an? .*hold"):
            self.coordinator.record_worker_message(
                worker["id"], "approval-needed", "delete staging",
                payload={"action": "delete"},
            )
        # The authorized hold survived intact, with its history.
        live = self._hold(task["id"])
        self.assertEqual(live["id"], first)
        self.assertEqual(live["status"], "authorized-pending-delivery")
        self.assertTrue(live["authorization"]["ticket"])

    def test_a_post_action_receipt_never_invalidates_the_authorization_it_used(self) -> None:
        """DEFECT 9: writing a publish receipt invalidated the approval for succeeding."""
        project, task, worker = self._paused_on_approval("receipt")
        self.coordinator.release_task_hold(task["id"], action="publish", confirm=True)
        self.coordinator.start_authorized_action(worker["id"])
        # The action's own side effects land in the worktree after the gate.
        (Path(task["workspace"]) / "publish-receipt.txt").write_text(
            "remote id 123\n", encoding="utf-8"
        )
        result = self.coordinator.record_worker_message(
            worker["id"], "result", "published; receipt recorded",
            payload={"receipt": ["remote id 123"]},
        )
        self.assertEqual(result["status"], "completed")
        closed = self._hold(task["id"])
        self.assertEqual(closed["status"], "closed")
        self.assertEqual(closed["outcome"]["receipts"], ["remote id 123"])
        # Receipts are outcome data, kept outside the pre-action snapshot.
        self.assertNotIn("receipts", closed["authorization"]["snapshot"])

    def test_acting_without_the_gate_is_recorded_as_unauthorized(self) -> None:
        """A receipt with no consumed ticket is not evidence of an approved action."""
        project, task, worker = self._paused_on_approval("ungated")
        self.coordinator.release_task_hold(task["id"], action="publish", confirm=True)
        reported = self.coordinator.record_worker_message(
            worker["id"], "result", "published it anyway",
            payload={"receipt": "remote-2"},
        )
        self.assertEqual(reported["status"], "approval-needed")
        self.assertEqual(self._hold(task["id"])["status"], "invalidated")
        kinds = [m["kind"] for m in self.coordinator.inspect_task(task["id"])["messages"]]
        self.assertIn("approval-invalidated", kinds)

    def test_legacy_approval_state_migrates_and_can_be_repaired(self) -> None:
        """DEFECT 10: the state that motivated this change could not report at all."""
        project, task, worker = self._paused_on_approval("legacy")
        # Rewrite the record in the shape the previous build left behind: schema
        # 1, a single `hold` mapping, and a worker already marked failed.
        raw = json.loads(self.state.state_file.read_text(encoding="utf-8"))
        legacy_hold = raw["tasks"][task["id"]]["holds"][-1]
        legacy_hold["status"] = "authorized"
        raw["version"] = 1
        raw["tasks"][task["id"]].pop("holds")
        raw["tasks"][task["id"]]["hold"] = legacy_hold
        raw["workers"][worker["id"]]["status"] = "failed"
        raw["workers"][worker["id"]]["exit_code"] = 1
        raw["workers"][worker["id"]]["ended_at"] = "legacy"
        self.state.state_file.write_text(json.dumps(raw), encoding="utf-8")

        # It opens rather than being refused as corrupt, and an authorization
        # nobody can vouch for is downgraded, not honoured.
        migrated = self.coordinator.inspect_task(task["id"])["task"]
        self.assertEqual(self.coordinator.store.load()["version"], 2)
        self.assertEqual(migrated["holds"][-1]["status"], "invalidated")

        # A dead session is repaired into something cleanable.
        dead = self.coordinator.repair_task_hold(task["id"], session_live=False)
        self.assertEqual(dead["outcome"], "abandoned")
        self.assertEqual(
            self.coordinator.inspect_task(task["id"])["task"]["status"], "failed"
        )

        # With provider evidence that the same session is live, the hold is
        # reconstructed from the recorded request and that worker revived.
        revived = self.coordinator.repair_task_hold(task["id"], session_live=True)
        self.assertEqual(revived["outcome"], "reconstructed")
        self.assertEqual(revived["hold"]["action"], "publish")
        self.assertEqual(
            self.coordinator.store.load()["workers"][worker["id"]]["status"], "running"
        )
        # And from there the normal path works end to end.
        self.coordinator.release_task_hold(task["id"], action="publish", confirm=True)
        self.coordinator.start_authorized_action(worker["id"])
        done = self.coordinator.record_worker_message(
            worker["id"], "result", "published after repair", payload={"receipt": "r"}
        )
        self.assertEqual(done["status"], "completed")

    def test_repair_never_invents_the_action_it_could_not_read(self) -> None:
        project, task, worker = self._paused_on_approval("restate")
        with self.coordinator.store.locked() as data:
            record = data["tasks"][task["id"]]
            record["holds"] = []
            record["status"] = "approval-needed"
            for message in data["messages"]:
                if message["kind"] == "approval-needed":
                    message["payload"] = {}
        outcome = self.coordinator.repair_task_hold(task["id"], session_live=True)
        self.assertEqual(outcome["outcome"], "restate-requested")
        self.assertIsNone(outcome["hold"])
        answers = [
            m for m in self.coordinator.inspect_task(task["id"])["messages"]
            if m["kind"] == "answer"
        ]
        self.assertIn("--action", answers[-1]["text"])

    def test_an_agent_cannot_authorize_by_clearing_or_spoofing_its_marker(self) -> None:
        """DEFECT 11: `env -u HELM_WORKER_ID` was accepted as the root."""
        project, task, worker = self._paused_on_approval("identity")
        argv = ["--state-dir", str(self.state.directory)]
        release = [
            *argv, "approval", "release", task["id"], "--action", "publish", "--confirm",
        ]
        # Marked: refused, as before.
        with mock.patch.dict(os.environ, {"HELM_WORKER_ID": worker["id"]}):
            with contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(cli.main(release), 2)

        # Unmarked but recorded as this very process: identified by lineage, not
        # by the variable it can edit.
        with self.coordinator.store.locked() as data:
            data["workers"][worker["id"]]["pid"] = os.getpid()
        environment = {k: v for k, v in os.environ.items() if k != "HELM_WORKER_ID"}
        with mock.patch.dict(os.environ, environment, clear=True):
            with contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(cli.main(release), 2)
            # And the core call an agent could make directly, skipping dispatch.
            with self.assertRaisesRegex(SafetyError, r"identified as worker"):
                self.coordinator.release_task_hold(
                    task["id"], action="publish", confirm=True
                )
            with self.assertRaisesRegex(SafetyError, r"identified as worker"):
                self.coordinator.grant_approval("publish", note="self-granted")
        self.assertEqual(self._hold(task["id"])["status"], "waiting")
        self.assertEqual(self.coordinator.list_approval_grants(), [])

    def test_a_spawned_command_inherits_the_workers_lineage(self) -> None:
        """A child process cannot shed its ancestry, whatever it does to its env."""
        project, task, worker = self._paused_on_approval("lineage")
        with self.coordinator.store.locked() as data:
            data["workers"][worker["id"]]["pid"] = os.getpid()
        environment = {k: v for k, v in os.environ.items() if k != "HELM_WORKER_ID"}
        environment["PYTHONPATH"] = str(REPO_ROOT)
        programs = {
            # Through the CLI, whose dispatch check is the readable refusal...
            "cli": (
                "import sys; from helm import cli; "
                f"sys.exit(cli.main(['--state-dir', {str(self.state.directory)!r}, "
                f"'approval', 'release', {task['id']!r}, '--action', 'publish', "
                "'--confirm']))"
            ),
            # ...and straight into core, which is the enforced boundary: an
            # agent that can import Coordinator never reaches dispatch at all.
            "core": (
                "from helm.core import Coordinator, StateStore; "
                f"c = Coordinator(StateStore({str(self.state.directory)!r})); "
                f"c.release_task_hold({task['id']!r}, action='publish', confirm=True)"
            ),
        }
        for route, program in programs.items():
            with self.subTest(route=route):
                finished = subprocess.run(
                    [sys.executable, "-c", program],
                    env=environment, text=True, capture_output=True,
                )
                self.assertNotEqual(finished.returncode, 0)
                combined = finished.stdout + finished.stderr
                self.assertTrue(
                    "identified as worker" in combined
                    or "cannot authorize it for itself" in combined,
                    combined,
                )
        self.assertEqual(self._hold(task["id"])["status"], "waiting")

    def test_a_configured_capability_is_required_and_never_inheritable(self) -> None:
        """The capability is the boundary that survives a stolen identity."""
        project, task, worker = self._paused_on_approval("capability")
        secret = "x" * 48
        path = self.coordinator.configure_authority(secret)
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        # It is never in what a worker is given.
        self.assertNotIn("HELM_AUTHORITY", worker_environment({"HELM_AUTHORITY": secret}))
        context = json.loads(Path(worker["context_file"]).read_text(encoding="utf-8"))
        self.assertNotIn(secret, json.dumps(context))

        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("HELM_AUTHORITY", None)
            with self.assertRaisesRegex(SafetyError, r"authorization capability"):
                self.coordinator.release_task_hold(
                    task["id"], action="publish", confirm=True
                )
        with mock.patch.dict(os.environ, {"HELM_AUTHORITY": "wrong"}):
            with self.assertRaisesRegex(SafetyError, r"does not match"):
                self.coordinator.release_task_hold(
                    task["id"], action="publish", confirm=True
                )
        with mock.patch.dict(os.environ, {"HELM_AUTHORITY": secret}):
            released = self.coordinator.release_task_hold(
                task["id"], action="publish", confirm=True
            )
        self.assertEqual(
            released["hold"]["authorization"]["authority"]["mode"], "capability"
        )

    def test_the_hold_transition_table_refuses_every_unwritten_route(self) -> None:
        """One table, enforced: a route that is not written cannot be reached."""
        from helm.core import HOLD_STATUSES, HOLD_TRANSITIONS

        events = sorted({event for _, event in HOLD_TRANSITIONS})
        project, task, worker = self._paused_on_approval("table")
        data = self.coordinator.store.load()
        record = data["tasks"][task["id"]]
        live = self.coordinator.task_hold(record)
        for status in sorted(HOLD_STATUSES):
            for event in events:
                with self.subTest(status=status, event=event):
                    live["status"] = status
                    expected = HOLD_TRANSITIONS.get((status, event))
                    if expected is None:
                        with self.assertRaisesRegex(SafetyError, r"cannot " + event):
                            self.coordinator._move_hold(
                                data,
                                data["projects"][project["id"]],
                                record,
                                live,
                                event,
                            )
                    else:
                        moved = self.coordinator._move_hold(
                            data,
                            data["projects"][project["id"]],
                            record,
                            live,
                            event,
                        )
                        self.assertEqual(moved["status"], expected)

    def test_only_the_asking_session_can_spend_its_authorization(self) -> None:
        project, task, worker = self._paused_on_approval("owner")
        self.coordinator.release_task_hold(task["id"], action="publish", confirm=True)
        other_task = self.coordinator.create_task(project["id"], "another job")
        other = self.coordinator.prepare_external_worker(
            other_task["id"], [sys.executable, "-c", ""]
        )
        with self.assertRaisesRegex(HelmError, r"has no approval hold"):
            self.coordinator.start_authorized_action(other["id"])
        # And a worker cannot act on an authorization that was never given.
        fresh_project, fresh_task, fresh_worker = self._paused_on_approval("unapproved")
        with self.assertRaisesRegex(SafetyError, r"not authorized"):
            self.coordinator.start_authorized_action(fresh_worker["id"])

    def test_release_authorizes_only_the_action_that_was_asked_for(self) -> None:
        project, task, worker = self._paused_on_approval("authorize")
        with self.assertRaisesRegex(SafetyError, r"helm task approve"):
            self.coordinator.release_task_hold(task["id"], action="merge", confirm=True)
        with self.assertRaisesRegex(SafetyError, r"asked for publish, not push"):
            self.coordinator.release_task_hold(task["id"], action="push", confirm=True)
        with self.assertRaisesRegex(SafetyError, r"no standing approval covers publish"):
            self.coordinator.release_task_hold(task["id"], action="publish")
        with self.assertRaisesRegex(HelmError, r"not both"):
            self.coordinator.release_task_hold(
                task["id"], action="publish", confirm=True, grant_id="g-any"
            )
        revoked = self.coordinator.grant_approval("publish", note="while away")
        self.coordinator.revoke_approval_grant(revoked["id"], "back now")
        with self.assertRaisesRegex(SafetyError, r"was revoked"):
            self.coordinator.release_task_hold(
                task["id"], action="publish", grant_id=revoked["id"]
            )
        pushes = self.coordinator.grant_approval("push", note="pushes are fine")
        with self.assertRaisesRegex(SafetyError, r"covers push, not publish"):
            self.coordinator.release_task_hold(
                task["id"], action="publish", grant_id=pushes["id"]
            )
        other = self.repo("elsewhere")
        self.coordinator.register_project("Elsewhere", str(other), project_id="elsewhere")
        elsewhere = self.coordinator.grant_approval(
            "publish", project_id="elsewhere", note="only that project"
        )
        with self.assertRaisesRegex(SafetyError, r"scoped to project elsewhere"):
            self.coordinator.release_task_hold(
                task["id"], action="publish", grant_id=elsewhere["id"]
            )
        with self.assertRaisesRegex(HelmError, r"unknown approval grant"):
            self.coordinator.release_task_hold(
                task["id"], action="publish", grant_id="g-invented"
            )
        self.assertEqual(self._hold(task["id"])["status"], "waiting")
        # An agent cannot release its own hold through the CLI either.
        argv = ["--state-dir", str(self.state.directory)]
        for actor in (worker["id"], "w-someone-else"):
            with mock.patch.dict(os.environ, {"HELM_WORKER_ID": actor}):
                with contextlib.redirect_stderr(io.StringIO()):
                    self.assertEqual(
                        cli.main([*argv, "approval", "release", task["id"],
                                  "--action", "publish", "--confirm"]),
                        2,
                    )
        self.assertEqual(self._hold(task["id"])["status"], "waiting")
        # A standing grant scoped to this project does authorize it.
        good = self.coordinator.grant_approval(
            "publish", project_id=project["id"], note="channel uploads are fine"
        )
        released = self.coordinator.release_task_hold(
            task["id"], action="publish", grant_id=good["id"]
        )
        self.assertEqual(
            released["hold"]["authorization"]["grant_id"], good["id"]
        )

    def test_a_paused_task_keeps_its_space_and_releases_it_once_it_finishes(self) -> None:
        root = self.repo("held-space")
        project = self.coordinator.register_project(
            "Held", str(root), project_id="held-space"
        )
        herdr = FakeHerdr()
        adapter = HerdrAdapter(self.coordinator, herdr)
        task = self.coordinator.create_task(project["id"], "publish something")
        worker = adapter.launch_task(task["id"], [sys.executable, "-c", ""], wait=False)
        self.coordinator.record_worker_message(
            worker["id"], "approval-needed", "ready to publish",
            payload={"action": "publish"},
        )

        # A human still has to look, so the pane and the space both stay.
        self.assertEqual(adapter.release_finished_tabs(), [])
        self.assertFalse(adapter.close_project_space_if_finished(project["id"]))
        self.assertEqual(herdr.closed_workspaces, [])
        # Delivery goes to a session the provider says is really there.
        self.assertTrue(adapter.session_reachable(worker["id"]))
        self.coordinator.release_task_hold(task["id"], action="publish", confirm=True)
        self.assertTrue(adapter.answer_worker(worker["id"], "Approved: publish"))
        self.assertEqual(herdr.sent_text[-1][1], "Approved: publish")
        self.assertEqual(herdr.sent_keys[-1][1], "Enter")
        self.coordinator.mark_hold_delivered(task["id"], delivered=True)

        self.coordinator.start_authorized_action(worker["id"])
        self.coordinator.record_worker_message(
            worker["id"], "result", "published", payload={"receipt": "r-9"}
        )
        # Reported, so the pane has nothing left to show and is released --
        # but the task is `completed`, not delivered, and releasing the tab is
        # the first thing a clean result does. The space stays until somebody
        # has actually decided what happens to the work.
        self.assertEqual(adapter.release_finished_tabs(), [worker["id"]])
        self.assertFalse(adapter.close_project_space_if_finished(project["id"]))
        self.assertEqual(herdr.closed_workspaces, [])

        self.coordinator.cleanup_task(task["id"])
        self.assertTrue(adapter.close_project_space_if_finished(project["id"]))
        self.assertEqual(len(herdr.closed_workspaces), 1)

    def test_a_vanished_pane_is_reconciled_before_anything_is_authorized(self) -> None:
        root = self.repo("vanished")
        project = self.coordinator.register_project(
            "Vanished", str(root), project_id="vanished"
        )

        class VanishedPaneHerdr(FakeHerdr):
            def pane_status(self, pane_id: str) -> dict[str, object]:
                return {"result": {"pane": {"status": "missing"}}}

        herdr = VanishedPaneHerdr()
        adapter = HerdrAdapter(self.coordinator, herdr)
        task = self.coordinator.create_task(project["id"], "publish something")
        worker = adapter.launch_task(task["id"], [sys.executable, "-c", ""], wait=False)
        self.coordinator.record_worker_message(
            worker["id"], "approval-needed", "ready", payload={"action": "publish"}
        )
        # The user closed the pane; the record has not caught up yet.
        self.assertFalse(adapter.session_reachable(worker["id"]))
        settled = self.coordinator.store.load()["workers"][worker["id"]]
        self.assertEqual(settled["status"], "failed")
        self.assertEqual(self._hold(task["id"])["status"], "abandoned")

    def test_an_agent_cannot_run_the_commands_its_rules_forbid_it(self) -> None:
        root = self.repo("authority")
        project = self.coordinator.register_project(
            "Auth", str(root), project_id="authority"
        )
        worker_task = self.coordinator.create_task(project["id"], "write the code")
        worker = self.coordinator.prepare_external_worker(
            worker_task["id"], [sys.executable, "-c", ""]
        )
        foreman_task = self.coordinator.create_foreman_task(project["id"])
        foreman = self.coordinator.prepare_external_worker(
            foreman_task["id"], [sys.executable, "-c", ""]
        )
        argv = ["--state-dir", str(self.state.directory)]

        def run_as(worker_id: str, *command: str) -> int:
            with mock.patch.dict(os.environ, {"HELM_WORKER_ID": worker_id}):
                return cli.main([*argv, *command])

        # A grant records the human's own policy. No agent writes one, however
        # convinced it is that the action is fine.
        self.assertEqual(
            run_as(foreman["id"], "approval", "grant", "merge", "--note", "sure"), 2
        )
        self.assertEqual(
            run_as(worker["id"], "approval", "grant", "merge", "--note", "sure"), 2
        )
        self.assertEqual(self.coordinator.list_approval_grants(), [])

        # Delegation is one level deep: the foreman may drive, the worker may
        # not spawn.
        before = len(self.coordinator.store.load()["tasks"])
        self.assertEqual(
            run_as(worker["id"], "task", "create", "--project", project["id"],
                   "--brief", "do more work"), 2
        )
        self.assertEqual(len(self.coordinator.store.load()["tasks"]), before)

        # And what the worker is actually for still works.
        self.assertEqual(
            run_as(worker["id"], "worker", "message", worker["id"],
                   "--type", "status", "--text", "still going"), 0
        )

    def test_implementation_in_an_assigned_worktree_needs_no_further_approval(
        self,
    ) -> None:
        """Delegation would deadlock if the brief were not authority to build.

        `agent-autonomy` and the `software-delivery` guardrails both told a
        worker to wait for an explicit approval before implementing. Nobody
        reads a worker's session, so that approval never arrives: the worker
        stalls looking exactly like one that died, and the spec decision above
        would have been read as the gate it is explicitly not.
        """
        coordinator, project = self._shipped_domains_project("specapproval")
        task = coordinator.create_task(
            project["id"], "implement it", domain="software-delivery"
        )
        blob = self._composed(coordinator, project, task)

        self.assertIn("the assigned task and its brief are the authority to", blob)
        self.assertIn("The assigned brief is the authority to implement", blob)
        for stale in (
            "Explicit approval is required before implementation starts",
            "Wait for explicit approval before implementing",
        ):
            self.assertNotIn(stale, blob, stale)

        # True safety is untouched: planning still asks, and the protected
        # list still stops for a human.
        self.assertIn("understanding and planning, which stop and ask", blob)
        for protected in ("merge", "push", "publish", "delete"):
            self.assertIn(protected, blob, protected)
        self.assertIn("Silence is not approval for any of those", blob)

    def test_approval_binds_terminal_worker_and_immutable_revision(self) -> None:
        root = self.repo("approval-boundary")
        project = self.coordinator.register_project("Approval", str(root), project_id="approval")
        task = self.coordinator.create_task(project["id"], "commit reviewed content")
        code = (
            "from pathlib import Path; import subprocess; "
            "Path('reviewed.txt').write_text('reviewed'); "
            "subprocess.run(['git','add','reviewed.txt'],check=True); "
            "subprocess.run(['git','commit','-qm','reviewed'],check=True)"
        )
        worker = self.coordinator.launch_worker(task["id"], [sys.executable, "-c", code])
        approved = self.coordinator.approve_task(task["id"], "reviewed")
        self.assertEqual(approved["approval"]["worker_id"], worker["id"])
        self.assertTrue(approved["approval"]["branch_tip"])
        self.assertTrue(approved["approval"]["tree"])
        workspace = Path(worker["workspace"])
        (workspace / "unreviewed.txt").write_text("unreviewed")
        subprocess.run(["git", "add", "unreviewed.txt"], cwd=workspace, check=True)
        subprocess.run(["git", "commit", "-qm", "unreviewed"], cwd=workspace, check=True)
        with self.assertRaisesRegex(SafetyError, "re-review"):
            self.coordinator.merge_task(task["id"])
        self.assertIsNone(self.coordinator.inspect_task(task["id"])["task"]["approval"])

    def test_live_worker_blocks_approval_and_cleanup_after_protocol_terminal_message(self) -> None:
        root = self.repo("live-worker")
        project = self.coordinator.register_project("Live", str(root), project_id="live")
        task = self.coordinator.create_task(project["id"], "keep running")
        adapter = HerdrAdapter(self.coordinator, FakeHerdr())
        worker = adapter.launch_task(task["id"], [sys.executable, "-c", ""], wait=False)
        self.coordinator.record_worker_message(
            worker["id"], "status", "reported complete", requested_status="completed"
        )
        # A non-terminal status must not close a still-working provider session
        # or make the task eligible for review/approval.
        self.assertEqual(self.coordinator.inspect_task(task["id"])["workers"][0]["status"], "running")
        with self.assertRaises(SafetyError):
            self.coordinator.approve_task(task["id"])
        with self.assertRaises(SafetyError):
            self.coordinator.cleanup_task(task["id"])
        with self.assertRaises(SafetyError):
            adapter.cleanup_task(task["id"])


class EscalationReconciliationTests(ApprovalTests):
    """`open_escalations` must reflect current unresolved state, not replay history."""

    def _running_task(self, name: str) -> tuple[dict, dict, dict]:
        root = self.repo(name)
        project = self.coordinator.register_project(name, str(root), project_id=name)
        task = self.coordinator.create_task(project["id"], "do the work")
        worker = self.coordinator.launch_worker(
            task["id"], [sys.executable, "-c", ""], wait=False
        )
        return project, task, worker

    def _escalation_task_ids(self, project_id: str | None = None) -> set[str]:
        return {
            e["task_id"] for e in self.coordinator.open_escalations(project_id)
        }

    def test_a_live_question_stays_in_the_queue(self) -> None:
        project, task, worker = self._running_task("live-question")
        self.coordinator.record_worker_message(worker["id"], "question", "which branch?")
        self.assertIn(task["id"], self._escalation_task_ids())
        items = self.coordinator.open_escalations()
        entry = next(e for e in items if e["task_id"] == task["id"])
        self.assertEqual(entry["kind"], "question")

    def test_an_answered_question_leaves_the_queue(self) -> None:
        project, task, worker = self._running_task("answered-question")
        self.coordinator.record_worker_message(worker["id"], "question", "which branch?")
        self.coordinator.record_worker_message(worker["id"], "answer", "main")
        self.assertNotIn(task["id"], self._escalation_task_ids())

    def test_a_question_moot_after_the_task_moves_on_without_an_answer(self) -> None:
        """A worker can report a terminal message without anybody ever answering.

        Replaying the old question after the task has finished would send a
        reader to a prompt nothing is still waiting on.
        """
        project, task, worker = self._running_task("moot-question")
        self.coordinator.record_worker_message(worker["id"], "question", "which branch?")
        self.assertIn(task["id"], self._escalation_task_ids())
        self.coordinator.record_worker_message(worker["id"], "result", "went with main")
        self.assertNotIn(task["id"], self._escalation_task_ids())

    def test_a_live_blocker_stays_in_the_queue(self) -> None:
        project, task, worker = self._running_task("live-blocker")
        self.coordinator.record_worker_message(worker["id"], "blocker", "needs a credential")
        self.assertEqual(
            self.coordinator.inspect_task(task["id"])["task"]["status"], "blocked"
        )
        self.assertIn(task["id"], self._escalation_task_ids())

    def test_an_abandoned_approval_hold_leaves_the_queue(self) -> None:
        """A session that can never be told anything is abandoned, not left
        asking for an answer forever."""
        project, task, worker = self._paused_on_approval("abandon-approval", execution="process")
        self.assertIn(task["id"], self._escalation_task_ids())
        repaired = self.coordinator.repair_task_hold(task["id"], session_live=False)
        self.assertEqual(repaired["outcome"], "abandoned")
        self.assertEqual(
            self.coordinator.inspect_task(task["id"])["task"]["status"], "failed"
        )
        self.assertNotIn(task["id"], self._escalation_task_ids())

    def test_only_the_newest_pending_ask_per_worker_is_shown(self) -> None:
        project, task, worker = self._running_task("repeat-asks")
        # A running worker can ask more than once before either is answered.
        # Only the current ask is a live prompt; an earlier one from the same
        # session is superseded by construction.
        self.coordinator.record_worker_message(worker["id"], "question", "first question")
        self.coordinator.record_worker_message(worker["id"], "question", "second, different question")
        items = [e for e in self.coordinator.open_escalations() if e["task_id"] == task["id"]]
        self.assertEqual(len(items), 1)
        self.assertIn("different question", items[0]["text"])

    def test_an_approval_hold_stays_in_the_queue_until_released(self) -> None:
        project, task, worker = self._paused_on_approval("live-approval")
        self.assertIn(task["id"], self._escalation_task_ids())

    def test_a_released_and_delivered_approval_leaves_the_queue_without_an_answer(self) -> None:
        """DEFECT: `open_escalations` only ever cleared on an `answer` message,
        so a hold released and delivered through `helm approval release` --
        never answered directly -- kept replaying the original prompt forever.
        """
        project, task, worker = self._paused_on_approval("released-approval")
        self.assertIn(task["id"], self._escalation_task_ids())
        self.coordinator.release_task_hold(task["id"], action="publish", confirm=True)
        # Authorized but not yet spent: still a live pause, still visible.
        self.assertIn(task["id"], self._escalation_task_ids())
        self.coordinator.start_authorized_action(worker["id"])
        # The session has spent the authorization and moved on; the original
        # approval-needed prompt is no longer anything to answer.
        self.assertNotIn(task["id"], self._escalation_task_ids())
        self.coordinator.record_worker_message(worker["id"], "result", "published it")
        self.assertNotIn(task["id"], self._escalation_task_ids())

    def test_escalations_stay_scoped_to_their_project(self) -> None:
        _, task_a, worker_a = self._running_task("proj-a")
        _, task_b, worker_b = self._running_task("proj-b")
        self.coordinator.record_worker_message(worker_a["id"], "question", "for a?")
        self.coordinator.record_worker_message(worker_b["id"], "question", "for b?")
        only_a = self._escalation_task_ids("proj-a")
        self.assertIn(task_a["id"], only_a)
        self.assertNotIn(task_b["id"], only_a)

    def test_a_question_stays_visible_while_the_task_is_paused_on_an_approval(self) -> None:
        """A live worker can ask a plain question while its own task sits in
        `approval-needed` on an unrelated hold. Task status is not a valid
        proxy for whether that worker's question is still live, and the
        still-open hold must not be lost either -- both are distinct asks
        from the same live session.
        """
        project, task, worker = self._paused_on_approval("question-during-hold")
        self.assertEqual(
            self.coordinator.inspect_task(task["id"])["task"]["status"], "approval-needed"
        )
        self.coordinator.record_worker_message(
            worker["id"], "question", "should the artifact include the changelog?"
        )
        items = [e for e in self.coordinator.open_escalations() if e["task_id"] == task["id"]]
        kinds = {e["kind"] for e in items}
        self.assertEqual(kinds, {"question", "approval-needed"})

    def test_a_settled_rounds_question_never_returns_under_a_later_round(self) -> None:
        """Round one's worker asks and is never answered, then reports a
        result of its own accord. Round two reopens the same task under a
        fresh worker and the task returns to `running` -- the old question
        must not resurface just because task status matches again.
        """
        root = self.repo("round-question")
        project = self.coordinator.register_project(
            "RoundQuestion", str(root), project_id="round-question"
        )
        task = self.coordinator.create_task(project["id"], "first pass")
        worker_one = self.coordinator.launch_worker(
            task["id"], [sys.executable, "-c", ""], wait=False
        )
        self.coordinator.record_worker_message(worker_one["id"], "question", "which base?")
        self.assertIn(task["id"], self._escalation_task_ids())
        self.coordinator.record_worker_message(worker_one["id"], "result", "used main anyway")
        self.assertNotIn(task["id"], self._escalation_task_ids())

        self.coordinator.continue_task(task["id"], "second pass")
        worker_two = self.coordinator.launch_worker(
            task["id"], [sys.executable, "-c", ""], wait=False
        )
        self.assertEqual(
            self.coordinator.inspect_task(task["id"])["task"]["status"], "running"
        )
        # Task status matches round one's question again, but round one's
        # own worker is settled -- its question must stay gone.
        self.assertNotIn(task["id"], self._escalation_task_ids())
        items = self.coordinator.open_escalations()
        self.assertFalse(any(e["worker_id"] == worker_one["id"] for e in items))

        # A fresh question from round two's own live worker is unaffected.
        self.coordinator.record_worker_message(worker_two["id"], "question", "which base now?")
        self.assertIn(task["id"], self._escalation_task_ids())

    def test_an_unrelated_answer_does_not_clear_a_still_open_approval_hold(self) -> None:
        """A foreman or commander routinely sends `answer` text into a paused
        worker's session without that touching its hold at all -- the hold's
        own status is what decides whether the protected-action decision is
        still open, not the presence of any later `answer` message.
        """
        project, task, worker = self._paused_on_approval("answer-does-not-clear-hold")
        self.assertIn(task["id"], self._escalation_task_ids())
        self.coordinator.record_worker_message(worker["id"], "answer", "looking at it")
        self.assertIn(task["id"], self._escalation_task_ids())
        items = [e for e in self.coordinator.open_escalations() if e["task_id"] == task["id"]]
        self.assertEqual([e["kind"] for e in items], ["approval-needed"])
        # The hold's own resolution still clears it.
        self.coordinator.release_task_hold(task["id"], action="publish", confirm=True)
        self.coordinator.start_authorized_action(worker["id"])
        self.assertNotIn(task["id"], self._escalation_task_ids())

    def test_a_missing_worker_record_is_surfaced_not_silently_dropped(self) -> None:
        """An ask whose worker record has gone missing cannot be proven
        resolved, so it must not vanish from the queue the way a genuinely
        answered one does.
        """
        project, task, worker = self._running_task("orphaned-worker")
        self.coordinator.record_worker_message(worker["id"], "question", "which branch?")
        with self.coordinator.store.locked() as data:
            del data["workers"][worker["id"]]
        items = [
            e for e in self.coordinator.open_escalations() if e["worker_id"] == worker["id"]
        ]
        self.assertEqual(len(items), 1)
        entry = items[0]
        self.assertEqual(entry["kind"], "question")
        self.assertEqual(entry["project_id"], project["id"])
        self.assertEqual(entry["task_id"], task["id"])
        self.assertIn("diagnostic", entry)
        # Still correctly scoped to its project, even without a worker record.
        self.assertTrue(
            any(e["worker_id"] == worker["id"] for e in self.coordinator.open_escalations(project["id"]))
        )

    def test_a_missing_task_record_is_surfaced_not_silently_dropped(self) -> None:
        project, task, worker = self._running_task("orphaned-task")
        self.coordinator.record_worker_message(worker["id"], "blocker", "needs a credential")
        with self.coordinator.store.locked() as data:
            del data["tasks"][task["id"]]
        items = [
            e for e in self.coordinator.open_escalations() if e["worker_id"] == worker["id"]
        ]
        self.assertEqual(len(items), 1)
        self.assertIn("diagnostic", items[0])
        self.assertEqual(items[0]["task_id"], task["id"])
        self.assertEqual(items[0]["project_id"], project["id"])

    def test_a_restated_approval_stays_a_single_live_entry(self) -> None:
        """A worker repeating the exact same unanswered request appends a new
        `approval-needed` message without moving the hold's own `message_id`.
        Matching against "the latest approval-needed message" would miss the
        restated one entirely; this must still show as one live entry.
        """
        project, task, worker = self._paused_on_approval("restated-approval")
        self.coordinator.record_worker_message(
            worker["id"], "approval-needed", "ready to publish the rendered file",
            payload={"action": "publish"},
        )
        items = [e for e in self.coordinator.open_escalations() if e["task_id"] == task["id"]]
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["kind"], "approval-needed")

    def test_a_new_rounds_approval_hold_never_resurrects_an_old_rounds(self) -> None:
        """End to end across two rounds on one task: round one's hold is
        released, spent, and the worker reports its result; round two is then
        continued under a fresh worker that opens its own, different hold.
        Only round two's approval-needed message may appear -- round one's
        already-settled request must never come back just because the task
        is `approval-needed` again.
        """
        project, task, worker_one = self._paused_on_approval(
            "multi-round-approval", action="publish"
        )
        self.assertIn(task["id"], self._escalation_task_ids())

        self.coordinator.release_task_hold(task["id"], action="publish", confirm=True)
        self.coordinator.start_authorized_action(worker_one["id"])
        self.coordinator.record_worker_message(
            worker_one["id"], "result", "published round one",
            payload={"receipt": "round-one-receipt"},
        )
        self.assertEqual(
            self.coordinator.inspect_task(task["id"])["task"]["status"], "completed"
        )
        self.assertNotIn(task["id"], self._escalation_task_ids())

        self.coordinator.continue_task(task["id"], "round two: remove the stale export")
        worker_two = self.coordinator.prepare_external_worker(
            task["id"], [sys.executable, "-c", ""], execution="external"
        )
        self.coordinator.record_worker_message(
            worker_two["id"], "approval-needed", "ready to delete the stale export",
            payload={"action": "delete"},
        )
        self.assertEqual(
            self.coordinator.inspect_task(task["id"])["task"]["status"], "approval-needed"
        )

        items = [e for e in self.coordinator.open_escalations() if e["task_id"] == task["id"]]
        self.assertEqual(len(items), 1)
        entry = items[0]
        self.assertEqual(entry["kind"], "approval-needed")
        self.assertEqual(entry["worker_id"], worker_two["id"])
        self.assertIn("delete the stale export", entry["text"])
        # Round one's own worker never reappears, under any kind.
        self.assertFalse(any(e["worker_id"] == worker_one["id"] for e in items))

    def test_a_hold_pointing_at_a_foreign_message_falls_into_the_diagnostic_path(self) -> None:
        """A hold's `message_id` resolving to a real message that belongs to
        someone else's ask -- wrong kind, wrong task, wrong worker, or wrong
        project -- must never lend that message's text or timestamp to this
        hold's escalation. The hold's own fields are the only thing trusted
        once that mismatch is detected.
        """
        project, task, worker = self._paused_on_approval("foreign-message")
        other_project, other_task, other_worker = self._paused_on_approval(
            "foreign-message-other"
        )
        with self.coordinator.store.locked() as data:
            hold = data["tasks"][task["id"]]["holds"][-1]
            foreign_id = data["tasks"][other_task["id"]]["holds"][-1]["message_id"]
            hold["message_id"] = foreign_id

        items = [e for e in self.coordinator.open_escalations() if e["task_id"] == task["id"]]
        self.assertEqual(len(items), 1)
        entry = items[0]
        self.assertEqual(entry["kind"], "approval-needed")
        self.assertEqual(entry["worker_id"], worker["id"])
        self.assertIn("diagnostic", entry)
        # Never the foreign task's text -- only this hold's own recorded text.
        self.assertNotIn(other_task["id"], entry["text"])
        self.assertIn("ready to publish", entry["text"])
        # The other project's genuine escalation is unaffected.
        other_items = [
            e for e in self.coordinator.open_escalations() if e["task_id"] == other_task["id"]
        ]
        self.assertEqual(len(other_items), 1)
        self.assertNotIn("diagnostic", other_items[0])

    def test_a_removed_tasks_restated_approval_history_collapses_to_one_row(self) -> None:
        """A worker can restate the same unanswered request more than once
        before its task record is later removed entirely -- by state cleanup
        outside Helm, or corruption. Every one of those historical messages
        still names the same worker; they must collapse to a single
        diagnostic row, not one per historical message.
        """
        project, task, worker = self._paused_on_approval("removed-task-restated")
        self.coordinator.record_worker_message(
            worker["id"], "approval-needed", "ready to publish the rendered file",
            payload={"action": "publish"},
        )
        self.coordinator.record_worker_message(
            worker["id"], "approval-needed", "ready to publish the rendered file",
            payload={"action": "publish"},
        )
        approval_needed_count = sum(
            1
            for m in self.coordinator.store.load()["messages"]
            if m.get("kind") == "approval-needed" and m.get("worker_id") == worker["id"]
        )
        self.assertEqual(approval_needed_count, 3)
        with self.coordinator.store.locked() as data:
            del data["tasks"][task["id"]]

        items = [
            e for e in self.coordinator.open_escalations() if e["worker_id"] == worker["id"]
        ]
        self.assertEqual(len(items), 1)
        self.assertIn("diagnostic", items[0])
        self.assertEqual(items[0]["kind"], "approval-needed")

    def test_a_live_hold_with_a_missing_worker_record_appears_exactly_once(self) -> None:
        """The hold itself is genuinely still open -- the task's own record
        says so -- but the worker behind it is gone. That must not be shown
        twice: once from the hold's own liveness and once again from a
        separate orphan pass over messages. It stays actionable (the hold is
        real) and is marked unverified rather than silently trusted or
        dropped, and the CLI must say so plainly.
        """
        project, task, worker = self._paused_on_approval("missing-worker-live-hold")
        with self.coordinator.store.locked() as data:
            del data["workers"][worker["id"]]

        items = [e for e in self.coordinator.open_escalations() if e["task_id"] == task["id"]]
        self.assertEqual(len(items), 1)
        entry = items[0]
        self.assertEqual(entry["kind"], "approval-needed")
        self.assertEqual(entry["worker_id"], worker["id"])
        self.assertIn("diagnostic", entry)
        self.assertIn("ready to publish", entry["text"])

        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            cli._print_status(self.coordinator, None)
        printed = output.getvalue()
        self.assertIn("Needs you (1)", printed)
        self.assertIn("UNVERIFIED", printed)
        self.assertEqual(
            sum(1 for line in printed.splitlines() if worker["id"] in line), 1
        )
