"""The foreman confirmation gates: requirement and solution, before a
state-changing worker may launch."""

from __future__ import annotations

import io
import contextlib
import json
import os
import signal
import subprocess
import sys
from pathlib import Path
from unittest import mock

from helm import cli
from helm.core import HelmError, SafetyError

from tests.support import HelmTestCase


class GateTests(HelmTestCase):
    def _project_with_live_foreman(self, name: str) -> tuple[dict, dict, dict]:
        root = self.repo(name)
        project = self.coordinator.register_project(name, str(root), project_id=name)
        foreman_task = self.coordinator.create_foreman_task(project["id"])
        worker = self.coordinator.prepare_external_worker(
            foreman_task["id"], [sys.executable, "-c", ""], execution="external"
        )
        return project, foreman_task, worker

    def _as_foreman(self, worker_id: str):
        return mock.patch.dict(os.environ, {"HELM_WORKER_ID": worker_id})

    def test_state_changing_worker_refused_until_both_gates_confirmed(self) -> None:
        project, foreman_task, worker = self._project_with_live_foreman("gatedproject")
        with self._as_foreman(worker["id"]):
            with self.assertRaisesRegex(HelmError, "requirement gate has not been proposed"):
                self.coordinator.create_task(project["id"], "ship the thing")

            self.coordinator.propose_gate(
                foreman_task["id"], "requirement", "goal: ship the thing; scope: X only"
            )
            with self.assertRaisesRegex(HelmError, "requirement gate .* is still waiting"):
                self.coordinator.create_task(project["id"], "ship the thing")

        self.coordinator.decide_gate(foreman_task["id"], "requirement", confirm=True, skip=False)

        with self._as_foreman(worker["id"]):
            with self.assertRaisesRegex(HelmError, "solution gate has not been proposed"):
                self.coordinator.create_task(project["id"], "ship the thing")

            self.coordinator.propose_gate(
                foreman_task["id"], "solution", "approach: do X; verification: tests"
            )
            with self.assertRaisesRegex(HelmError, "solution gate .* is still waiting"):
                self.coordinator.create_task(project["id"], "ship the thing")

        self.coordinator.decide_gate(foreman_task["id"], "solution", confirm=True, skip=False)

        with self._as_foreman(worker["id"]):
            task = self.coordinator.create_task(project["id"], "ship the thing")
        self.assertEqual(task["role"], "worker")

    def test_read_only_task_is_exempt_from_both_gates(self) -> None:
        project, foreman_task, worker = self._project_with_live_foreman("readonlygate")
        with self._as_foreman(worker["id"]):
            task = self.coordinator.create_task(
                project["id"], "just look around", read_only=True
            )
        self.assertTrue(task["read_only"])

    def test_no_live_foreman_refuses_a_foreman_caller(self) -> None:
        # A real foreman identity, but from a different project than the one
        # with no live foreman -- proves the check is per-project.
        _other, _other_task, worker = self._project_with_live_foreman("elsewhereforeman")
        root = self.repo("nolivegate")
        project = self.coordinator.register_project("NoLiveGate", str(root), project_id="nolivegate")
        with self._as_foreman(worker["id"]):
            with self.assertRaisesRegex(HelmError, "no live foreman for this project"):
                self.coordinator.create_task(project["id"], "do something")

    def test_root_caller_is_not_gated(self) -> None:
        """Root already carries full authority, same as every other foreman-only rule."""
        root = self.repo("rootbypass")
        project = self.coordinator.register_project("RootBypass", str(root), project_id="rootbypass")
        task = self.coordinator.create_task(project["id"], "root can just do it")
        self.assertEqual(task["role"], "worker")

    def test_skip_decision_also_clears_the_gate(self) -> None:
        project, foreman_task, worker = self._project_with_live_foreman("skipgate")
        with self._as_foreman(worker["id"]):
            self.coordinator.propose_gate(foreman_task["id"], "requirement", "goal: X")
        self.coordinator.decide_gate(foreman_task["id"], "requirement", confirm=False, skip=True, note="trivial")
        with self._as_foreman(worker["id"]):
            self.coordinator.propose_gate(foreman_task["id"], "solution", "approach: Y")
        self.coordinator.decide_gate(foreman_task["id"], "solution", confirm=False, skip=True)
        with self._as_foreman(worker["id"]):
            task = self.coordinator.create_task(project["id"], "do it")
        self.assertEqual(task["role"], "worker")

    def test_reproposing_requirement_invalidates_both_decisions(self) -> None:
        project, foreman_task, worker = self._project_with_live_foreman("materialchange")
        with self._as_foreman(worker["id"]):
            self.coordinator.propose_gate(foreman_task["id"], "requirement", "goal: A")
        self.coordinator.decide_gate(foreman_task["id"], "requirement", confirm=True, skip=False)
        with self._as_foreman(worker["id"]):
            self.coordinator.propose_gate(foreman_task["id"], "solution", "approach: A1")
        self.coordinator.decide_gate(foreman_task["id"], "solution", confirm=True, skip=False)

        with self._as_foreman(worker["id"]):
            # A material requirement change: propose again.
            self.coordinator.propose_gate(foreman_task["id"], "requirement", "goal: B, different scope")
            with self.assertRaisesRegex(HelmError, "requirement gate .* is still waiting"):
                self.coordinator.create_task(project["id"], "do B")

        inspected = self.coordinator.inspect_task(foreman_task["id"])["task"]
        self.assertIsNone(inspected["gates"]["solution"])

    def test_reproposing_solution_only_resets_solution(self) -> None:
        project, foreman_task, worker = self._project_with_live_foreman("solutiononly")
        with self._as_foreman(worker["id"]):
            self.coordinator.propose_gate(foreman_task["id"], "requirement", "goal: A")
        self.coordinator.decide_gate(foreman_task["id"], "requirement", confirm=True, skip=False)
        with self._as_foreman(worker["id"]):
            self.coordinator.propose_gate(foreman_task["id"], "solution", "approach: A1")
        self.coordinator.decide_gate(foreman_task["id"], "solution", confirm=True, skip=False)

        with self._as_foreman(worker["id"]):
            self.coordinator.propose_gate(foreman_task["id"], "solution", "approach: A2, different")

        inspected = self.coordinator.inspect_task(foreman_task["id"])["task"]
        self.assertTrue(
            inspected["gates"]["requirement"]["confirmed_at"]
            or inspected["gates"]["requirement"]["skipped"]
        )
        self.assertIsNone(inspected["gates"]["solution"]["confirmed_at"])

    def test_solution_cannot_be_proposed_before_requirement_decided(self) -> None:
        project, foreman_task, worker = self._project_with_live_foreman("orderedgates")
        with self._as_foreman(worker["id"]):
            self.coordinator.propose_gate(foreman_task["id"], "requirement", "goal: A")
            with self.assertRaisesRegex(HelmError, "requirement gate must be decided"):
                self.coordinator.propose_gate(foreman_task["id"], "solution", "approach: A1")

    def test_only_a_foreman_task_carries_gates(self) -> None:
        project = self.coordinator.register_project(
            "notforeman", str(self.repo("notforeman")), project_id="notforeman"
        )
        worker_task = self.coordinator.create_task(project["id"], "plain work")
        with self.assertRaisesRegex(HelmError, "only a project's foreman task"):
            self.coordinator.propose_gate(worker_task["id"], "requirement", "goal: A")
        with self.assertRaisesRegex(HelmError, "only a project's foreman task"):
            self.coordinator.decide_gate(worker_task["id"], "requirement", confirm=True, skip=False)

    def test_neither_worker_nor_foreman_can_decide_its_own_gate(self) -> None:
        project, foreman_task, worker = self._project_with_live_foreman("selfdecide")
        with self._as_foreman(worker["id"]):
            self.coordinator.propose_gate(foreman_task["id"], "requirement", "goal: A")
            with self.assertRaises(SafetyError):
                self.coordinator.decide_gate(foreman_task["id"], "requirement", confirm=True, skip=False)

    def test_cli_gate_propose_and_decide_round_trip(self) -> None:
        project, foreman_task, worker = self._project_with_live_foreman("cligate")
        argv = ["--state-dir", str(self.state.directory)]
        with self._as_foreman(worker["id"]):
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(
                    cli.main([*argv, "gate", "propose", foreman_task["id"],
                              "--type", "requirement", "--text", "goal: ship it"]),
                    0,
                )
            # A foreman cannot decide its own gate through the CLI either.
            with contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(
                    cli.main([*argv, "gate", "decide", foreman_task["id"],
                              "--type", "requirement", "--confirm"]),
                    2,
                )
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(
                cli.main([*argv, "gate", "decide", foreman_task["id"],
                          "--type", "requirement", "--confirm"]),
                0,
            )
        inspected = self.coordinator.inspect_task(foreman_task["id"])["task"]
        self.assertIsNotNone(inspected["gates"]["requirement"]["confirmed_at"])

    # ---------- read-only is structural, not a label ----------

    def test_read_only_workspace_cannot_be_written_to(self) -> None:
        root = self.repo("readonlywrite")
        project = self.coordinator.register_project(
            "ReadOnlyWrite", str(root), project_id="readonlywrite"
        )
        task = self.coordinator.create_task(
            project["id"], "just look around", read_only=True
        )
        allocated = self.coordinator.allocate_task(task["id"])
        workspace = Path(allocated["workspace"])
        target = workspace / "change.txt"
        with self.assertRaises(PermissionError):
            target.write_text("an attempted edit", encoding="utf-8")
        self.assertFalse(target.exists())
        with self.assertRaises(PermissionError):
            (workspace / "new-dir").mkdir()

    def test_read_only_worker_cannot_author_new_content_via_worktree_write(self) -> None:
        """A 'read-only' brief that asks to add-and-commit new content cannot succeed.

        This proves the worktree lock specifically: authoring content requires
        writing a tracked file, and that write fails at the filesystem. It does
        not by itself prove a commit can never land on the branch -- an
        index-only mutation (`git rm --cached`, `hash-object`/`update-index`)
        needs no worktree write and is not stopped by this lock. See
        `test_read_only_task_index_only_deletion_commit_is_refused_at_delivery`
        for that case, and why delivery itself refuses a read-only task
        regardless of how a commit was produced.
        """
        root = self.repo("readonlybypass")
        project = self.coordinator.register_project(
            "ReadOnlyBypass", str(root), project_id="readonlybypass"
        )
        task = self.coordinator.create_task(
            project["id"], "implement X and commit it", read_only=True
        )
        allocated = self.coordinator.allocate_task(task["id"])
        workspace = Path(allocated["workspace"])
        before = subprocess.run(
            ["git", "-C", str(workspace), "rev-parse", "HEAD"],
            capture_output=True, text=True, check=True,
        ).stdout.strip()
        write = subprocess.run(
            ["bash", "-c", f'echo hi > "{workspace}/change.txt"'],
            capture_output=True, text=True,
        )
        self.assertNotEqual(write.returncode, 0)
        add = subprocess.run(
            ["git", "-C", str(workspace), "add", "-A"], capture_output=True, text=True
        )
        commit = subprocess.run(
            ["git", "-C", str(workspace), "commit", "-qm", "sneak it in"],
            capture_output=True, text=True,
        )
        self.assertNotEqual(commit.returncode, 0)
        after = subprocess.run(
            ["git", "-C", str(workspace), "rev-parse", "HEAD"],
            capture_output=True, text=True, check=True,
        ).stdout.strip()
        self.assertEqual(before, after)
        self.assertTrue(self.coordinator._workspace_clean(workspace))

    def test_read_only_task_index_only_deletion_commit_is_refused_at_delivery(self) -> None:
        """The gap the worktree lock cannot close: `git rm --cached` + commit.

        The index and object store for a linked worktree live outside it
        (under the project's `.git/worktrees/<branch>`), so an index-only
        removal needs no worktree write and the lock never engages -- the
        commit genuinely lands on the branch. `approve_task`/`merge_task`
        close this: a read-only task is refused at delivery regardless of how
        a commit got there, so the gap in the write-lock can never become a
        deliverable outcome.
        """
        root = self.repo("readonlyindexdelete")
        project = self.coordinator.register_project(
            "ReadOnlyIndexDelete", str(root), project_id="readonlyindexdelete"
        )
        # Something tracked to delete via the index only.
        subprocess.run(
            ["bash", "-c", f'echo seed > "{root}/seed.txt"'], check=True
        )
        subprocess.run(["git", "-C", str(root), "add", "seed.txt"], check=True)
        subprocess.run(
            ["git", "-C", str(root), "commit", "-qm", "seed"], check=True
        )
        task = self.coordinator.create_task(
            project["id"], "just look around", read_only=True
        )
        allocated = self.coordinator.allocate_task(task["id"])
        workspace = Path(allocated["workspace"])
        before = subprocess.run(
            ["git", "-C", str(workspace), "rev-parse", "HEAD"],
            capture_output=True, text=True, check=True,
        ).stdout.strip()

        rm_cached = subprocess.run(
            ["git", "-C", str(workspace), "rm", "--cached", "seed.txt"],
            capture_output=True, text=True,
        )
        self.assertEqual(rm_cached.returncode, 0, rm_cached.stderr)
        commit = subprocess.run(
            ["git", "-C", str(workspace), "commit", "-qm", "index-only removal"],
            capture_output=True, text=True,
        )
        self.assertEqual(commit.returncode, 0, commit.stderr)
        after = subprocess.run(
            ["git", "-C", str(workspace), "rev-parse", "HEAD"],
            capture_output=True, text=True, check=True,
        ).stdout.strip()
        # The commit genuinely landed: this is the gap the worktree lock does
        # not close on its own.
        self.assertNotEqual(before, after)

        worker = self.coordinator.launch_worker(task["id"], [sys.executable, "-c", ""])
        self.coordinator.record_worker_message(
            worker["id"], "result", "done", requested_status="completed"
        )
        with self.assertRaisesRegex(SafetyError, "read-only.*cannot be approved"):
            self.coordinator.approve_task(task["id"], "looks fine")

    def test_cleanup_of_a_read_only_task_still_removes_its_locked_workspace(self) -> None:
        root = self.repo("readonlycleanup")
        project = self.coordinator.register_project(
            "ReadOnlyCleanup", str(root), project_id="readonlycleanup"
        )
        task = self.coordinator.create_task(
            project["id"], "just look around", read_only=True
        )
        worker = self.coordinator.launch_worker(task["id"], [sys.executable, "-c", ""])
        self.coordinator.record_worker_message(
            worker["id"], "result", "nothing to change", requested_status="completed"
        )
        cleaned = self.coordinator.cleanup_task(task["id"])
        self.assertTrue(cleaned["workspace_removed"])

    # ---------- continuing a task requires an explicit round classification ----------

    def _completed_worker_task(self, name: str) -> dict:
        root = self.repo(name)
        project = self.coordinator.register_project(name, str(root), project_id=name)
        task = self.coordinator.create_task(project["id"], "write it")
        worker = self.coordinator.launch_worker(
            task["id"], [sys.executable, "-c", ""], wait=False
        )
        self.coordinator.record_worker_message(
            worker["id"], "result", "done", requested_status="completed"
        )
        return task

    def test_continuing_read_only_locks_the_workspace(self) -> None:
        task = self._completed_worker_task("continuereadonly")
        reopened = self.coordinator.continue_task(
            task["id"], "double check the numbers", read_only=True
        )
        self.assertTrue(reopened["read_only"])
        workspace = Path(reopened["workspace"])
        with self.assertRaises(PermissionError):
            (workspace / "sneaky.txt").write_text("nope", encoding="utf-8")

    def test_continuing_state_changing_unlocks_a_previously_read_only_task(self) -> None:
        task = self._completed_worker_task("continueunlock")
        reopened = self.coordinator.continue_task(
            task["id"], "just double check", read_only=True
        )
        workspace = Path(reopened["workspace"])
        with self.assertRaises(PermissionError):
            (workspace / "blocked.txt").write_text("nope", encoding="utf-8")

        # The read-only round has to finish before another round can start;
        # nothing writable happened, so it completes with no diff.
        worker = self.coordinator.launch_worker(task["id"], [sys.executable, "-c", ""])
        self.coordinator.record_worker_message(
            worker["id"], "result", "confirmed", requested_status="completed"
        )

        unlocked = self.coordinator.continue_task(
            task["id"], "actually fix it now", read_only=False
        )
        self.assertFalse(unlocked["read_only"])
        (workspace / "allowed.txt").write_text("now writable", encoding="utf-8")
        self.assertTrue((workspace / "allowed.txt").exists())

    def test_a_completed_read_only_round_cannot_be_silently_continued_as_state_changing(
        self,
    ) -> None:
        """Reproduces the exact bypass: finish a read-only round, then continue it
        with a state-changing brief while a foreman is driving without decided
        gates. It must be refused, not silently accepted with read_only left True.
        """
        root = self.repo("readonlybypasscontinue")
        project = self.coordinator.register_project(
            "ReadOnlyBypassContinue", str(root), project_id="readonlybypasscontinue"
        )
        foreman_task = self.coordinator.create_foreman_task(project["id"])
        foreman_worker = self.coordinator.prepare_external_worker(
            foreman_task["id"], [sys.executable, "-c", ""], execution="external"
        )
        with self._as_foreman(foreman_worker["id"]):
            task = self.coordinator.create_task(
                project["id"], "look around first", read_only=True
            )
        worker = self.coordinator.launch_worker(
            task["id"], [sys.executable, "-c", ""], wait=False
        )
        self.coordinator.record_worker_message(
            worker["id"], "result", "looked around", requested_status="completed"
        )

        with self._as_foreman(foreman_worker["id"]):
            with self.assertRaisesRegex(HelmError, "requirement gate has not been proposed"):
                self.coordinator.continue_task(
                    task["id"], "rewrite the module and commit", read_only=False
                )
        # Refused: the task must not have been silently reclassified.
        still = self.coordinator.inspect_task(task["id"])["task"]
        self.assertTrue(still["read_only"])

    def test_continue_defaults_to_state_changing_not_inherited_read_only(self) -> None:
        """The library default is the gated, safer reading -- never 'whatever it was.'"""
        task = self._completed_worker_task("continuedefault")
        reopened = self.coordinator.continue_task(task["id"], "one more change")
        self.assertFalse(reopened["read_only"])

    def test_cli_continue_requires_an_explicit_round_kind(self) -> None:
        task = self._completed_worker_task("clicontinuekind")
        argv = ["--state-dir", str(self.state.directory)]
        with contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit) as raised:
                cli.main([*argv, "task", "continue", task["id"], "--brief", "again"])
        self.assertEqual(raised.exception.code, 2)

    # ---------- a long contract stays visible on the commander's decision list ----------

    def test_a_long_gate_proposal_still_becomes_a_visible_bounded_decision(self) -> None:
        project, foreman_task, worker = self._project_with_live_foreman("longcontract")
        long_text = (
            "goal: ship the redesigned onboarding flow end to end; "
            "scope: covers the signup form, email verification, and the first "
            "dashboard render, explicitly excluding billing and the mobile app; "
            "exclusions: no changes to the payments service or the marketing site; "
            "acceptance evidence: a passing end-to-end test recording, a screenshot "
            "of the finished dashboard, and a link to the staging deploy that "
            "reviewers can click through themselves without any special setup steps. "
        ) * 4
        self.assertGreater(len(long_text), 800)
        with self._as_foreman(worker["id"]):
            self.coordinator.propose_gate(foreman_task["id"], "requirement", long_text)

        pending = self.coordinator.open_action_items(project["id"])
        matching = [item for item in pending if item.get("task_id") == foreman_task["id"]]
        self.assertEqual(len(matching), 1)
        self.assertLessEqual(len(matching[0]["text"]), 800)
        self.assertIn("requirement gate", matching[0]["text"])

    def test_gate_decision_stays_visible_at_the_default_situation_line_limit(self) -> None:
        """The bound applied is exactly the project's own SITUATION_LINE_LIMIT."""
        from helm.core import Coordinator

        project, foreman_task, worker = self._project_with_live_foreman("boundarycheck")
        text = "x" * (Coordinator.SITUATION_LINE_LIMIT + 200)
        with self._as_foreman(worker["id"]):
            self.coordinator.propose_gate(foreman_task["id"], "requirement", text)
        pending = self.coordinator.open_action_items(project["id"])
        matching = [item for item in pending if item.get("task_id") == foreman_task["id"]]
        self.assertEqual(len(matching), 1)
        self.assertLessEqual(len(matching[0]["text"]), Coordinator.SITUATION_LINE_LIMIT)

    # ---------- a gate proposal's action item records when it was closed ----------

    def test_deciding_a_gate_stamps_resolved_at_on_its_action_item(self) -> None:
        project, foreman_task, worker = self._project_with_live_foreman("resolvedat")
        with self._as_foreman(worker["id"]):
            self.coordinator.propose_gate(foreman_task["id"], "requirement", "goal: A")
        raw = self.coordinator._load_status(project["id"])
        [item] = [
            i for i in raw["action_items"] if i.get("task_id") == foreman_task["id"]
        ]
        self.assertIsNone(item.get("resolved_at"))

        self.coordinator.decide_gate(foreman_task["id"], "requirement", confirm=True, skip=False)

        raw = self.coordinator._load_status(project["id"])
        [decided] = [
            i for i in raw["action_items"] if i.get("task_id") == foreman_task["id"]
        ]
        self.assertEqual(decided["status"], "confirmed")
        self.assertIsNotNone(decided.get("resolved_at"))

    def test_reproposing_a_gate_stamps_resolved_at_on_the_stale_item(self) -> None:
        project, foreman_task, worker = self._project_with_live_foreman("resolvedatrepropose")
        with self._as_foreman(worker["id"]):
            self.coordinator.propose_gate(foreman_task["id"], "requirement", "goal: A")
            self.coordinator.propose_gate(foreman_task["id"], "requirement", "goal: B")
        raw = self.coordinator._load_status(project["id"])
        closed = [
            i for i in raw["action_items"]
            if i.get("task_id") == foreman_task["id"] and i.get("status") != "open"
        ]
        self.assertEqual(len(closed), 1)
        self.assertEqual(closed[0]["status"], "re-proposed")
        self.assertIsNotNone(closed[0].get("resolved_at"))

    # ---------- propose_gate enforces strict project isolation ----------

    def test_a_foreman_cannot_propose_gates_on_another_projects_foreman_task(self) -> None:
        _own_project, own_task, own_worker = self._project_with_live_foreman("ownproject")
        other_project, other_task, _other_worker = self._project_with_live_foreman(
            "otherproject"
        )
        with self._as_foreman(own_worker["id"]):
            with self.assertRaisesRegex(SafetyError, "only propose gates for the project it drives"):
                self.coordinator.propose_gate(other_task["id"], "requirement", "goal: hijack")
        # Refused, not merely unconfirmed: no gate was written at all.
        inspected = self.coordinator.inspect_task(other_task["id"])["task"]
        self.assertIsNone(inspected["gates"]["requirement"])

    def test_root_may_still_propose_gates_on_any_project(self) -> None:
        """Root already carries full authority; the isolation check is caller-scoped."""
        _project, foreman_task, _worker = self._project_with_live_foreman("rootproposescope")
        task = self.coordinator.propose_gate(foreman_task["id"], "requirement", "goal: root did this")
        self.assertEqual(task["gates"]["requirement"]["text"], "goal: root did this")

    # ---------- a read-only task states its own restriction in its own context ----------

    def test_read_only_tasks_context_states_the_restriction_explicitly(self) -> None:
        root = self.repo("readonlycontext")
        project = self.coordinator.register_project(
            "ReadOnlyContext", str(root), project_id="readonlycontext"
        )
        task = self.coordinator.create_task(
            project["id"], "just look around", read_only=True
        )
        context = self.coordinator._context(project, task, "worker")
        kinds = {section["kind"] for section in context["context_sections"]}
        self.assertIn("read-only", kinds)
        [section] = [s for s in context["context_sections"] if s["kind"] == "read-only"]
        self.assertIn("permission error", section["content"])
        self.assertIn("read-only", context["precedence"])
        task_section = next(s for s in context["context_sections"] if s["kind"] == "task")
        self.assertTrue(json.loads(task_section["content"])["read_only"])

    def test_state_changing_tasks_context_has_no_read_only_section(self) -> None:
        root = self.repo("statechangingcontext")
        project = self.coordinator.register_project(
            "StateChangingContext", str(root), project_id="statechangingcontext"
        )
        task = self.coordinator.create_task(project["id"], "make the change")
        context = self.coordinator._context(project, task, "worker")
        kinds = {section["kind"] for section in context["context_sections"]}
        self.assertNotIn("read-only", kinds)
        task_section = next(s for s in context["context_sections"] if s["kind"] == "task")
        self.assertFalse(json.loads(task_section["content"])["read_only"])

    # ---------- a read-only worker's death gets a specific diagnostic ----------

    def test_a_dead_read_only_worker_gets_a_specific_permission_hint(self) -> None:
        root = self.repo("readonlydiagnostic")
        project = self.coordinator.register_project(
            "ReadOnlyDiagnostic", str(root), project_id="readonlydiagnostic"
        )
        task = self.coordinator.create_task(
            project["id"], "just look around", read_only=True
        )
        worker = self.coordinator.launch_worker(
            task["id"], [sys.executable, "-c", "import time; time.sleep(120)"], wait=False
        )
        # Died mid-task with no exit record -- exactly what a runtime that
        # tries to write a settings/cache directory into a locked worktree on
        # startup looks like from Helm's side.
        os.kill(worker["pid"], signal.SIGKILL)
        os.waitpid(worker["pid"], 0)
        Path(worker["exit_file"]).unlink(missing_ok=True)

        health = self.coordinator.worker_health()
        [entry] = [h for h in health if h["worker_id"] == worker["id"]]
        self.assertEqual(entry["verdict"], "died")
        self.assertIn("read-only", entry["detail"])
        self.assertIn("permission", entry["detail"])

    def test_a_dead_state_changing_worker_gets_no_read_only_hint(self) -> None:
        root = self.repo("statechangingdiagnostic")
        project = self.coordinator.register_project(
            "StateChangingDiagnostic", str(root), project_id="statechangingdiagnostic"
        )
        task = self.coordinator.create_task(project["id"], "edit a file")
        worker = self.coordinator.launch_worker(
            task["id"], [sys.executable, "-c", "import time; time.sleep(120)"], wait=False
        )
        os.kill(worker["pid"], signal.SIGKILL)
        os.waitpid(worker["pid"], 0)
        Path(worker["exit_file"]).unlink(missing_ok=True)

        health = self.coordinator.worker_health()
        [entry] = [h for h in health if h["worker_id"] == worker["id"]]
        self.assertEqual(entry["verdict"], "died")
        self.assertNotIn("read-only", entry["detail"])

    # ---------- precedence only lists a section that actually exists ----------

    def test_precedence_omits_read_only_when_the_task_is_state_changing(self) -> None:
        root = self.repo("precedencestatechanging")
        project = self.coordinator.register_project(
            "PrecedenceStateChanging", str(root), project_id="precedencestatechanging"
        )
        task = self.coordinator.create_task(project["id"], "make the change")
        context = self.coordinator._context(project, task, "worker")
        self.assertNotIn("read-only", context["precedence"])
        self.assertNotIn(
            "read-only", [section["kind"] for section in context["context_sections"]]
        )
        self.assertEqual(context["precedence"][-1], "task")

    def test_precedence_includes_read_only_only_when_the_section_exists(self) -> None:
        root = self.repo("precedencereadonly")
        project = self.coordinator.register_project(
            "PrecedenceReadOnly", str(root), project_id="precedencereadonly"
        )
        task = self.coordinator.create_task(
            project["id"], "just look around", read_only=True
        )
        context = self.coordinator._context(project, task, "worker")
        self.assertIn("read-only", context["precedence"])
        self.assertEqual(context["precedence"][-1], "read-only")
        self.assertIn(
            "read-only", [section["kind"] for section in context["context_sections"]]
        )

    # ---------- the workspace lock preserves every non-write permission bit ----------

    def test_locking_and_unlocking_round_trips_an_owner_private_file_mode(self) -> None:
        """0600 (owner-only, no exec) must come back as 0600, not 0644."""
        workspace = Path(self.temp.name) / "modeprivate-workspace"
        workspace.mkdir()
        secret = workspace / "secret.txt"
        secret.write_text("shh", encoding="utf-8")
        os.chmod(secret, 0o600)

        self.coordinator._set_workspace_writable(workspace, writable=False)
        locked_mode = os.stat(secret).st_mode & 0o777
        self.assertEqual(locked_mode, 0o400)

        self.coordinator._set_workspace_writable(workspace, writable=True)
        restored_mode = os.stat(secret).st_mode & 0o777
        self.assertEqual(restored_mode, 0o600)

    def test_locking_and_unlocking_round_trips_an_executable_file_mode(self) -> None:
        """0755 (owner/group/other exec) must keep its exec bits on both sides."""
        workspace = Path(self.temp.name) / "modeexec-workspace"
        workspace.mkdir()
        script = workspace / "run.sh"
        script.write_text("#!/bin/sh\necho hi\n", encoding="utf-8")
        os.chmod(script, 0o755)

        self.coordinator._set_workspace_writable(workspace, writable=False)
        locked_mode = os.stat(script).st_mode & 0o777
        self.assertEqual(locked_mode, 0o555)

        self.coordinator._set_workspace_writable(workspace, writable=True)
        restored_mode = os.stat(script).st_mode & 0o777
        self.assertEqual(restored_mode, 0o755)

    # ---------- read-only refusal reaches every delivery path ----------

    def _read_only_allocated_task(self, name: str) -> dict:
        root = self.repo(name)
        project = self.coordinator.register_project(name, str(root), project_id=name)
        task = self.coordinator.create_task(
            project["id"], "just look around", read_only=True
        )
        self.coordinator.allocate_task(task["id"])
        return task

    def test_deliver_task_artifacts_refuses_a_read_only_task(self) -> None:
        task = self._read_only_allocated_task("readonlydeliverartifacts")
        with self.assertRaisesRegex(SafetyError, "read-only.*cannot be delivered"):
            self.coordinator.deliver_task_artifacts(task["id"])

    def test_record_pr_opened_refuses_a_read_only_task(self) -> None:
        task = self._read_only_allocated_task("readonlyrecordpropened")
        with self.assertRaisesRegex(SafetyError, "read-only.*cannot be recorded as a PR"):
            self.coordinator.record_pr_opened(task["id"], "https://example.invalid/pr/1")

    def test_record_pr_status_refuses_a_read_only_task(self) -> None:
        task = self._read_only_allocated_task("readonlyrecordprstatus")
        with self.assertRaisesRegex(SafetyError, "read-only.*cannot be recorded as a PR"):
            self.coordinator.record_pr_status(task["id"], state="merged")

    def test_cli_publish_refuses_a_read_only_task(self) -> None:
        task = self._read_only_allocated_task("clireadonlypublish")
        argv = ["--state-dir", str(self.state.directory)]
        with contextlib.redirect_stderr(io.StringIO()) as captured:
            exit_code = cli.main([*argv, "task", "pr", task["id"], "--confirm", "--no-open"])
        self.assertNotEqual(exit_code, 0)
        self.assertIn("cannot be published", captured.getvalue())

    # ---------- a confirmed pair authorizes exactly one new task ----------

    def _confirm_both_gates(self, foreman_task: dict, worker: dict, tag: str) -> None:
        with self._as_foreman(worker["id"]):
            self.coordinator.propose_gate(foreman_task["id"], "requirement", f"goal: {tag}")
        self.coordinator.decide_gate(foreman_task["id"], "requirement", confirm=True, skip=False)
        with self._as_foreman(worker["id"]):
            self.coordinator.propose_gate(foreman_task["id"], "solution", f"approach: {tag}")
        self.coordinator.decide_gate(foreman_task["id"], "solution", confirm=True, skip=False)

    def test_a_confirmed_pair_authorizes_only_one_new_state_changing_task(self) -> None:
        project, foreman_task, worker = self._project_with_live_foreman("onebindperpair")
        self._confirm_both_gates(foreman_task, worker, "A")

        with self._as_foreman(worker["id"]):
            first = self.coordinator.create_task(project["id"], "do A")
            with self.assertRaisesRegex(HelmError, "already authorized task"):
                self.coordinator.create_task(project["id"], "do something else")

        inspected = self.coordinator.inspect_task(foreman_task["id"])["task"]
        self.assertEqual(inspected["gates"]["bound_task_id"], first["id"])

    def test_reproposing_a_gate_frees_it_for_a_second_task(self) -> None:
        project, foreman_task, worker = self._project_with_live_foreman("rebindperpair")
        self._confirm_both_gates(foreman_task, worker, "A")
        with self._as_foreman(worker["id"]):
            self.coordinator.create_task(project["id"], "do A")

        # A material change: re-propose and reconfirm both gates.
        self._confirm_both_gates(foreman_task, worker, "B")
        with self._as_foreman(worker["id"]):
            second = self.coordinator.create_task(project["id"], "do B")
        self.assertEqual(second["role"], "worker")

    def test_continuing_the_bound_task_does_not_reprompt_for_confirmation(self) -> None:
        project, foreman_task, worker = self._project_with_live_foreman("continuebound")
        self._confirm_both_gates(foreman_task, worker, "A")
        with self._as_foreman(worker["id"]):
            task = self.coordinator.create_task(project["id"], "do A")
        launched = self.coordinator.launch_worker(
            task["id"], [sys.executable, "-c", ""], wait=False
        )
        self.coordinator.record_worker_message(
            launched["id"], "result", "done", requested_status="completed"
        )

        # The same task's own continuation is not a *new* authorization, so
        # this must succeed even though the pair was already spent above and
        # nothing was re-proposed.
        with self._as_foreman(worker["id"]):
            reopened = self.coordinator.continue_task(
                task["id"], "one more pass on A", read_only=False
            )
        self.assertFalse(reopened["read_only"])

        # But a second, different task still cannot ride on the same pair.
        with self._as_foreman(worker["id"]):
            with self.assertRaisesRegex(HelmError, "already authorized task"):
                self.coordinator.create_task(project["id"], "do something unrelated")

    def test_a_failed_launch_still_leaves_an_auditable_binding(self) -> None:
        """The pair is spent at task creation, not at a successful launch, so a
        launch that never starts (or dies immediately) does not leave the
        authorization looking unspent while also being unusable -- the task
        that spent it is on the record either way."""
        project, foreman_task, worker = self._project_with_live_foreman("auditedbinding")
        self._confirm_both_gates(foreman_task, worker, "A")
        with self._as_foreman(worker["id"]):
            task = self.coordinator.create_task(project["id"], "do A")

        inspected = self.coordinator.inspect_task(foreman_task["id"])["task"]
        self.assertEqual(inspected["gates"]["bound_task_id"], task["id"])

        # A second task is refused, whether or not the first task's worker
        # ever launched successfully -- the binding is on the task record,
        # not on a launch outcome.
        with self._as_foreman(worker["id"]):
            with self.assertRaisesRegex(HelmError, "already authorized task"):
                self.coordinator.create_task(project["id"], "do A again after a bad launch")


class GatesSurviveAForemanRestartTests(HelmTestCase):
    """A confirmed decision belongs to the WORK, not to whoever is driving it.

    Gates were recorded on one foreman task row and read from the project's
    live foreman, so replacing the foreman discarded the commander's decision.
    The successor came up with empty gates and could not reach the solution
    gate for want of a decided requirement. The cost is not the second
    keystroke -- it is that the loss is invisible, because a fresh foreman with
    empty gates looks exactly like one that has not proposed yet.
    """

    def _project_with_live_foreman(self, name: str) -> tuple[dict, dict, dict]:
        root = self.repo(name)
        project = self.coordinator.register_project(name, str(root), project_id=name)
        foreman_task = self.coordinator.create_foreman_task(project["id"])
        worker = self.coordinator.prepare_external_worker(
            foreman_task["id"], [sys.executable, "-c", ""], execution="external"
        )
        return project, foreman_task, worker

    def _as_foreman(self, worker_id: str):
        return mock.patch.dict(os.environ, {"HELM_WORKER_ID": worker_id})

    def _retire_foreman(self, foreman_task: dict) -> None:
        data = self.coordinator.store.load()
        data["tasks"][foreman_task["id"]]["status"] = "completed"
        for worker in data["workers"].values():
            if worker["task_id"] == foreman_task["id"]:
                worker["status"] = "completed"
        self.coordinator.store.save(data)

    def test_a_decided_requirement_survives_the_foreman_that_carried_it(self) -> None:
        project, foreman_task, worker = self._project_with_live_foreman("carried")
        with self._as_foreman(worker["id"]):
            self.coordinator.propose_gate(
                foreman_task["id"], "requirement", "goal: ship it; scope: X only"
            )
        self.coordinator.decide_gate(
            foreman_task["id"], "requirement", confirm=True, skip=False
        )
        self._retire_foreman(foreman_task)

        successor = self.coordinator.create_foreman_task(project["id"])
        carried = successor["gates"]["requirement"]
        self.assertIsNotNone(carried, "the commander's decision must not die with a driver")
        self.assertIsNotNone(carried["confirmed_at"])
        self.assertEqual(successor["gates"]["bound_task_id"], None)

        # And the successor can now reach the solution gate, which is the thing
        # that was actually blocked.
        replacement = self.coordinator.prepare_external_worker(
            successor["id"], [sys.executable, "-c", ""], execution="external"
        )
        with self._as_foreman(replacement["id"]):
            self.coordinator.propose_gate(
                successor["id"], "solution", "approach: do X; verification: tests"
            )

    def test_a_SPENT_pair_is_not_carried_to_a_new_foreman(self) -> None:
        """Otherwise one confirmation buys two state-changing tasks."""
        project, foreman_task, worker = self._project_with_live_foreman("spent")
        with self._as_foreman(worker["id"]):
            self.coordinator.propose_gate(foreman_task["id"], "requirement", "goal: X")
        self.coordinator.decide_gate(
            foreman_task["id"], "requirement", confirm=True, skip=False
        )
        with self._as_foreman(worker["id"]):
            self.coordinator.propose_gate(foreman_task["id"], "solution", "approach: X")
        self.coordinator.decide_gate(
            foreman_task["id"], "solution", confirm=True, skip=False
        )
        with self._as_foreman(worker["id"]):
            spent_on = self.coordinator.create_task(project["id"], "the one authorized task")
        self.assertTrue(spent_on["id"])
        self._retire_foreman(foreman_task)

        successor = self.coordinator.create_foreman_task(project["id"])
        self.assertIsNone(
            successor["gates"]["requirement"],
            "a spent authorization must not be reissued by replacing the foreman",
        )
        self.assertIsNone(successor["gates"]["solution"])

    def test_an_undecided_proposal_is_not_carried(self) -> None:
        """Only a COMMANDER DECISION is worth preserving; a draft is not."""
        project, foreman_task, worker = self._project_with_live_foreman("undecided")
        with self._as_foreman(worker["id"]):
            self.coordinator.propose_gate(foreman_task["id"], "requirement", "goal: X")
        self._retire_foreman(foreman_task)

        successor = self.coordinator.create_foreman_task(project["id"])
        self.assertIsNone(successor["gates"]["requirement"])
