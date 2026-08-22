"""The worker/task lifecycle state machine: convergence under event ordering.

The contract these assert is written down in `docs/worker-lifecycle.md`. Every
test here applies the same event set in both orders and compares the *whole*
settled state -- worker record, task status, hold, messages -- rather than the
task status alone, because the bug this suite exists for was two orders that
agreed on one field and disagreed on the rest.
"""

from __future__ import annotations

import json
import os
import signal
import sys
import threading
import time
from pathlib import Path

from typing import Any
from unittest import mock

from helm import core
from helm.core import Coordinator, HelmError, StateStore

from tests.support import HelmTestCase


class WorkerLifecycleConvergenceTests(HelmTestCase):
    def _worker(self, name: str) -> tuple[dict, dict, dict]:
        """A project, task and a worker record, with no process behind it.

        Every ordering permutation here is about *when Helm learns* the two
        facts, not about a real race, so the exit record is written by the
        test. An external worker gives exactly that: `poll_worker` reads its
        exit file normally, and no runner is spawned to be left behind. The two
        tests that genuinely need a live process say so themselves.
        """
        root = self.repo(name)
        project = self.coordinator.register_project(
            name.title(), str(root), project_id=name
        )
        task = self.coordinator.create_task(project["id"], f"{name} work")
        worker = self.coordinator.prepare_external_worker(
            task["id"], [sys.executable, "-c", ""]
        )
        return project, task, worker

    def _local_worker(self, name: str) -> tuple[dict, dict, dict]:
        """The same, with a real lingering runner Helm owns.

        Only for the two cases about a process as such: a pid that vanished,
        and what `wait_worker` does while a settled session is still open.
        """
        root = self.repo(name)
        project = self.coordinator.register_project(
            name.title(), str(root), project_id=name
        )
        task = self.coordinator.create_task(project["id"], f"{name} work")
        worker = self.coordinator.launch_worker(
            task["id"], [sys.executable, "-c", "import time; time.sleep(120)"], wait=False
        )
        self.addCleanup(self._kill, worker.get("pid"))
        Path(worker["exit_file"]).unlink(missing_ok=True)
        return project, task, worker

    @staticmethod
    def _kill(pid: int | None, *, wait: bool = True) -> None:
        """Kill the runner *and* the command it is hosting.

        Killing the runner alone orphans its child, which is how a suite ends
        up with a pile of sleeping processes outliving it. The runner leads its
        own process group, so the group is what gets signalled.
        """
        if not pid:
            return
        pid = int(pid)
        for send in (
            lambda: os.killpg(os.getpgid(pid), signal.SIGKILL),
            lambda: os.kill(pid, signal.SIGKILL),
        ):
            try:
                send()
            except (ProcessLookupError, PermissionError, OSError):
                continue
            break
        if not wait:
            return
        try:
            os.waitpid(pid, 0)
        except (ProcessLookupError, ChildProcessError, OSError):
            pass

    def _exit(self, worker: dict, code: int) -> None:
        Path(worker["exit_file"]).write_text(
            json.dumps({"returncode": code}) + "\n", encoding="utf-8"
        )

    def _kinds(self, task_id: str, *, fallback: bool = False) -> dict[str, int]:
        """Message kinds, by default excluding the process fallback's own.

        Superseding an outcome does not erase the history of having held it, so
        an exit-first ordering legitimately keeps the fallback record of the
        exit it acted on. Those carry `source: process-fallback`, which is what
        makes them separable here instead of a silent difference.
        """
        counts: dict[str, int] = {}
        for message in self.coordinator.inspect_task(task_id)["messages"]:
            is_fallback = message.get("payload", {}).get("source") == "process-fallback"
            if is_fallback is not fallback:
                continue
            counts[message["kind"]] = counts.get(message["kind"], 0) + 1
        return counts

    def _state(self, task_id: str) -> dict:
        """Everything the contract says both orders must agree on."""
        inspected = self.coordinator.inspect_task(task_id)
        worker = inspected["workers"][0]
        task = inspected["task"]
        return {
            "task_status": task["status"],
            "worker_status": worker["status"],
            "exit_code": worker["exit_code"],
            "protocol_outcome": worker.get("protocol_outcome"),
            "outcome_source": worker.get("outcome_source"),
            "exit_observed": bool(worker.get("exit_observed")),
            "process_exit_code": worker.get("process_exit_code"),
            "holds": [h["status"] for h in task.get("holds", [])],
            "kinds": self._kinds(task_id),
        }

    # ---------- the six required permutations ----------

    def _converges(
        self, name: str, kind: str, code: int, expected_task: str
    ) -> dict:
        """The same terminal message and the same exit, in both orders."""
        _, first_task, first_worker = self._worker(f"{name}-protocol-first")
        payload = {"action": "publish"} if kind == "approval-needed" else {}
        self.coordinator.record_worker_message(
            first_worker["id"], kind, "the worker's own word", payload=payload
        )
        self._exit(first_worker, code)
        self.coordinator.poll_worker(first_worker["id"])
        protocol_first = self._state(first_task["id"])

        _, second_task, second_worker = self._worker(f"{name}-exit-first")
        self._exit(second_worker, code)
        self.coordinator.poll_worker(second_worker["id"])
        self.coordinator.record_worker_message(
            second_worker["id"], kind, "the worker's own word", payload=payload
        )
        exit_first = self._state(second_task["id"])

        self.assertEqual(protocol_first, exit_first)
        self.assertEqual(protocol_first["task_status"], expected_task)
        # The one difference the orders may leave: the exit-first run acted on
        # the exit before the worker's word arrived, and says so.
        self.assertEqual(self._kinds(first_task["id"], fallback=True), {})
        self.assertTrue(self._kinds(second_task["id"], fallback=True))
        return protocol_first

    def test_a_result_and_a_nonzero_exit_converge_on_completed(self) -> None:
        state = self._converges("result", "result", 1, "completed")
        # The worker's word decides; the return code stands beside it as
        # evidence rather than turning a completed task into a failed one.
        self.assertEqual(state["worker_status"], "completed")
        self.assertEqual(state["exit_code"], 0)
        self.assertEqual(state["protocol_outcome"], "result")
        self.assertEqual(state["outcome_source"], "protocol")
        self.assertTrue(state["exit_observed"])
        self.assertEqual(state["process_exit_code"], 1)
        self.assertEqual(state["kinds"].get("exit-evidence"), 1)

    def test_a_blocker_and_a_zero_exit_converge_on_blocked(self) -> None:
        state = self._converges("blocker", "blocker", 0, "blocked")
        self.assertEqual(state["worker_status"], "failed")
        self.assertEqual(state["exit_code"], 1)
        self.assertEqual(state["protocol_outcome"], "blocker")
        self.assertEqual(state["process_exit_code"], 0)
        self.assertEqual(state["kinds"].get("exit-evidence"), 1)
        # An orderly exit must not be read back as success the worker never
        # claimed -- this is the defect the state machine was written for.
        self.assertEqual(state["kinds"].get("result"), None)

    def test_a_failure_and_a_zero_exit_converge_on_failed(self) -> None:
        state = self._converges("failure", "failure", 0, "failed")
        self.assertEqual(state["worker_status"], "failed")
        self.assertEqual(state["protocol_outcome"], "failure")
        self.assertEqual(state["process_exit_code"], 0)
        self.assertEqual(state["kinds"].get("exit-evidence"), 1)

    def test_approval_needed_and_an_exit_converge_on_an_abandoned_hold(self) -> None:
        state = self._converges("hold", "approval-needed", 0, "failed")
        # A dead session cannot spend an authorization, so the hold is let go
        # of -- explicitly, with its reason recorded, in either order.
        self.assertEqual(state["holds"], ["abandoned"])
        self.assertEqual(state["protocol_outcome"], None)
        self.assertEqual(state["kinds"].get("approval-abandoned"), 1)
        self.assertEqual(state["kinds"].get("approval-needed"), 1)

    def test_an_abandoned_hold_leaves_no_stale_commander_action(self) -> None:
        """In both orders. The item is keyed to the hold, so it must close."""
        for order in ("approval-first", "exit-first"):
            with self.subTest(order=order):
                project, task, worker = self._worker(f"attention-{order}")
                if order == "approval-first":
                    self.coordinator.record_worker_message(
                        worker["id"], "approval-needed", "publish the build",
                        payload={"action": "publish"},
                    )
                    opened = self.coordinator.project_status(project["id"])
                    self.assertTrue([
                        i for i in opened["action_items"]
                        if "Authorize or refuse" in i["text"]
                    ])
                    self._exit(worker, 0)
                    self.coordinator.poll_worker(worker["id"])
                else:
                    self._exit(worker, 0)
                    self.coordinator.poll_worker(worker["id"])
                    self.coordinator.record_worker_message(
                        worker["id"], "approval-needed", "publish the build",
                        payload={"action": "publish"},
                    )
                settled = self.coordinator.project_status(project["id"])
                self.assertEqual(
                    [i for i in settled["action_items"]
                     if "Authorize or refuse" in i["text"]],
                    [],
                )
                self.assertEqual(
                    self.coordinator.inspect_task(task["id"])["task"]["status"], "failed"
                )

    def test_exit_alone_still_settles_the_task_either_way(self) -> None:
        for code, expected in ((0, "completed"), (3, "failed")):
            with self.subTest(code=code):
                _, task, worker = self._worker(f"exit-only-{code}")
                self._exit(worker, code)
                self.coordinator.poll_worker(worker["id"])
                state = self._state(task["id"])
                self.assertEqual(state["task_status"], expected)
                self.assertEqual(state["exit_code"], code)
                # The fallback synthesizes the message, but it is never the
                # worker's word: nothing may read it back as a protocol outcome.
                self.assertIsNone(state["protocol_outcome"])
                self.assertEqual(state["outcome_source"], "process")

    def test_a_runner_that_vanished_without_a_record_fails_the_task(self) -> None:
        _, task, worker = self._local_worker("vanished")
        self._kill(worker["pid"])
        self.coordinator.poll_worker(worker["id"])
        state = self._state(task["id"])
        self.assertEqual(state["task_status"], "failed")
        self.assertEqual(state["worker_status"], "failed")
        self.assertIsNone(state["protocol_outcome"])
        self.assertEqual(self._kinds(task["id"], fallback=True), {"failure": 2})

    def test_a_protocol_settled_session_that_vanished_is_kept_as_evidence(self) -> None:
        """No return code, but the session ending is still a fact worth having."""
        _, task, worker = self._local_worker("vanished-after-result")
        self.coordinator.record_worker_message(worker["id"], "result", "done")
        self._kill(worker["pid"])
        self.coordinator.poll_worker(worker["id"])
        state = self._state(task["id"])
        self.assertEqual(state["task_status"], "completed")
        self.assertTrue(state["exit_observed"])
        self.assertIsNone(state["process_exit_code"])
        self.assertEqual(state["kinds"].get("exit-evidence"), 1)
        # And it is not concluded from: an unknown code is not a mismatch.
        self.coordinator.poll_worker(worker["id"])
        self.assertEqual(self._state(task["id"]), state)

    # ---------- idempotence ----------

    def test_repeated_events_change_nothing(self) -> None:
        _, task, worker = self._worker("repeats")
        self.coordinator.record_worker_message(worker["id"], "blocker", "stuck")
        self._exit(worker, 0)
        self.coordinator.poll_worker(worker["id"])
        settled = self._state(task["id"])
        for _ in range(3):
            self.coordinator.poll_worker(worker["id"])
        self.assertEqual(self._state(task["id"]), settled)
        # Saying the same terminal thing again records nothing new rather than
        # raising: a retrying worker must not be able to worsen the record.
        self.coordinator.record_worker_message(worker["id"], "blocker", "stuck")
        self.assertEqual(self._state(task["id"]), settled)

    def test_a_duplicate_late_push_is_a_no_op(self) -> None:
        _, task, worker = self._worker("late-duplicate")
        self._exit(worker, 1)
        self.coordinator.poll_worker(worker["id"])
        self.coordinator.record_worker_message(worker["id"], "result", "done anyway")
        once = self._state(task["id"])
        self.coordinator.record_worker_message(worker["id"], "result", "done anyway")
        self.assertEqual(self._state(task["id"]), once)
        self.assertEqual(once["kinds"].get("exit-evidence"), 1)
        self.assertEqual(once["task_status"], "completed")

        # A second, contradicting verdict never overwrites the first, and says
        # so exactly once.
        self.coordinator.record_worker_message(worker["id"], "failure", "no, broken")
        conflicted = self._state(task["id"])
        self.assertEqual(conflicted["protocol_outcome"], "result")
        self.assertEqual(conflicted["task_status"], "completed")
        self.assertEqual(conflicted["kinds"].get("protocol-conflict"), 1)
        self.coordinator.record_worker_message(worker["id"], "failure", "no, broken")
        self.assertEqual(self._state(task["id"]), conflicted)

    def test_a_duplicate_terminal_line_in_the_log_is_a_no_op(self) -> None:
        """The stdout route reaches the same intake, so it is covered too."""
        _, task, worker = self._worker("duplicate-lines")
        line = json.dumps({"helm": 1, "type": "blocker", "text": "needs a key"})
        Path(worker["log_file"]).write_text(f"{line}\n{line}\n", encoding="utf-8")
        self.coordinator.poll_worker(worker["id"])
        state = self._state(task["id"])
        self.assertEqual(state["task_status"], "blocked")
        self.assertEqual(state["kinds"].get("blocker"), 1)

    def test_ordinary_late_messages_stay_refused(self) -> None:
        _, _, worker = self._worker("late-nonterminal")
        self._exit(worker, 0)
        self.coordinator.poll_worker(worker["id"])
        for kind in ("status", "question", "artifact"):
            with self.subTest(kind=kind):
                with self.assertRaisesRegex(HelmError, "no longer running"):
                    self.coordinator.record_worker_message(
                        worker["id"], kind, "still here"
                    )

    def test_a_stopped_worker_is_not_a_process_observation(self) -> None:
        """`stop` writes returncode null; that must not open the late path."""
        _, task, worker = self._worker("stopped")
        self.coordinator.stop_worker(worker["id"], "commander stopped it")
        state = self._state(task["id"])
        self.assertFalse(state["exit_observed"])
        self.assertIsNone(state["protocol_outcome"])
        with self.assertRaisesRegex(HelmError, "no longer running"):
            self.coordinator.record_worker_message(worker["id"], "result", "too late")
        self.coordinator.poll_worker(worker["id"])
        self.assertEqual(self._state(task["id"]), state)

    def test_reopening_a_worker_starts_a_new_episode(self) -> None:
        """A reviewer kept live for round two must get to answer round two.

        Reopened the way the review loop reopens one -- the loop's own path is
        covered in tests/test_review.py; what matters here is that clearing the
        episode is what makes the second verdict a verdict rather than a
        duplicate folded away.
        """
        _, task, worker = self._worker("reopened")
        self.coordinator.record_worker_message(worker["id"], "result", "round one")
        self._exit(worker, 0)
        self.coordinator.poll_worker(worker["id"])
        self.assertTrue(self._state(task["id"])["exit_observed"])

        with self.coordinator.store.locked() as data:
            live = data["workers"][worker["id"]]
            live["status"] = "running"
            live["exit_code"] = None
            live["ended_at"] = None
            self.coordinator.begin_worker_episode(live)
        Path(worker["exit_file"]).unlink()

        self.coordinator.record_worker_message(worker["id"], "blocker", "round two")
        state = self._state(task["id"])
        self.assertEqual(state["protocol_outcome"], "blocker")
        self.assertEqual(state["task_status"], "blocked")
        self.assertEqual(self._kinds(task["id"]).get("blocker"), 1)
        # And the next exit is a new observation, not one already folded in.
        self._exit(worker, 0)
        self.coordinator.poll_worker(worker["id"])
        reobserved = self._state(task["id"])
        self.assertTrue(reobserved["exit_observed"])
        self.assertEqual(reobserved["task_status"], "blocked")
        self.assertEqual(reobserved["kinds"].get("exit-evidence"), 1)

    def test_a_reopened_worker_is_not_read_as_already_reported(self) -> None:
        """History is cumulative; an episode is not.

        Round one's `result` stays in the record forever, so every reader that
        judges liveness by scanning for a terminal message would call round two
        finished before it had said anything.
        """
        _, task, worker = self._worker("reopen-readers")
        self.coordinator.record_worker_message(worker["id"], "result", "round one")
        reported = {e["worker_id"]: e for e in self.coordinator.worker_health()}
        if worker["id"] in reported:
            self.assertEqual(reported[worker["id"]]["verdict"], "reported")

        with self.coordinator.store.locked() as data:
            live = data["workers"][worker["id"]]
            live["status"] = "running"
            live["exit_code"] = None
            live["ended_at"] = None
            self.coordinator.begin_worker_episode(live)
            data["tasks"][task["id"]]["status"] = "running"

        health = {e["worker_id"]: e for e in self.coordinator.worker_health()}
        self.assertIn(worker["id"], health)
        self.assertNotEqual(health[worker["id"]]["verdict"], "reported")
        # And nothing settles this round on the previous round's verdict.
        with self.assertRaisesRegex(HelmError, r"not delivered a terminal message"):
            self.coordinator.settle_reported_worker(worker["id"])
        self.assertEqual(
            self.coordinator.inspect_task(task["id"])["task"]["status"], "running"
        )

        self.coordinator.record_worker_message(worker["id"], "blocker", "round two")
        settled = {e["worker_id"]: e for e in self.coordinator.worker_health()}
        if worker["id"] in settled:
            self.assertEqual(settled[worker["id"]]["verdict"], "reported")

    def test_a_worker_recorded_before_the_episode_fields_still_settles(self) -> None:
        """The scan survives only as a fallback, and only for those records."""
        _, task, worker = self._worker("legacy")
        self.coordinator.record_worker_message(worker["id"], "result", "done")
        with self.coordinator.store.locked() as data:
            old = data["workers"][worker["id"]]
            for field in self.coordinator._EPISODE_FIELDS:
                old.pop(field, None)
            old["status"] = "running"
        self.coordinator.settle_reported_worker(worker["id"])
        self.assertEqual(
            self.coordinator.inspect_task(task["id"])["task"]["status"], "completed"
        )

    # ---------- persistence and concurrency ----------

    def test_the_settled_state_survives_a_reload(self) -> None:
        _, task, worker = self._worker("reload")
        self.coordinator.record_worker_message(worker["id"], "blocker", "stuck")
        self._exit(worker, 0)
        self.coordinator.poll_worker(worker["id"])
        before = self._state(task["id"])

        # A fresh coordinator over the same store: the state machine lives in
        # the document, not in this process's memory.
        self.coordinator = Coordinator(StateStore(self.state.directory))
        self.assertEqual(self._state(task["id"]), before)
        self.coordinator.poll_worker(worker["id"])
        self.assertEqual(self._state(task["id"]), before)
        with self.assertRaisesRegex(HelmError, "no longer running"):
            self.coordinator.record_worker_message(worker["id"], "status", "hi")

    def test_a_push_racing_its_own_exit_converges_whoever_wins_the_lock(self) -> None:
        """The real race: a push and a poll contending for the state lock.

        Whichever gets there first, the worker's `blocker` is the outcome and
        the exit is evidence -- so the assertion is on the converged state, not
        on which thread happened to win.
        """
        for attempt in range(5):
            with self.subTest(attempt=attempt):
                _, task, worker = self._worker(f"race-{attempt}")
                self._exit(worker, 0)
                start = threading.Barrier(2)
                errors: list[Exception] = []

                def push() -> None:
                    start.wait()
                    try:
                        self.coordinator.record_worker_message(
                            worker["id"], "blocker", "needs a credential"
                        )
                    except Exception as exc:  # pragma: no cover - reported below
                        errors.append(exc)

                def poll() -> None:
                    start.wait()
                    try:
                        self.coordinator.poll_worker(worker["id"])
                    except Exception as exc:  # pragma: no cover - reported below
                        errors.append(exc)

                threads = [threading.Thread(target=push), threading.Thread(target=poll)]
                for thread in threads:
                    thread.start()
                for thread in threads:
                    thread.join(30)
                self.assertEqual(errors, [])
                state = self._state(task["id"])
                self.assertEqual(state["task_status"], "blocked")
                self.assertEqual(state["protocol_outcome"], "blocker")
                self.assertEqual(state["worker_status"], "failed")
                self.assertEqual(state["process_exit_code"], 0)
                self.assertEqual(state["kinds"].get("exit-evidence"), 1)

    def _open_items(self, project_id: str) -> list[dict]:
        return [
            item
            for item in self.coordinator.project_status(project_id)["action_items"]
            if item.get("status", "open") == "open"
            and "Authorize or refuse" in item["text"]
        ]

    def _latest_situation(self, project_id: str) -> str:
        live = [
            entry
            for entry in self.coordinator.project_status(project_id)["situation"]
            if not entry.get("superseded_by")
        ]
        return live[-1]["text"] if live else ""

    def test_a_request_whose_hold_is_abandoned_mid_flight_reconciles(self) -> None:
        """The request's effects run with the state lock released.

        So a poll can abandon the hold in the gap, and the two dangerous
        interleavings are different failures:

        `item` -- the request's situation is already written, the poll then
        abandons and resolves an action item that does not exist yet, and the
        request creates it afterwards: a pause left on the commander's list
        that nothing will ever close.

        `situation` -- the poll gets there first and writes "abandoned", and
        the delayed request then appends "Approval request" as the newest word
        about a task that has already failed.

        Both gates fire every run rather than once in a thousand.
        """
        for gate_on in ("item", "situation"):
            with self.subTest(gate=gate_on):
                self._abandon_mid_flight(gate_on)

    def _abandon_mid_flight(self, gate_on: str) -> None:
        project, task, worker = self._worker(f"mid-flight-{gate_on}")
        self._exit(worker, 0)
        abandoned = threading.Event()
        failures: list[BaseException] = []
        original_situation = self.coordinator.record_situation
        original_item = self.coordinator.record_project_action_item

        def gate(which: str) -> None:
            # Only in the pushing thread, so the poll's own effects run freely
            # rather than deadlocking against their own gate.
            if which == gate_on and threading.current_thread() is pusher:
                self.assertTrue(abandoned.wait(30))

        def gated_situation(*args: Any, **kwargs: Any):
            gate("situation")
            return original_situation(*args, **kwargs)

        def gated_item(*args: Any, **kwargs: Any):
            gate("item")
            return original_item(*args, **kwargs)

        def push() -> None:
            try:
                self.coordinator.record_worker_message(
                    worker["id"], "approval-needed", "publish the build",
                    payload={"action": "publish"},
                )
            except BaseException as exc:  # pragma: no cover - asserted below
                # An exception that only printed would let this test pass on
                # assertions that happen to hold for the wrong reason.
                failures.append(exc)

        pusher = threading.Thread(target=push)
        for patch in (
            mock.patch.object(
                self.coordinator, "record_situation", side_effect=gated_situation
            ),
            mock.patch.object(
                self.coordinator, "record_project_action_item",
                side_effect=gated_item,
            ),
        ):
            patch.start()
            self.addCleanup(patch.stop)
        pusher.start()
        # Wait for the hold itself, which the request writes under the lock --
        # so the poll below really does abandon this request's hold.
        deadline = time.monotonic() + 30
        while not self.coordinator.inspect_task(task["id"])["task"].get("holds"):
            self.assertLess(time.monotonic(), deadline)
            time.sleep(0.01)
        self.coordinator.poll_worker(worker["id"])
        abandoned.set()
        pusher.join(30)
        self.assertFalse(pusher.is_alive())
        self.assertEqual(failures, [])

        # Newest word first, because that is the half the `situation` gate is
        # about; the stale item is what the `item` gate is about.
        self.assertIn("abandoned", self._latest_situation(project["id"]))
        self.assertEqual(self._open_items(project["id"]), [])
        state = self._state(task["id"])
        self.assertEqual(state["task_status"], "failed")
        self.assertEqual(state["holds"], ["abandoned"])

    def test_the_exit_first_request_says_abandoned_not_paused(self) -> None:
        """The same reconciliation, reached sequentially."""
        project, task, worker = self._worker("exit-first-record")
        self._exit(worker, 0)
        self.coordinator.poll_worker(worker["id"])
        self.coordinator.record_worker_message(
            worker["id"], "approval-needed", "publish the build",
            payload={"action": "publish"},
        )
        self.assertEqual(self._open_items(project["id"]), [])
        self.assertIn("abandoned", self._latest_situation(project["id"]))
        self.assertEqual(self._state(task["id"])["task_status"], "failed")
        # Saying it twice records nothing more, and cannot reopen the pause.
        before = self.coordinator.project_status(project["id"])
        self.coordinator.record_worker_message(
            worker["id"], "approval-needed", "publish the build",
            payload={"action": "publish"},
        )
        self.assertEqual(self.coordinator.project_status(project["id"]), before)
        self.assertEqual(self._state(task["id"])["holds"], ["abandoned"])

    def test_the_default_wait_returns_on_a_session_that_stays_open(self) -> None:
        """A result settles the worker; its agent may sit there for hours.

        The default is `timeout=None`, which is the path that mattered: it used
        to reap the pid with a blocking `waitpid` and hang for as long as the
        agent kept its session open. A bounded timeout would have hidden that
        by expiring, so this asks for no timeout at all.
        """
        _, _, worker = self._local_worker("lingering")
        self.coordinator.record_worker_message(worker["id"], "result", "done")
        started = time.monotonic()
        waited = self.coordinator.wait_worker(worker["id"])
        self.assertLess(time.monotonic() - started, 5.0)
        self.assertEqual(waited["status"], "completed")
        self.assertFalse(waited.get("exit_observed"))
        # And the session really is still there: the wait returned on the
        # worker's word, not because the process had quietly gone.
        self.assertTrue(self.coordinator._pid_alive(worker["pid"]))

    def test_launching_with_wait_returns_on_the_workers_own_word(self) -> None:
        """`launch_worker(wait=True)` waited on the Popen after the wait path.

        So even a wait that returned promptly was followed by a block on the
        very process the contract says need not exit. The runner is detached
        instead, exactly as an async launch detaches it.
        """
        root = self.repo("wait-launch")
        project = self.coordinator.register_project(
            "Wait", str(root), project_id="wait-launch"
        )
        task = self.coordinator.create_task(project["id"], "report then linger")
        reported = threading.Event()
        launched: dict[str, Any] = {}

        def report_once_it_is_running() -> None:
            # The worker record exists as soon as the launch has taken the
            # lock; push its result from outside, which is what a real agent's
            # own `helm worker message` does.
            while not launched:
                time.sleep(0.01)
            self.coordinator.record_worker_message(
                launched["id"], "result", "done, session stays open"
            )
            reported.set()

        original = self.coordinator._prepare_worker_locked

        def capture(*args, **kwargs):
            worker, command = original(*args, **kwargs)
            launched.update(worker)
            return worker, command

        pusher = threading.Thread(target=report_once_it_is_running)
        with mock.patch.object(
            self.coordinator, "_prepare_worker_locked", side_effect=capture
        ):
            pusher.start()
            started = time.monotonic()
            worker = self.coordinator.launch_worker(
                task["id"],
                [sys.executable, "-c", "import time; time.sleep(120)"],
                wait=True,
            )
        pusher.join(30)
        self.addCleanup(self._kill, worker.get("pid"))
        self.assertTrue(reported.is_set())
        self.assertLess(time.monotonic() - started, 30.0)
        self.assertEqual(worker["status"], "completed")
        self.assertEqual(
            self.coordinator.inspect_task(task["id"])["task"]["status"], "completed"
        )

    def test_polling_reaps_a_local_child_rather_than_leaving_a_zombie(self) -> None:
        """Nothing has to call `wait_worker`, so polling cannot leave zombies.

        And an unreaped child is not merely untidy: `kill(pid, 0)` says a
        zombie is alive, so the worker never settled at all.
        """
        _, task, worker = self._local_worker("reaped")
        self._kill(worker["pid"], wait=False)
        settled = self.coordinator.poll_worker(worker["id"])
        self.assertNotEqual(settled["status"], "running")
        self.assertTrue(settled["exit_observed"])
        self.assertEqual(
            self.coordinator.inspect_task(task["id"])["task"]["status"], "failed"
        )
        with self.assertRaises(ChildProcessError):
            # Already reaped by the poll, which never blocked to do it.
            os.waitpid(int(worker["pid"]), 0)

    def test_a_fresh_workspace_is_trusted_before_the_runtime_asks(self) -> None:
        """The trust dialog is certain, and it is a deadlock in a pane.

        Claude Code asks whether the directory it started in is trusted, and
        skips the question only in non-interactive mode. Every Helm task gets a
        fresh worktree, so the answer is never already on file and an
        interactive worker stops dead before reading its assignment --
        `--permission-mode bypassPermissions` does not cover it and neither
        does `--dangerously-skip-permissions`.

        The write has to be surgical, because it is the commander's own file.
        """
        base = self._helm_root("trust")
        config = base / "fake.claude.json"
        config.write_text(
            json.dumps({"projects": {"/already": {"lastCost": 3}}, "keepMe": "yes"})
        )
        original = core._TRUST_CONFIGS
        core._TRUST_CONFIGS = {"claude": (config, "hasTrustDialogAccepted")}
        try:
            workspace = base / "fresh-worktree"
            workspace.mkdir()
            core._pretrust_workspace("claude", workspace)
            written = json.loads(config.read_text())
            self.assertTrue(
                written["projects"][str(core.canonical(workspace))][
                    "hasTrustDialogAccepted"
                ]
            )
            # Nothing else in the commander's file is touched.
            self.assertEqual(written["keepMe"], "yes")
            self.assertEqual(written["projects"]["/already"], {"lastCost": 3})

            # A runtime with no such dialog is left alone entirely.
            core._pretrust_workspace("codex", base / "other")
            self.assertEqual(len(json.loads(config.read_text())["projects"]), 2)

            # And an unwritable config costs a prompt, never a launch.
            core._TRUST_CONFIGS = {"claude": (Path("/nonexistent/dir/x.json"), "k")}
            core._pretrust_workspace("claude", workspace)
        finally:
            core._TRUST_CONFIGS = original


class WorkerRoundTests(HelmTestCase):
    def _task_with_resident(self, name: str):
        import sys
        from helm.herdr import HerdrAdapter
        from tests.support import FakeHerdr
        root = self.repo(name)
        project = self.coordinator.register_project(name.title(), str(root), project_id=name)
        task = self.coordinator.create_task(project["id"], "first round")
        herdr = FakeHerdr()
        adapter = HerdrAdapter(self.coordinator, herdr)
        worker = adapter.launch_task(
            task["id"], [sys.executable, "-c", "import time; time.sleep(60)"], wait=False
        )
        return project, task, worker, adapter

    def test_a_round_reuses_the_named_resident_worker(self) -> None:
        """A live worker blocks a plain round but not one that names it as the
        resident the round is being delivered into."""
        _project, task, worker, _adapter = self._task_with_resident("residentround")
        from helm.core import SafetyError
        with self.coordinator.store.locked() as data:
            data["tasks"][task["id"]]["status"] = "completed"
        with self.assertRaisesRegex(SafetyError, "still running"):
            self.coordinator.continue_task(task["id"], "round two")
        reopened = self.coordinator.continue_task(
            task["id"], "round two", reuse_worker=worker["id"]
        )
        self.assertEqual(reopened["status"], "allocated")
        self.assertEqual(reopened["brief"], "round two")
        self.coordinator.stop_worker(worker["id"], reason="test cleanup")

    def test_a_round_still_refuses_a_live_worker_it_did_not_name(self) -> None:
        _project, task, worker, _adapter = self._task_with_resident("wrongresident")
        from helm.core import SafetyError
        with self.coordinator.store.locked() as data:
            data["tasks"][task["id"]]["status"] = "completed"
        with self.assertRaisesRegex(SafetyError, "still running"):
            self.coordinator.continue_task(
                task["id"], "round two", reuse_worker="w-notthisone"
            )
        self.coordinator.stop_worker(worker["id"], reason="test cleanup")

    def test_a_failed_delivery_still_launches_a_fresh_worker(self) -> None:
        """When the resident session does not accept the round, stopping it
        must not strand the round: the task stays continuable and a fresh
        worker is launched instead of a silent no-op."""
        import contextlib as _ctx
        import io
        from unittest import mock
        from helm import cli
        _project, task, worker, adapter = self._task_with_resident("deadresident")
        with self.coordinator.store.locked() as data:
            data["tasks"][task["id"]]["status"] = "completed"
        output = io.StringIO()
        with _ctx.redirect_stdout(output), mock.patch.object(
            cli, "HerdrAdapter", return_value=adapter
        ), mock.patch.object(adapter, "answer_worker", return_value=False), \
                mock.patch.object(
                    adapter, "launch_task",
                    side_effect=lambda tid, _cmd, **kw: type(adapter).launch_task(
                        adapter, tid, [sys.executable, "-c", ""], **kw
                    ),
                ):
            code = cli.main([
                "--state-dir", str(self.state.directory),
                "worker", "round", task["id"],
                "--state-changing", "--brief", "round two",
            ])
        self.assertEqual(code, 0)
        text = output.getvalue()
        self.assertIn("did not accept the round", text)
        self.assertIn("fresh worker", text)
        data = self.coordinator.store.load()
        self.assertNotEqual(data["tasks"][task["id"]]["status"], "failed")
        fresh = [
            w for w in data["workers"].values()
            if w["task_id"] == task["id"] and w["id"] != worker["id"]
        ]
        self.assertTrue(fresh)
        for w in fresh:
            self.coordinator.stop_worker(w["id"], reason="test cleanup")

    def test_a_vanished_pane_mid_delivery_still_launches_a_fresh_worker(self) -> None:
        """A pane error raised during delivery is a refused round, not a raw
        provider error stranding the task."""
        import contextlib as _ctx
        import io
        from unittest import mock
        from helm import cli
        from helm.herdr import HerdrUnavailable
        _project, task, worker, adapter = self._task_with_resident("vanishedpane")
        with self.coordinator.store.locked() as data:
            data["tasks"][task["id"]]["status"] = "completed"
        output = io.StringIO()
        with _ctx.redirect_stdout(output), mock.patch.object(
            cli, "HerdrAdapter", return_value=adapter
        ), mock.patch.object(
            adapter, "answer_worker", side_effect=HerdrUnavailable("pane gone")
        ), mock.patch.object(
            adapter, "launch_task",
            side_effect=lambda tid, _cmd, **kw: type(adapter).launch_task(
                adapter, tid, [sys.executable, "-c", ""], **kw
            ),
        ):
            code = cli.main([
                "--state-dir", str(self.state.directory),
                "worker", "round", task["id"],
                "--state-changing", "--brief", "round two",
            ])
        self.assertEqual(code, 0)
        self.assertIn("fresh worker", output.getvalue())
        data = self.coordinator.store.load()
        for w in data["workers"].values():
            if w["task_id"] == task["id"] and w.get("status") == "running":
                self.coordinator.stop_worker(w["id"], reason="test cleanup")

    def test_a_failed_launch_restores_the_round_to_continuable(self) -> None:
        """When the fresh launch itself dies, the task must not be stranded
        in `allocated` where no later round can continue it."""
        import contextlib as _ctx
        import io
        from unittest import mock
        from helm import cli
        from helm.herdr import HerdrUnavailable
        _project, task, worker, adapter = self._task_with_resident("launchdies")
        self.coordinator.stop_worker(worker["id"], reason="clear resident")
        with self.coordinator.store.locked() as data:
            data["tasks"][task["id"]]["status"] = "completed"
        with _ctx.redirect_stdout(io.StringIO()), _ctx.redirect_stderr(io.StringIO()), \
                mock.patch.object(cli, "HerdrAdapter", return_value=adapter), \
                mock.patch.object(
                    adapter, "launch_task", side_effect=HerdrUnavailable("no pane")
                ):
            code = cli.main([
                "--state-dir", str(self.state.directory),
                "worker", "round", task["id"],
                "--state-changing", "--fresh", "--brief", "round two",
            ])
        self.assertNotEqual(code, 0)
        data = self.coordinator.store.load()
        self.assertEqual(data["tasks"][task["id"]]["status"], "completed")

    def test_the_worker_context_tells_the_author_to_stay_resident(self) -> None:
        root = self.repo("residencycontract")
        project = self.coordinator.register_project(
            "Residency", str(root), project_id="residencycontract"
        )
        task = self.coordinator.create_task(project["id"], "write the code")
        import sys, json
        worker = self.coordinator.launch_worker(
            task["id"], [sys.executable, "-c", ""], wait=False
        )
        context_path = (
            self.coordinator.store.directory / "workers" / worker["id"] / "context.json"
        )
        text = context_path.read_text(encoding="utf-8")
        self.assertIn("STAY in this session", text)
        self.assertIn("stand down", text)
