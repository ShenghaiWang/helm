"""The worker protocol, worker/foreman process lifecycle and health."""

from __future__ import annotations

import contextlib
import datetime as _dt
import io
import os
import json
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path
from unittest import mock

from helm import cli
from helm.core import (
    CORE_SAFETY_RULES,
    HelmError,
    _git_root,
    inside,
)
from helm.herdr import HerdrAdapter

from tests.support import FakeHerdr, HelmTestCase, REPO_ROOT, SHIPPED_DOMAINS


class WorkerProtocolTests(HelmTestCase):
    def _runner_config(self, name: str) -> Path:
        root = self.repo(name)
        base = Path(self.temp.name) / f"{name}-runner"
        base.mkdir()
        common_dir = subprocess.run(
            ["git", "-C", str(root), "rev-parse", "--path-format=absolute", "--git-common-dir"],
            check=True,
            text=True,
            stdout=subprocess.PIPE,
        ).stdout.strip()
        config_path = base / "runner.json"
        config_path.write_text(
            json.dumps(
                {
                    "command": [sys.executable, "-c", "print('hello from the worker')"],
                    "cwd": str(root),
                    "git_common_dir": common_dir,
                    "log": str(base / "output.log"),
                    "exit": str(base / "exit.json"),
                    "worker_env": {"HELM_WORKER_ID": "w-visible"},
                }
            ),
            encoding="utf-8",
        )
        config_path.chmod(0o600)
        return config_path

    def _foreman_runner_config(self, name: str, *, workspace: Path, state_dir: Path) -> Path:
        base = Path(self.temp.name) / f"{name}-foreman-runner"
        base.mkdir()
        config_path = base / "runner.json"
        config_path.write_text(
            json.dumps(
                {
                    "command": [sys.executable, "-c", "print('foreman is driving')"],
                    "cwd": str(workspace),
                    "git_common_dir": str(base / "unused-common-dir"),
                    "workspace_kind": "state-directory",
                    "state_dir": str(state_dir),
                    "log": str(base / "output.log"),
                    "exit": str(base / "exit.json"),
                    "worker_env": {"HELM_WORKER_ID": "w-foreman"},
                }
            ),
            encoding="utf-8",
        )
        config_path.chmod(0o600)
        return config_path

    def test_run_delegates_to_a_herdr_worker_space_by_default(self) -> None:
        parser = cli._build_parser()
        default = parser.parse_args(["run", "media", "prepare the next artifact"])
        self.assertTrue(default.herdr)
        opted_out = parser.parse_args(["run", "media", "prepare the next artifact", "--no-herdr"])
        self.assertFalse(opted_out.herdr)

    def test_worker_messages_are_routed_and_worker_output_is_data(self) -> None:
        root = self.repo("messages")
        project = self.coordinator.register_project("Messages", str(root), project_id="messages")
        task = self.coordinator.create_task(project["id"], "report a blocker")
        command = [
            sys.executable,
            "-c",
            (
                "import json; print(json.dumps({'helm': 1, 'type': 'blocker', "
                "'text': 'needs credentials'})); print('plain output')"
            ),
        ]
        worker = self.coordinator.launch_worker(task["id"], command)
        # It reported a blocker and then exited cleanly. The blocker is the
        # outcome -- of the task and of the worker -- and the orderly exit is
        # kept beside it as evidence rather than read back as success.
        # docs/worker-lifecycle.md is the contract; tests/test_worker_lifecycle.py
        # holds it in both orders.
        self.assertEqual(worker["status"], "failed")
        self.assertEqual(worker["protocol_outcome"], "blocker")
        self.assertEqual(worker["process_exit_code"], 0)
        inspected = self.coordinator.inspect_task(task["id"])
        self.assertTrue(
            any(message["kind"] == "exit-evidence" for message in inspected["messages"])
        )
        self.assertEqual(inspected["project"]["id"], "messages")
        self.assertEqual(inspected["task"]["status"], "blocked")
        self.assertTrue(any(message["kind"] == "blocker" for message in inspected["messages"]))
        # Plain output is still data, and it is still kept -- in the worker's
        # own log, which is what `helm tail` reads. It is deliberately not a
        # second copy in shared state: half a million such lines had grown to
        # 99.4% of a 224 MB state file that a save rewrites in full.
        self.assertFalse(any(message["kind"] == "output" for message in inspected["messages"]))
        self.assertIn(
            "plain output", "\n".join(self.coordinator.worker_output(worker["id"], 50))
        )

    def test_helm_sees_a_stalled_worker_without_anyone_opening_its_ui(self) -> None:
        root = self.repo("health")
        project = self.coordinator.register_project("Health", str(root), project_id="health")
        task = self.coordinator.create_task(project["id"], "go quiet")
        worker = self.coordinator.launch_worker(
            task["id"], [sys.executable, "-c", ""], wait=False
        )
        # Fresh output and nothing reported yet is starting up, not a fault:
        # flagging every new worker would make the attention list noise.
        healthy = {entry["worker_id"]: entry for entry in self.coordinator.worker_health()}
        self.assertEqual(healthy[worker["id"]]["verdict"], "starting")

        # Age the log past the threshold with no protocol message: this is the
        # failure a human would otherwise only find by looking at the pane.
        stale = time.time() - 10_000
        os.utime(worker["log_file"], (stale, stale))
        stalled = {entry["worker_id"]: entry for entry in self.coordinator.worker_health()}
        self.assertEqual(stalled[worker["id"]]["verdict"], "stalled")
        self.assertIn("no terminal output", stalled[worker["id"]]["detail"])

        # A worker that already delivered a terminal message is idle, not
        # stalled; flagging it would make the attention list worthless.
        self.coordinator.record_worker_message(worker["id"], "result", "done")
        reported = {entry["worker_id"]: entry for entry in self.coordinator.worker_health()}
        if worker["id"] in reported:
            self.assertEqual(reported[worker["id"]]["verdict"], "reported")

    def test_sweep_settles_a_worker_that_finished_while_nobody_was_looking(self) -> None:
        root = self.repo("sweep")
        project = self.coordinator.register_project("Sweep", str(root), project_id="sweep")
        task = self.coordinator.create_task(project["id"], "finish unobserved")
        worker = self.coordinator.launch_worker(
            task["id"], [sys.executable, "-c", ""], wait=False
        )
        # The process ended and wrote its exit record, but no one polled it, so
        # the task sits in `running` for as long as nobody looks.
        Path(worker["exit_file"]).write_text(json.dumps({"returncode": 0}) + "\n", encoding="utf-8")
        self.assertEqual(self.coordinator.inspect_task(task["id"])["task"]["status"], "running")
        report = {entry["worker_id"]: entry for entry in self.coordinator.sweep_workers()}
        self.assertEqual(report[worker["id"]]["verdict"], "settled")
        self.assertNotEqual(
            self.coordinator.inspect_task(task["id"])["task"]["status"], "running"
        )
        # Repair stops at the unambiguous: a stalled worker is reported, never
        # silently failed, because its pane is the evidence for diagnosing it.
        self.assertEqual(self.coordinator.sweep_workers(), [])

    def test_a_worker_terminal_message_ends_the_task_without_a_process_exit(self) -> None:
        root = self.repo("settle")
        project = self.coordinator.register_project("Settle", str(root), project_id="settle")
        task = self.coordinator.create_task(project["id"], "report then linger")
        worker = self.coordinator.launch_worker(
            task["id"], [sys.executable, "-c", ""], wait=False
        )
        # An agent CLI keeps its session open after finishing, and a session
        # killed with its pane never writes an exit record at all -- the
        # protocol result itself must settle the lifecycle for review.
        self.coordinator.record_worker_message(worker["id"], "result", "work done")
        settled = self.coordinator.inspect_task(task["id"])["workers"][0]
        self.assertEqual(settled["status"], "completed")
        self.assertEqual(self.coordinator.inspect_task(task["id"])["task"]["status"], "completed")

        # A blocker settles to blocked, not completed: the worker's own word
        # decides the outcome, and settling never invents success.
        other = self.coordinator.create_task(project["id"], "hit a wall")
        blocked = self.coordinator.launch_worker(
            other["id"], [sys.executable, "-c", ""], wait=False
        )
        self.coordinator.record_worker_message(blocked["id"], "blocker", "needs a credential")
        self.coordinator.settle_reported_worker(blocked["id"])
        self.assertEqual(self.coordinator.inspect_task(other["id"])["task"]["status"], "blocked")

        # A worker that has said nothing terminal is not settled by guesswork.
        quiet_task = self.coordinator.create_task(project["id"], "say nothing")
        quiet = self.coordinator.launch_worker(
            quiet_task["id"], [sys.executable, "-c", ""], wait=False
        )
        with self.assertRaisesRegex(HelmError, r"not delivered a terminal message"):
            self.coordinator.settle_reported_worker(quiet["id"])

    def test_a_foreman_tab_is_called_foreman(self) -> None:
        """Its brief is standing text, so slugging it named the tab after that.

        Every foreman got a tab reading like the opening words of "You are this
        project's foreman...", which says nothing about which pane it is.
        """
        foreman_task = {"role": "foreman", "brief": "You are this project's foreman. You own..."}
        worker = {"id": "w-fe58cded506e"}
        self.assertEqual(HerdrAdapter._worker_tab_label(foreman_task, worker), "foreman")

        # An ordinary worker still gets its brief and a disambiguating suffix,
        # because a project has many of those at once.
        worker_task = {"role": "worker", "brief": "Implement slice S0 of the migration"}
        label = HerdrAdapter._worker_tab_label(worker_task, worker)
        self.assertNotEqual(label, "foreman")
        self.assertIn("fe58", label)

    def test_a_reviewer_gets_no_checkout_and_no_branch(self) -> None:
        """A reviewer reads a diff; it never writes one.

        Giving each review its own worktree cost a full clone per review --
        thirty-five of them on one project, every branch deleted as empty when
        the tasks were cleaned up.
        """
        root = self.repo("reviewrole")
        project = self.coordinator.register_project(
            "Reviewrole", str(root), project_id="reviewrole"
        )
        task = self.coordinator.create_task(project["id"], "read it", role="reviewer")

        self.assertIsNone(task["branch"])
        workspace = Path(task["workspace"])
        self.assertIn("reviewers", workspace.parts)
        # Inside Helm's own state, never the project or a user's checkout.
        self.assertTrue(str(workspace).startswith(str(self.state.directory)))

        self.coordinator.prepare_external_worker(task["id"], [sys.executable, "-c", ""])
        worktrees = subprocess.run(
            ["git", "-C", str(root), "worktree", "list"],
            check=True, text=True, stdout=subprocess.PIPE,
        ).stdout
        self.assertNotIn(task["id"], worktrees)
        self.assertEqual(
            subprocess.run(
                ["git", "-C", str(root), "branch", "--list", f"helm/{project['id']}/*"],
                check=True, text=True, stdout=subprocess.PIPE,
            ).stdout.strip(),
            "",
        )

    def test_a_foreman_drives_one_project_and_is_never_work_to_merge(self) -> None:
        root = self.repo("foremanning")
        project = self.coordinator.register_project(
            "Foreman", str(root), project_id="foremanning"
        )
        self.coordinator.record_situation(project["id"], "the trailer is waiting on audio")
        task = self.coordinator.create_foreman_task(project["id"])

        # It is started with the state of play, not with a conversation.
        self.assertEqual(task["role"], "foreman")
        self.assertIn("the trailer is waiting on audio", task["brief"])
        self.assertIn("You cannot approve, merge, publish, push, delete", task["brief"])
        # Its domain is about driving, never about the work it drives: a
        # driver carrying the work's domain would leak it into every task it
        # creates, including the ones that are not code.
        self.assertEqual(task["domain"], "driving-delegated-work")

        worker = self.coordinator.prepare_external_worker(
            task["id"], [sys.executable, "-c", ""]
        )
        self.assertEqual(self.coordinator.foreman_for(project["id"])["id"], worker["id"])

        # A foreman produces no branch, so it is never offered as work to
        # merge and never gets a card on the board.
        self.coordinator.record_worker_message(worker["id"], "result", "driving")
        status = self.coordinator.project_status(project["id"])
        self.assertNotIn(task["id"], [entry["task_id"] for entry in status["unmerged"]])
        cards = [card["id"] for group in self.coordinator.board() for card in group["tasks"]]
        self.assertNotIn(task["id"], cards)

    def test_an_idle_foreman_stands_down_so_its_project_can_release_its_space(self) -> None:
        # A foreman was appointed once and never terminated, and releasing a
        # space requires no running worker -- so for every project with a
        # driver, which is every project by default, "a finished project
        # releases its space" could never fire.
        root = self.repo("standing-down")
        project = self.coordinator.register_project(
            "Down", str(root), project_id="standing-down"
        )
        work = self.coordinator.create_task(project["id"], "the work", no_domain=True)
        worker = self.coordinator.launch_worker(
            work["id"], [sys.executable, "-c", ""], wait=False
        )
        foreman_task = self.coordinator.create_foreman_task(project["id"])
        foreman = self.coordinator.launch_worker(
            foreman_task["id"], [sys.executable, "-c", "import time; time.sleep(30)"], wait=False
        )

        # While the work it drives is unfinished, it stays.
        self.assertIsNone(self.coordinator.stand_down_idle_foreman(project["id"]))

        self.coordinator.record_worker_message(worker["id"], "result", "done")
        self.coordinator.wait_worker(worker["id"])
        stood = self.coordinator.stand_down_idle_foreman(project["id"])
        self.assertIsNotNone(stood)
        self.assertEqual(stood["id"], foreman["id"])
        self.assertEqual(stood["status"], "completed")
        settled = self.coordinator.store.load()
        self.assertEqual(settled["tasks"][foreman_task["id"]]["status"], "completed")
        # Idempotent, and nothing left running to block a space release.
        self.assertIsNone(self.coordinator.stand_down_idle_foreman(project["id"]))
        self.assertFalse(
            [w for w in settled["workers"].values() if w.get("status") == "running"]
        )

    def test_a_foreman_waiting_on_its_own_worker_is_not_reported_as_a_fault(self) -> None:
        # A driver blocked on the review it launched is doing its job. Calling
        # that a fault trains the reader to ignore the attention list.
        root = self.repo("waiting-foreman")
        project = self.coordinator.register_project(
            "Waiting", str(root), project_id="waiting-foreman"
        )
        driven_task = self.coordinator.create_task(project["id"], "the work", no_domain=True)
        self.coordinator.launch_worker(
            driven_task["id"], [sys.executable, "-c", "import time; time.sleep(30)"], wait=False
        )
        foreman_task = self.coordinator.create_foreman_task(project["id"])
        foreman = self.coordinator.launch_worker(
            foreman_task["id"], [sys.executable, "-c", "import time; time.sleep(30)"], wait=False
        )

        health = {h["worker_id"]: h for h in self.coordinator.worker_health(silence_seconds=0)}
        self.assertEqual(health[foreman["id"]]["verdict"], "driving")
        self.assertIn("waiting on", health[foreman["id"]]["detail"])

    def test_a_foreman_is_told_it_is_the_foreman_not_the_worker(self) -> None:
        # The foreman is launched down the same path as any worker, so it used
        # to open by being told it was "Helm's delegated worker for this task"
        # and to "work only in the assigned worktree" -- directly contradicting
        # the brief that follows, and nudging it toward doing the work itself.
        root = self.repo("named")
        project = self.coordinator.register_project("Named", str(root), project_id="named")
        foreman = self.coordinator.create_foreman_task(project["id"])
        worker_task = self.coordinator.create_task(project["id"], "do the actual work")
        context = Path(self.temp.name) / "context.json"

        driving = self.coordinator._worker_prompt(project, foreman, context)
        self.assertIn("You are the foreman", driving)
        self.assertNotIn("delegated worker", driving)
        self.assertNotIn("work only in the assigned worktree", driving)

        working = self.coordinator._worker_prompt(project, worker_task, context)
        self.assertIn("delegated worker", working)

    def test_a_foreman_gets_no_worktree_and_no_branch_to_edit(self) -> None:
        # It drives work rather than doing it, so a checkout and a task branch
        # are both an invitation and a branch to shed for a task that never
        # held a change.
        root = self.repo("driving")
        project = self.coordinator.register_project("Driving", str(root), project_id="driving")
        foreman = self.coordinator.create_foreman_task(project["id"])
        self.assertEqual(foreman["role"], "foreman")
        self.assertIsNone(foreman["branch"])

        allocated = self.coordinator.allocate_task(foreman["id"])
        workspace = Path(allocated["workspace"])
        self.assertTrue(workspace.is_dir())
        # Helm-owned state, never a checkout of the project.
        self.assertTrue(inside(workspace, self.coordinator.store.directory))
        self.assertNotEqual(_git_root(workspace), workspace)
        branches = subprocess.run(
            ["git", "-C", str(root), "branch", "--format=%(refname:short)"],
            check=True, text=True, stdout=subprocess.PIPE,
        ).stdout.split()
        self.assertEqual([b for b in branches if b.startswith("helm/")], [])
        # And the project's own checkout is untouched by any of it.
        self.assertEqual(len(self.coordinator._task_workers(self.coordinator.store.load(), foreman["id"])), 0)

    def test_a_finished_worker_wakes_its_foreman_instead_of_waiting_to_be_polled(self) -> None:
        root = self.repo("waking")
        project = self.coordinator.register_project(
            "Wake", str(root), project_id="waking"
        )
        foreman_task = self.coordinator.create_foreman_task(project["id"])
        foreman = self.coordinator.prepare_external_worker(
            foreman_task["id"], [sys.executable, "-c", ""]
        )
        task = self.coordinator.create_task(project["id"], "write the code")
        coder = self.coordinator.prepare_external_worker(
            task["id"], [sys.executable, "-c", ""]
        )
        adapter = HerdrAdapter(self.coordinator, FakeHerdr())
        delivered: list[tuple[str, str]] = []

        with mock.patch.object(
            adapter, "answer_worker", side_effect=lambda w, t: delivered.append((w, t)) or True
        ):
            # Routine progress must not interrupt the driver: a foreman told
            # about every push spends its attention reading, not driving.
            self.coordinator.record_worker_message(coder["id"], "status", "still going")
            self.assertFalse(adapter.notify_foreman(coder["id"]))
            self.assertEqual(delivered, [])

            # A status explicitly marked as an intermediate outcome summary
            # does wake the driver; this is how a long coding/review loop
            # reports the shape of progress without spamming every heartbeat.
            self.coordinator.record_worker_message(
                coder["id"],
                "status",
                "round 3 implementation done; waiting on reviewer",
                payload={"summary": True},
            )
            self.assertTrue(adapter.notify_foreman(coder["id"]))
            self.assertIn("round 3 implementation done", delivered[-1][1])
            status = self.coordinator.project_status(project["id"])
            self.assertTrue(any("Worker summary:" in entry["text"] for entry in status["situation"]))

            # A terminal message is the whole point. Without this the foreman
            # only learns its delegated work finished by happening to poll.
            self.coordinator.record_worker_message(
                coder["id"], "result", "Done. Commit abc1234, comments only."
            )
            self.assertTrue(adapter.notify_foreman(coder["id"]))
            self.assertEqual(delivered[-1][0], foreman["id"])
            told = delivered[-1][1]
            self.assertIn(coder["id"], told)
            self.assertIn("Commit abc1234", told)
            # Hand it the next command, not just the news.
            self.assertIn(f"helm review {task['id']}", told)

            # A foreman's own report goes to Helm, never back into itself.
            delivered.clear()
            self.coordinator.record_worker_message(
                foreman["id"],
                "status",
                "task round 5: reviewer approved, waiting on merge decision",
                payload={"summary": True},
            )
            status = self.coordinator.project_status(project["id"])
            self.assertTrue(any("Foreman report:" in entry["text"] and "task round 5" in entry["text"] for entry in status["situation"]))
            self.coordinator.record_worker_message(foreman["id"], "result", "project driven")
            self.assertFalse(adapter.notify_foreman(foreman["id"]))
            self.assertEqual(delivered, [])

    def test_a_worker_that_died_right_after_reporting_is_not_called_healthy(self) -> None:
        root = self.repo("vanishing")
        project = self.coordinator.register_project(
            "Vanish", str(root), project_id="vanishing"
        )
        task = self.coordinator.create_task(project["id"], "edit a file")
        worker = self.coordinator.launch_worker(
            task["id"], [sys.executable, "-c", "import time; time.sleep(120)"], wait=False
        )
        # It reports, as a working agent does.
        self.coordinator.record_worker_message(
            worker["id"], "status", "edited the file; committing next"
        )
        healthy = {e["worker_id"]: e for e in self.coordinator.worker_health()}
        self.assertEqual(healthy[worker["id"]]["verdict"], "healthy")

        # Then its process dies without an exit record -- which is what an
        # agent CLI in one-shot mode does the moment its turn ends, mid-task
        # and before committing. Judging liveness from its own messages called
        # this healthy indefinitely: the last push is always fresh, because it
        # died right after making it.
        os.kill(worker["pid"], signal.SIGKILL)
        os.waitpid(worker["pid"], 0)
        Path(worker["exit_file"]).unlink(missing_ok=True)

        after = {e["worker_id"]: e for e in self.coordinator.worker_health()}
        self.assertEqual(after[worker["id"]]["verdict"], "died")
        self.assertIn("uncommitted", after[worker["id"]]["detail"])

    def test_a_launched_worker_gets_a_tab_so_a_foremans_agents_are_visible(self) -> None:
        parser = cli._build_parser()
        # A foreman spawns through `helm worker launch`. When that went
        # straight to the process launcher, every agent a foreman started was
        # invisible while the foreman itself sat in a tab -- and a project's
        # space is supposed to hold one tab per worker.
        self.assertTrue(parser.parse_args(["worker", "launch", "t-1"]).herdr)
        self.assertFalse(
            parser.parse_args(["worker", "launch", "t-1", "--no-herdr"]).herdr
        )

        root = self.repo("tabbed")
        project = self.coordinator.register_project(
            "Tabbed", str(root), project_id="tabbed"
        )
        task = self.coordinator.create_task(project["id"], "write the code")
        seen: list[str] = []

        def record(task_id, command, **kwargs):
            seen.append(task_id)
            return {"id": "w-x", "status": "running", "task_id": task_id,
                    "project_id": project["id"], "execution": "herdr"}

        with mock.patch.object(HerdrAdapter, "launch_task", side_effect=record), \
             mock.patch.object(cli, "_ensure_foreman"):
            self.assertEqual(
                cli.main(["--state-dir", str(self.state.directory),
                          "worker", "launch", task["id"], "--async"]),
                0,
            )
        self.assertEqual(seen, [task["id"]])

    def test_an_over_long_situation_note_is_refused_rather_than_silently_cut(self) -> None:
        root = self.repo("noting")
        project = self.coordinator.register_project(
            "Note", str(root), project_id="noting"
        )
        # A note used to be trimmed to the limit without a word said, which
        # destroyed exactly the wrong end: what to do next goes last, so a
        # long note lost its point and still looked complete. A foreman read
        # one of those, found no goal, and started the wrong work.
        goal = "NEXT: redo F1, and nothing else."
        overlong = ("x" * self.coordinator.SITUATION_LINE_LIMIT) + " " + goal
        with self.assertRaises(HelmError) as caught:
            self.coordinator.record_situation(project["id"], overlong)
        # The error has to say how to fix it, or the writer just trims by hand
        # and loses the same content deliberately instead of accidentally.
        self.assertIn("Split it into separate notes", str(caught.exception))
        self.assertEqual(self.coordinator.project_status(project["id"])["situation"], [])

        # A note inside the limit is stored whole -- no ellipsis, no trim.
        exact = "y" * self.coordinator.SITUATION_LINE_LIMIT
        entry = self.coordinator.record_situation(project["id"], exact)
        self.assertEqual(entry["text"], exact)
        self.coordinator.record_situation(project["id"], goal)
        recorded = [
            e["text"] for e in self.coordinator.project_status(project["id"])["situation"]
        ]
        self.assertEqual(recorded[-1], goal)

    def test_stopping_a_worker_settles_it_so_nothing_stays_running_forever(self) -> None:
        root = self.repo("stopping")
        project = self.coordinator.register_project(
            "Stop", str(root), project_id="stopping"
        )
        task = self.coordinator.create_task(project["id"], "run for a long time")
        worker = self.coordinator.launch_worker(
            task["id"], [sys.executable, "-c", "import time; time.sleep(120)"], wait=False
        )
        self.assertEqual(worker["status"], "running")

        stopped = self.coordinator.stop_worker(worker["id"], "abandoned by the commander")
        self.assertTrue(stopped["signalled"])
        self.assertEqual(stopped["status"], "failed")
        # The process is actually gone, not merely recorded as gone.
        with self.assertRaises(OSError):
            os.kill(worker["pid"], 0)

        data = self.coordinator.store.load()
        # The task leaves `running`, which is what lets cleanup and a new
        # foreman proceed. Before this there was no way out at all.
        self.assertEqual(data["tasks"][task["id"]]["status"], "failed")
        failure = [
            m for m in data["messages"]
            if m.get("worker_id") == worker["id"] and m.get("kind") == "failure"
        ][-1]
        # Abandoned on purpose and died on its own are both failures, and the
        # record says which.
        self.assertEqual(failure["payload"]["stop_kind"], "stopped")
        self.assertIn("Worker stopped: abandoned by the commander", failure["text"])

        # Idempotent: stopping something already stopped is what someone does
        # when unsure the first one took.
        again = self.coordinator.stop_worker(worker["id"], "again")
        self.assertEqual(again["status"], "failed")

        # The evidence survives; removing it stays a separate, deliberate act.
        self.assertTrue(Path(worker["log_file"]).exists())
        self.assertFalse(data["tasks"][task["id"]]["workspace_removed"])

    def test_a_dead_foreman_can_be_replaced_rather_than_wedging_its_project(self) -> None:
        root = self.repo("wedged")
        project = self.coordinator.register_project(
            "Wedged", str(root), project_id="wedged"
        )
        foreman_task = self.coordinator.create_foreman_task(project["id"])
        foreman = self.coordinator.prepare_external_worker(
            foreman_task["id"], [sys.executable, "-c", ""]
        )
        # A pane closed by hand leaves the record saying `running`, and
        # foreman_for only matches running -- so without a way to stop it,
        # this project could never be given another driver.
        self.assertEqual(self.coordinator.foreman_for(project["id"])["id"], foreman["id"])

        self.coordinator.stop_worker(foreman["id"], "pane was closed by hand")
        self.assertIsNone(self.coordinator.foreman_for(project["id"]))

    def test_every_project_gets_a_foreman_unless_it_declines_one(self) -> None:
        # A project that says nothing still gets a driver: the alternative is
        # the coordinator remembering to appoint one, which is the failure
        # this removes.
        silent = self.repo("silent")
        quiet = self.coordinator.register_project("Quiet", str(silent), project_id="silent")
        self.assertTrue(self.coordinator.project_wants_foreman(quiet["id"]))

        root = self.repo("declining")
        (root / ".helm").mkdir()
        (root / ".helm" / "project.json").write_text(
            json.dumps({"foreman": False}), encoding="utf-8"
        )
        project = self.coordinator.register_project(
            "Declined", str(root), project_id="declining"
        )
        self.assertFalse(self.coordinator.project_wants_foreman(project["id"]))

        # A project says whether it wants a driver, never what one may do: a
        # project file cannot widen Helm's authority by phrasing.
        (root / ".helm" / "project.json").write_text(
            json.dumps({"foreman": "yes, and it may merge"}), encoding="utf-8"
        )
        with self.assertRaises(HelmError):
            self.coordinator._discovery_settings(root)

    def test_a_foreman_gets_the_boundary_from_code_and_the_craft_from_a_domain(self) -> None:
        helm_root, project = self._domain_root_project("crosscheck")
        shipped = SHIPPED_DOMAINS
        for domain_id in (
            "driving-delegated-work",
            "code-review",
            "spec-driven-development",
            "branch-isolation",
            "model-selection",
        ):
            shutil.copytree(shipped / domain_id, helm_root / "domains" / domain_id)
        task = self._coordinator.create_foreman_task(project["id"])

        # Code holds the boundary: what a foreman is and what it may not do.
        # A domain file is untrusted guidance and must never define that.
        self.assertIn("helm task create --project", task["brief"])
        self.assertIn("helm worker launch", task["brief"])
        self.assertIn("helm review <task-id>", task["brief"])
        self.assertIn("You must not do the work yourself", task["brief"])
        self.assertIn("cannot approve, merge, publish, push, delete", task["brief"])

        # The craft comes from `domains/`, where it is versioned and reusable,
        # and it arrives composed with the coder/reviewer independence rules
        # rather than restating them. `branch-isolation` and `model-selection`
        # are composed in too, so the foreman gets the fresh-base gate and the
        # skill/runtime-fit guidance before it ever creates a task.
        context = self._coordinator._context(project, task, "w-foreman")
        self.assertEqual(
            context["domain_chain"],
            [
                "spec-driven-development",
                "code-review",
                "branch-isolation",
                "model-selection",
                "driving-delegated-work",
            ],
        )
        text = json.dumps(context)
        self.assertIn("Do not do the delegated work yourself", text)
        self.assertIn("the reviewer must not be the author", text)
        # The fresh-base gate reaches the foreman before it allocates a task.
        self.assertIn("The base must be fresh and verified before a worktree is cut", text)
        self.assertIn(
            "resolve the project's *configured* default/base branch "
            "(never a hardcoded or inferred name; a repository default is "
            "only inferred once, at registration, and only falls back to "
            "the checked-out branch when the project has no remote at all",
            text,
        )

    def test_a_foreman_that_is_down_outranks_any_stalled_worker(self) -> None:
        root = self.repo("driverdown")
        project = self.coordinator.register_project(
            "Down", str(root), project_id="driverdown"
        )
        ordinary = self.coordinator.create_task(project["id"], "write the code")
        self.coordinator.prepare_external_worker(ordinary["id"], [sys.executable, "-c", ""])
        foreman_task = self.coordinator.create_foreman_task(project["id"])
        foreman = self.coordinator.prepare_external_worker(
            foreman_task["id"], [sys.executable, "-c", ""]
        )

        health = self.coordinator.worker_health()
        # Foreman first: it is the thing that would have noticed the other.
        self.assertEqual(health[0]["worker_id"], foreman["id"])
        self.assertEqual(health[0]["role"], "foreman")
        self.assertEqual(health[1]["role"], "worker")

    def test_output_recovery_reads_only_the_round_that_asked_for_it(self) -> None:
        root = self.repo("marking")
        project = self.coordinator.register_project("Mark", str(root), project_id="marking")
        task = self.coordinator.create_task(project["id"], "produce output")
        worker = self.coordinator.prepare_external_worker(task["id"], [sys.executable, "-c", ""])
        Path(worker["log_file"]).write_text("APPROVED round one\n", encoding="utf-8")
        mark = self.coordinator.worker_output_mark(worker["id"])
        with Path(worker["log_file"]).open("a", encoding="utf-8") as handle:
            handle.write("CHANGES-REQUESTED round two\n")
        # A stale verdict from an earlier round must not answer this one.
        self.assertEqual(
            self.coordinator.worker_output(worker["id"], since=mark),
            ["CHANGES-REQUESTED round two"],
        )

    def test_recovery_never_reads_the_brief_back_as_the_answer_to_itself(self) -> None:
        """An agent that draws a TUI draws its own prompt into the pane.

        Two pi reviewers "reported" the instruction verbatim: the pane wrapped
        mid-sentence so a line began `CHANGES-REQUESTED -- Helm reads...`, and
        the single-line guards missed it because the words that would have
        disqualified it -- the other verdict word, and `FIRST WORD` -- had
        wrapped onto the line above. Helm wrote that brief, so it can tell its
        own instruction from an answer to it.
        """
        root = self.repo("echoed")
        project = self.coordinator.register_project("Echo", str(root), project_id="echoed")
        task = self.coordinator.create_task(project["id"], "produce output")
        worker = self.coordinator.prepare_external_worker(task["id"], [sys.executable, "-c", ""])
        brief = (
            "Finish with one result message whose FIRST WORD is APPROVED or "
            "CHANGES-REQUESTED -- Helm reads that word to decide whether the loop "
            "continues -- followed by your findings."
        )
        # Exactly how the pane wrapped it: the disqualifying words are above.
        Path(worker["log_file"]).write_text(
            "Finish with one result message whose FIRST WORD is APPROVED or\n"
            "CHANGES-REQUESTED -- Helm reads that word to decide whether the loop\n"
            "continues -- followed by your findings.\n"
            "  Working...\n",
            encoding="utf-8",
        )
        adapter = HerdrAdapter(self.coordinator, FakeHerdr())
        self.assertIsNone(
            adapter._verdict_from_output(worker["id"], 0, brief),
            "the reviewer's own instruction was recovered as its verdict",
        )
        # A real verdict in the same pane is still found.
        with Path(worker["log_file"]).open("a", encoding="utf-8") as handle:
            handle.write("APPROVED no blocking findings\n")
        recovered = adapter._verdict_from_output(worker["id"], 0, brief)
        self.assertIsNotNone(recovered)
        self.assertTrue(recovered["text"].startswith("APPROVED"))

    def test_worker_output_is_decoded_once_rather_than_by_every_reader(self) -> None:
        root = self.repo("tailing")
        project = self.coordinator.register_project("Tail", str(root), project_id="tailing")
        task = self.coordinator.create_task(project["id"], "produce output")
        worker = self.coordinator.launch_worker(
            task["id"], [sys.executable, "-c", ""], wait=False
        )
        Path(worker["log_file"]).write_text(
            "\x1b[38;5;153mcoloured line\x1b[0m\n\n\x1b]0;title\x07second line\n"
            # A CSI sequence may carry intermediate bytes before its final
            # letter. Agent CLIs emit this cursor-shape one constantly, and
            # missing it littered "[0 q" through every decoded line.
            "\x1b[0 qthird line\x1b[2 q\n",
            encoding="utf-8",
        )
        out = self.coordinator.worker_output(worker["id"], lines=10)
        # Escapes stripped, blank lines dropped, newest last.
        self.assertEqual(out, ["coloured line", "second line", "third line"])
        self.assertEqual(self.coordinator.worker_output(worker["id"], lines=1), ["third line"])

    def test_reflection_gathers_evidence_without_drawing_conclusions(self) -> None:
        root = self.repo("reflecting")
        project = self.coordinator.register_project("Reflect", str(root), project_id="reflecting")
        task = self.coordinator.create_task(project["id"], "do a thing")
        worker = self.coordinator.launch_worker(
            task["id"], [sys.executable, "-c", ""], wait=False
        )
        self.coordinator.record_worker_message(worker["id"], "blocker", "needs a credential")
        evidence = self.coordinator.reflection_evidence(since_hours=24)
        self.assertEqual(evidence["tasks_created"], 1)
        self.assertIn(task["id"], evidence["tasks_without_domain"])
        self.assertTrue(any("credential" in f["text"] for f in evidence["failures_and_blockers"]))
        # It gathers; it does not judge. The prompt is what asks for judgement,
        # because deciding whether a pattern is a defect needs reading.
        self.assertIn("Reflect on this", evidence["prompt"])
        self.assertNotIn("recommendation", evidence)
        # A narrow window excludes older activity rather than reporting it.
        self.assertEqual(self.coordinator.reflection_evidence(since_hours=0)["tasks_created"], 0)

    def test_a_worker_that_broke_in_its_own_session_is_noticed(self) -> None:
        root = self.repo("breaking")
        project = self.coordinator.register_project("Break", str(root), project_id="breaking")
        task = self.coordinator.create_task(project["id"], "break midway")
        worker = self.coordinator.launch_worker(
            task["id"], [sys.executable, "-c", ""], wait=False
        )
        self.coordinator.record_worker_message(worker["id"], "status", "working")
        Path(worker["log_file"]).write_text(
            "doing the work\nAPI Error: Connection closed mid-response\n", encoding="utf-8"
        )
        # Helm cannot see inside the session, but the evidence was already on
        # disk and simply never read.
        health = {e["worker_id"]: e for e in self.coordinator.worker_health()}
        self.assertEqual(health[worker["id"]]["verdict"], "erroring")
        self.assertIn("API Error", health[worker["id"]]["detail"])

        # A worker that broke but then reported a terminal message has told us
        # itself; its own word wins over a scraped signature.
        self.coordinator.record_worker_message(worker["id"], "result", "recovered and finished")
        after = {e["worker_id"]: e for e in self.coordinator.worker_health()}
        if worker["id"] in after:
            self.assertNotEqual(after[worker["id"]]["verdict"], "erroring")

        # Ordinary output is not a failure: the check must stay narrow or the
        # warning stops meaning anything.
        Path(worker["log_file"]).write_text("compiling\nall tests pass\n", encoding="utf-8")
        self.assertEqual(self.coordinator.worker_failures(worker["id"]), [])

    def test_an_answer_clears_the_input_and_does_not_race_the_newline(self) -> None:
        root = self.repo("answering")
        project = self.coordinator.register_project("Ans", str(root), project_id="answering")
        task = self.coordinator.create_task(project["id"], "wait for an answer")
        herdr = FakeHerdr()
        adapter = HerdrAdapter(self.coordinator, herdr)
        adapter.ANSWER_SETTLE_SECONDS = 0
        worker = adapter.launch_task(task["id"], [sys.executable, "-c", ""], wait=False)
        self.assertTrue(adapter.answer_worker(worker["id"], "carry on"))

        # Escape before the text, because a paste arriving mid-execution puts
        # the session in an interrupted state where the next text never
        # submits; Enter only after it, because sent together the newline
        # races the paste and submits a fragment.
        self.assertEqual([key for _, key in herdr.sent_keys], ["Escape", "Enter"])
        self.assertEqual([text for _, text in herdr.sent_text], ["carry on"])

    def test_run_returns_without_waiting_so_the_session_stays_responsive(self) -> None:
        parser = cli._build_parser()
        default = parser.parse_args(["run", "media", "a task"])
        self.assertTrue(default.asynchronous)
        blocking = parser.parse_args(["run", "media", "a task", "--wait"])
        self.assertFalse(blocking.asynchronous)

    def test_a_worker_can_ask_and_helm_answers_into_its_session(self) -> None:
        root = self.repo("asking")
        project = self.coordinator.register_project("Asking", str(root), project_id="asking")
        task = self.coordinator.create_task(project["id"], "build it")
        herdr = FakeHerdr()
        adapter = HerdrAdapter(self.coordinator, herdr)
        worker = adapter.launch_task(task["id"], [sys.executable, "-c", ""], wait=False)

        # Asking is not blocking: the task keeps running.
        self.coordinator.record_worker_message(worker["id"], "question", "which base branch?")
        self.assertEqual(
            self.coordinator.inspect_task(task["id"])["task"]["status"], "running"
        )

        self.coordinator.record_worker_message(worker["id"], "answer", "branch off main")
        self.assertTrue(adapter.answer_worker(worker["id"], "branch off main"))
        # send-text alone does not submit; Enter must follow as its own call.
        self.assertEqual(herdr.sent_text[-1][1], "branch off main")
        self.assertEqual(herdr.sent_keys[-1][1], "Enter")
        self.assertEqual(herdr.sent_text[-1][0], herdr.sent_keys[-1][0])
        kinds = [m["kind"] for m in self.coordinator.inspect_task(task["id"])["messages"]]
        self.assertIn("question", kinds)
        self.assertIn("answer", kinds)

    def test_worker_context_carries_a_push_reporting_contract(self) -> None:
        root = self.repo("reporting")
        project = self.coordinator.register_project("Reporting", str(root), project_id="reporting")
        task = self.coordinator.create_task(project["id"], "report as you go")
        context = self.coordinator._context(project, task, "w-report")
        reporting = context["reporting"]
        self.assertEqual(reporting["mode"], "push")
        self.assertIn("worker message", reporting["command"])
        self.assertIn("w-report", reporting["command"])
        self.assertIn("status", reporting["types"])
        self.assertIn("blocker", reporting["types"])
        # The worker is told the coordinator is not watching it.
        self.assertTrue(any("does not watch" in line for line in reporting["instructions"]))
        self.assertIn("Report your own progress", CORE_SAFETY_RULES)

    def test_a_worker_sends_confirmations_to_helm_instead_of_pausing(self) -> None:
        root = self.repo("confirmations")
        project = self.coordinator.register_project("Confirm", str(root), project_id="confirm")
        task = self.coordinator.create_task(project["id"], "ask rather than stall")
        reporting = self.coordinator._context(project, task, "w-confirm")["reporting"]
        instructions = " ".join(reporting["instructions"])
        # An agent CLI's habit is to pause and ask the person in front of it.
        # Nobody is in front of it, so the confirmation has to reach Helm.
        for required in ("confirmation", "--type question", "silent stall"):
            self.assertIn(required, instructions)
        self.assertIn("question", reporting["types"])
        rules = CORE_SAFETY_RULES
        self.assertIn("Send every confirmation to Helm", rules)
        self.assertIn("should I proceed?", rules)
        self.assertIn("do not idle waiting", rules)
        # Deciding a confirmation never becomes authority over a protected
        # action: those still reach a human.
        for protected in ("merging, publishing, pushing, deleting", "still require a\n  human"):
            self.assertIn(protected, rules)

    def test_a_herdr_worker_can_route_its_own_pushes(self) -> None:
        root = self.repo("routing-env")
        project = self.coordinator.register_project("Routing", str(root), project_id="routing")
        task = self.coordinator.create_task(project["id"], "push and route")
        herdr = FakeHerdr()
        worker = HerdrAdapter(self.coordinator, herdr).launch_task(
            task["id"], [sys.executable, "-c", ""], wait=False
        )
        config = json.loads(Path(worker["config_file"]).read_text(encoding="utf-8"))
        # Without this marker the worker's own `helm worker message` sees Herdr
        # as unavailable, so a push is recorded but never displayed.
        self.assertEqual(config["worker_env"].get("HERDR_ENV"), "1")

        process_task = self.coordinator.create_task(project["id"], "process worker")
        process_worker = self.coordinator.launch_worker(
            process_task["id"], [sys.executable, "-c", ""], wait=False
        )
        process_config = json.loads(
            Path(process_worker["config_file"]).read_text(encoding="utf-8")
        )
        # A process worker has no pane, so it gets no such marker.
        self.assertNotIn("HERDR_ENV", process_config["worker_env"])

    def test_runner_gives_an_interactive_worker_a_real_terminal(self) -> None:
        config_path = self._runner_config("runner-tty")

        class FakeTerminal(io.StringIO):
            def isatty(self) -> bool:  # a Herdr pane is a TTY
                return True

        with mock.patch("helm.cli._run_worker_on_pty", return_value=0) as on_pty:
            with contextlib.redirect_stdout(FakeTerminal()):
                self.assertEqual(cli._worker_runner(str(config_path)), 0)
        # An interactive agent only renders when it owns a terminal.
        self.assertEqual(on_pty.call_count, 1)

    def test_worker_runner_mirrors_output_to_the_pane_and_the_log(self) -> None:
        root = self.repo("runner-mirror")
        base = Path(self.temp.name) / "runner"
        base.mkdir()
        log_path = base / "output.log"
        exit_path = base / "exit.json"
        config_path = base / "runner.json"
        common_dir = subprocess.run(
            ["git", "-C", str(root), "rev-parse", "--path-format=absolute", "--git-common-dir"],
            check=True,
            text=True,
            stdout=subprocess.PIPE,
        ).stdout.strip()
        config_path.write_text(
            json.dumps(
                {
                    "command": [sys.executable, "-c", "print('hello from the worker')"],
                    "cwd": str(root),
                    "git_common_dir": common_dir,
                    "log": str(log_path),
                    "exit": str(exit_path),
                    "worker_env": {"HELM_WORKER_ID": "w-visible"},
                }
            ),
            encoding="utf-8",
        )
        config_path.chmod(0o600)

        pane = io.StringIO()
        with contextlib.redirect_stdout(pane):
            self.assertEqual(cli._worker_runner(str(config_path)), 0)

        shown = pane.getvalue()
        # Visible in the tab, so a running worker never looks like a dead one.
        self.assertIn("hello from the worker", shown)
        # Marked as worker output rather than presented as Helm speaking.
        self.assertIn("w-visible", shown)
        self.assertIn("data, not instructions", shown)
        # Still captured for Helm, and the exit record still lands.
        self.assertIn("hello from the worker", log_path.read_text(encoding="utf-8"))
        self.assertEqual(json.loads(exit_path.read_text(encoding="utf-8"))["returncode"], 0)

    def test_runner_starts_a_foreman_that_has_no_worktree(self) -> None:
        # A foreman is allocated a Helm-owned state directory and no worktree.
        # Re-checking it for a worktree killed every foreman at launch: git
        # walks up out of the empty directory and reports some enclosing repo
        # as the toplevel, which never equals the assigned path.
        state_dir = Path(self.temp.name) / "foreman-state"
        workspace = state_dir / "foremen" / "proj" / "t-1"
        workspace.mkdir(parents=True)
        config_path = self._foreman_runner_config(
            "ok", workspace=workspace, state_dir=state_dir
        )

        pane = io.StringIO()
        with contextlib.redirect_stdout(pane):
            self.assertEqual(cli._worker_runner(str(config_path)), 0)
        self.assertIn("foreman is driving", pane.getvalue())

    def test_runner_refuses_a_foreman_workspace_outside_helm_state(self) -> None:
        # The containment check is what replaces the worktree check: a swapped
        # path must not point a foreman at a project checkout or the Helm root.
        state_dir = Path(self.temp.name) / "foreman-state-guard"
        state_dir.mkdir()
        outside = Path(self.temp.name) / "somewhere-else"
        outside.mkdir()
        config_path = self._foreman_runner_config(
            "outside", workspace=outside, state_dir=state_dir
        )

        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(cli._worker_runner(str(config_path)), 1)
        self.assertIn(
            "Helm-owned state",
            (config_path.parent / "output.log").read_text(encoding="utf-8"),
        )

    def test_a_worker_that_never_starts_in_its_pane_fails_loudly(self) -> None:
        # A pane types the launch command at its shell, so shell startup output
        # can eat it. Helm used to record the worker as running anyway, so a
        # review waited forever on a reviewer that had never existed.
        root = self.repo("herdr-never-started")
        project = self.coordinator.register_project(
            "Never", str(root), project_id="never"
        )
        herdr = FakeHerdr()
        adapter = HerdrAdapter(self.coordinator, herdr)
        task = self.coordinator.create_task(project["id"], "a task whose pane eats the command")

        # pane_run accepts the command and nothing runs: exactly what a shell
        # prompt swallowing the first character looks like to Helm.
        herdr.runs_start_the_runner = False
        adapter.RUNNER_START_TIMEOUT = 0.5
        with self.assertRaises(HelmError) as raised:
            adapter.launch_task(task["id"], [sys.executable, "-c", ""], wait=False)
        self.assertIn("never started", str(raised.exception))

        worker = next(
            w
            for w in self.coordinator.store.load()["workers"].values()
            if w["task_id"] == task["id"]
        )
        self.assertEqual(worker["status"], "failed")

    def test_external_wait_expiring_does_not_kill_a_working_worker(self) -> None:
        """A budget running out means we stopped waiting, not that it died.

        An agent worker takes minutes.  Failing the assignment on expiry marked
        live workers dead after five seconds and wrote them a returncode-1 exit
        record while they were still working and still pushing messages.
        """
        root = self.repo("slow-herdr")
        project = self.coordinator.register_project("Slow", str(root), project_id="slow")
        task = self.coordinator.create_task(project["id"], "still working")
        adapter = HerdrAdapter(self.coordinator, FakeHerdr())
        worker = adapter.launch_task(task["id"], [sys.executable, "-c", ""], wait=False)

        waited = adapter.wait_worker(worker["id"], timeout=0.01)

        self.assertEqual(waited["status"], "running")
        self.assertIsNone(waited.get("exit_code"))
        # No fabricated exit record: the worker writes its own, and inventing
        # one makes a live worker unrecoverable even after it finishes.
        self.assertFalse(Path(waited["exit_file"]).exists())
        inspected = self.coordinator.inspect_task(task["id"])
        self.assertEqual(inspected["task"]["status"], "running")
        self.assertEqual(
            [m for m in inspected["messages"] if m["kind"] == "failure"], []
        )

    def test_external_wait_recovers_a_worker_whose_pane_disappeared(self) -> None:
        """Real loss is still recovered -- on the provider's evidence, not a clock."""

        class VanishedPaneHerdr(FakeHerdr):
            def pane_status(self, pane_id: str) -> dict[str, object]:
                return {"result": {"pane": {"status": "missing"}}}

        root = self.repo("lost-herdr")
        project = self.coordinator.register_project("Lost", str(root), project_id="lost")
        task = self.coordinator.create_task(project["id"], "lost pane")
        adapter = HerdrAdapter(self.coordinator, VanishedPaneHerdr())
        worker = adapter.launch_task(task["id"], [sys.executable, "-c", ""], wait=False)

        recovered = adapter.wait_worker(worker["id"], timeout=0.01)

        self.assertEqual(recovered["status"], "failed")
        self.assertEqual(recovered["exit_code"], 1)
        self.assertTrue(Path(recovered["exit_file"]).exists())

    def test_a_worker_that_exits_zero_is_never_recorded_as_failed(self) -> None:
        """The flake this fixes, driven through the public path."""
        root = self.repo("exitrace")
        project = self.coordinator.register_project(
            "Exit", str(root), project_id="exitrace"
        )
        for _ in range(6):
            task = self.coordinator.create_task(project["id"], "exit cleanly")
            worker = self.coordinator.launch_worker(
                task["id"], [sys.executable, "-c", "print('done')"]
            )
            self.assertEqual(worker["status"], "completed", worker.get("exit_code"))
            self.assertEqual(
                self.coordinator.inspect_task(task["id"])["task"]["status"], "completed"
            )


def _stamp_ago(seconds: float) -> str:
    moment = _dt.datetime.now(_dt.timezone.utc) - _dt.timedelta(seconds=seconds)
    return moment.isoformat().replace("+00:00", "Z")


class LiveButSilentWorkerTests(HelmTestCase):
    """A silent worker is not a dead one, and a live one is not a healthy one."""

    def _paneless(self, name: str):
        root = self.repo(name)
        project = self.coordinator.register_project(
            name.title(), str(root), project_id=name
        )
        task = self.coordinator.create_task(project["id"], "think for a while")
        worker = self.coordinator.launch_worker(
            task["id"], [sys.executable, "-c", ""], wait=False
        )
        # A Herdr worker has no pid here, which is precisely why output was the
        # only signal and a long model call read as a stall.
        data = self.coordinator.store.load()
        data["workers"][worker["id"]]["execution"] = "herdr"
        self.coordinator.store.save(data)
        return worker

    def _age(self, worker, seconds: float) -> None:
        stamp = time.time() - seconds
        os.utime(worker["log_file"], (stamp, stamp))

    def test_a_live_worker_silent_for_minutes_is_working_not_stalled(self) -> None:
        worker = self._paneless("thinking")
        self._age(worker, 320)  # just past the 300s threshold

        blind = {e["worker_id"]: e for e in self.coordinator.worker_health()}
        self.assertEqual(blind[worker["id"]]["verdict"], "stalled")

        health = {
            e["worker_id"]: e
            for e in self.coordinator.worker_health(liveness=lambda w: True)
        }
        entry = health[worker["id"]]
        self.assertEqual(entry["verdict"], "working")
        self.assertIn("long model call", entry["detail"])

    def test_being_alive_only_buys_a_grace_period_not_silence_forever(self) -> None:
        """The reviewer that looped for hours was alive the whole time.

        The verdict must not claim more than liveness proves: a silent live
        worker may be wedged OR waiting on a slow model, and calling it stuck
        is how a reader is talked into killing work that was only thinking.
        """
        worker = self._paneless("runaway")
        self._age(worker, 4_000)

        health = {
            e["worker_id"]: e
            for e in self.coordinator.worker_health(liveness=lambda w: True)
        }
        entry = health[worker["id"]]
        self.assertEqual(entry["verdict"], "stalled")
        self.assertIn("slow, wedged or looping, not gone", entry["detail"])

    def test_a_provider_that_says_the_session_is_gone_settles_it_as_died(self) -> None:
        worker = self._paneless("gone")
        self._age(worker, 320)
        health = {
            e["worker_id"]: e
            for e in self.coordinator.worker_health(liveness=lambda w: False)
        }
        self.assertEqual(health[worker["id"]]["verdict"], "died")

    def test_an_unavailable_provider_is_not_evidence_either_way(self) -> None:
        worker = self._paneless("unknown")
        self._age(worker, 320)
        for probe in (lambda w: None, lambda w: (_ for _ in ()).throw(RuntimeError("herdr down"))):
            health = {
                e["worker_id"]: e
                for e in self.coordinator.worker_health(liveness=probe)
            }
            entry = health[worker["id"]]
            self.assertEqual(entry["verdict"], "stalled")
            self.assertNotIn("still alive", entry["detail"])

    def test_a_worker_still_producing_output_is_not_a_fault_at_five_minutes(self) -> None:
        """Its log is moving, so it is working; only the protocol push is late.

        This landed in the "needs a human" list at just over five minutes,
        which is an agent mid-edit. An attention list full of healthy workers
        trains its reader to skip it -- the same failure as reporting nothing.
        """
        worker = self._paneless("mid-edit")
        # Output fresh (the log was just written), but nothing reported yet.
        self.coordinator.record_worker_message(worker["id"], "status", "starting")
        data = self.coordinator.store.load()
        # `last_reported_at` is the worker's OWN push, which is what the
        # health check reads -- not the message log.
        data["workers"][worker["id"]]["last_reported_at"] = _stamp_ago(320)
        self.coordinator.store.save(data)

        health = {e["worker_id"]: e for e in self.coordinator.worker_health()}
        entry = health[worker["id"]]
        self.assertEqual(entry["verdict"], "working")
        self.assertIn("within the reporting grace", entry["detail"])

    def test_a_worker_that_never_reports_is_still_surfaced_eventually(self) -> None:
        """A worker that reports nothing is indistinguishable from a dead one."""
        worker = self._paneless("silent")
        self.coordinator.record_worker_message(worker["id"], "status", "starting")
        data = self.coordinator.store.load()
        data["workers"][worker["id"]]["last_reported_at"] = _stamp_ago(4_000)
        self.coordinator.store.save(data)

        health = {e["worker_id"]: e for e in self.coordinator.worker_health()}
        self.assertEqual(health[worker["id"]]["verdict"], "quiet")


class FailureScanReadsTextNotEscapesTests(HelmTestCase):
    """A failure signature must be matched against what a human would read.

    An interactive agent emits terminal capability queries and cursor
    programming continuously. Matching those raw bytes let an escape blob be
    reported as a worker failure -- a reviewer that was writing 31KB every six
    seconds was flagged as `erroring`, with `[>0q+q4d73Gi=31337,s=1,v=1,a` as
    the stated reason. It matters in both directions: fewer false positives,
    and a reported line a human can actually read.
    """

    def test_terminal_escapes_are_not_read_as_failures(self) -> None:
        root = self.repo("escapes")
        project = self.coordinator.register_project(
            "Escapes", str(root), project_id="escapes"
        )
        task = self.coordinator.create_task(project["id"], "emit terminal noise")
        worker = self.coordinator.launch_worker(
            task["id"], [sys.executable, "-c", ""], wait=False
        )
        log = Path(worker["log_file"])
        log.write_text(
            "\x1b[>0q\x1b+q4d73Gi=31337,s=1,v=1,a=q\x1b[>4;0m\x1b[>5u\n"
            "\x1b[?2026h\x1b[?25l\x1b[38;5;60mPercolating\x1b[0m\n",
            encoding="utf-8",
        )
        self.assertEqual(self.coordinator.worker_failures(worker["id"]), [])

    def test_a_real_failure_still_reports_and_reports_readably(self) -> None:
        root = self.repo("realfail")
        project = self.coordinator.register_project(
            "Realfail", str(root), project_id="realfail"
        )
        task = self.coordinator.create_task(project["id"], "fail for real")
        worker = self.coordinator.launch_worker(
            task["id"], [sys.executable, "-c", ""], wait=False
        )
        log = Path(worker["log_file"])
        # A genuine failure, wearing the colour codes a real pane puts on it.
        log.write_text(
            "\x1b[?25l\x1b[31mAPI Error: overloaded, retrying\x1b[0m\x1b[?25h\n",
            encoding="utf-8",
        )
        failures = self.coordinator.worker_failures(worker["id"])
        self.assertEqual(len(failures), 1)
        self.assertEqual(failures[0], "API Error: overloaded, retrying")
        self.assertNotIn("\x1b", failures[0], "the reported reason must be readable")

    def test_prose_about_a_killed_process_is_not_a_killed_process(self) -> None:
        """An agent whose job is writing about failures writes the word often.

        `Killed` as a bare substring matched a foreman DESCRIBING its own fix
        -- "a staleness horizon so a writer killed mid-append cannot wedge the
        day" -- and reported it as a worker that had died. The signature has to
        say WHERE the word must appear, not only what it says.
        """
        root = self.repo("prose")
        project = self.coordinator.register_project(
            "Prose", str(root), project_id="prose"
        )
        task = self.coordinator.create_task(project["id"], "write about failures")
        worker = self.coordinator.launch_worker(
            task["id"], [sys.executable, "-c", ""], wait=False
        )
        log = Path(worker["log_file"])
        log.write_text(
            "a staleness horizon so a writer killed mid-append cannot wedge the day\n"
            "the process was killed by the OOM killer, so the lock must expire\n",
            encoding="utf-8",
        )
        self.assertEqual(self.coordinator.worker_failures(worker["id"]), [])

    def test_the_shell_kill_report_is_still_a_failure(self) -> None:
        """Narrowing must not blind the check to the thing it exists for."""
        root = self.repo("oomed")
        project = self.coordinator.register_project(
            "Oomed", str(root), project_id="oomed"
        )
        task = self.coordinator.create_task(project["id"], "get killed")
        worker = self.coordinator.launch_worker(
            task["id"], [sys.executable, "-c", ""], wait=False
        )
        for report in ("Killed", "Killed: 9", "zsh: killed  node dist/main.js",
                       "Killed process 4821 (node)"):
            Path(worker["log_file"]).write_text(report + "\n", encoding="utf-8")
            self.assertEqual(
                self.coordinator.worker_failures(worker["id"]), [report],
                f"{report!r} is the report this check exists for",
            )
