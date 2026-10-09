"""Turn-based execution: prompts become turns of one resumed session; nothing is typed."""

from __future__ import annotations

import contextlib
import io
import json
import os
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path
from unittest import mock

from helm.herdr import HerdrAdapter
from tests.support import FakeHerdr, HelmTestCase


class TurnsTests(HelmTestCase):
    def _fake_claude(self) -> Path:
        bin_dir = Path(self.temp.name) / "bin"
        bin_dir.mkdir(exist_ok=True)
        script = bin_dir / "claude"
        # Records its argv, then speaks like `claude --print --output-format
        # stream-json`: a system line, an assistant line, a result line.
        script.write_text(
            "#!/usr/bin/env python3\n"
            "import json, sys, os\n"
            "args = sys.argv[1:]\n"
            "with open(os.path.join(os.path.dirname(os.path.abspath(__file__)), 'claude-args.log'), 'a') as h:\n"
            "    h.write(json.dumps(args) + '\\n')\n"
            "session = ''\n"
            "for flag in ('--session-id', '--resume'):\n"
            "    if flag in args: session = args[args.index(flag) + 1]\n"
            "prompt = args[-1]\n"
            "print(json.dumps({'type': 'system', 'session_id': session}))\n"
            "print(json.dumps({'type': 'assistant', 'message': {'content': [{'type': 'text', 'text': 'working on: ' + prompt[:40]}]}}))\n"
            "print(json.dumps({'type': 'result', 'result': 'turn done: ' + prompt[:40], 'session_id': session, 'num_turns': 1}))\n"
        )
        script.chmod(0o755)
        return bin_dir

    def _wait_for(self, path: Path, timeout: float = 20.0) -> None:
        deadline = time.time() + timeout
        while time.time() < deadline:
            if path.exists():
                return
            time.sleep(0.1)
        self.fail(f"{path} never appeared")

    def test_prompts_become_turns_of_one_resumed_session(self) -> None:
        self.write_preferences(execution={"turns": "on"})
        root = self.repo("turned")
        project = self.coordinator.register_project("Turned", str(root), project_id="turned")
        task = self.coordinator.create_task(project["id"], "do the thing", agent="claude")
        bin_dir = self._fake_claude()
        argv_log = bin_dir / "claude-args.log"
        env = {"PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}"}
        with mock.patch.dict(os.environ, env):
            worker = self.coordinator.launch_worker(task["id"], None, wait=False, agent="claude")
            self.assertEqual(worker["execution_mode"], "turns")
            self.assertTrue(worker["agent_session_id"], "Claude Code's session id is chosen up front")
            config = json.loads(Path(worker["config_file"]).read_text())
            self.assertTrue(config["turns"])
            self.assertIn("--session-id", config["turn_start"])
            self.assertIn("--resume", config["turn_resume"])
            # Every turn carries the settings file that keeps the memory files
            # above the workspace out of the session; without it each turn
            # opened with the Helm root's own instructions. The inbox-watch
            # hook stays out, because a turn is woken by being started.
            for template in (config["turn_start"], config["turn_resume"]):
                self.assertIn("--settings", template)
                turn_settings = json.loads(Path(template[template.index("--settings") + 1]).read_text())
                self.assertTrue(turn_settings["claudeMdExcludes"])
                self.assertIs(turn_settings["autoMemoryEnabled"], False)
                self.assertNotIn("hooks", turn_settings)
            context = json.loads(Path(worker["context_file"]).read_text())
            self.assertEqual(context["execution"]["mode"], "turns")
            self.assertIn("end your turn", context["execution"]["rules"])
            # Two workers ended a turn waiting on a background command that
            # nothing wakes them for; the contract has to say so in words.
            self.assertIn("never end a turn waiting on one", context["execution"]["rules"])
            turns_dir = Path(worker["config_file"]).parent / "turns"
            self._wait_for(turns_dir / "1.json")
            first = json.loads((turns_dir / "1.json").read_text())
            self.assertEqual(first["session_id"], worker["agent_session_id"])
            self.assertIn("turn done", first["text"])
            # Helm reads the closing line: session kept, last words recorded.
            time.sleep(0.5)
            self.coordinator.poll_worker(worker["id"])
            live = self.state.load()["workers"][worker["id"]]
            self.assertEqual(live["status"], "running")
            self.assertEqual(live["agent_session_id"], worker["agent_session_id"])
            self.assertTrue(any(
                m["kind"] == "status" and m["text"].startswith("turn 1 ended")
                for m in self.state.load()["messages"] if m.get("worker_id") == worker["id"]
            ))
            # An answer is the next turn's prompt, never a keystroke.
            adapter = HerdrAdapter(self.coordinator, FakeHerdr())
            self.assertEqual(adapter.answer_worker(worker["id"], "branch off main"), "turned")
            self._wait_for(turns_dir / "2.json")
            runs = [json.loads(line) for line in argv_log.read_text().splitlines()]
            self.assertEqual(len(runs), 2)
            self.assertIn("--session-id", runs[0])
            self.assertIn("--resume", runs[1])
            self.assertEqual(runs[1][runs[1].index("--resume") + 1], worker["agent_session_id"])
            self.assertEqual(runs[1][-1], "branch off main")
            # Told to stop, the runner writes the exit record and the poll settles it.
            self.coordinator.stop_turns(worker["id"])
            self._wait_for(Path(worker["exit_file"]))
            self.coordinator.poll_worker(worker["id"])
            self.assertNotEqual(self.state.load()["workers"][worker["id"]]["status"], "running")

    def test_a_dead_runner_is_restarted_and_resumes(self) -> None:
        self.write_preferences(execution={"turns": "on"})
        root = self.repo("revived")
        project = self.coordinator.register_project("Revived", str(root), project_id="revived")
        task = self.coordinator.create_task(project["id"], "do the thing", agent="claude")
        bin_dir = self._fake_claude()
        argv_log = bin_dir / "claude-args.log"
        env = {"PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}"}
        with mock.patch.dict(os.environ, env):
            worker = self.coordinator.launch_worker(task["id"], None, wait=False, agent="claude")
            turns_dir = Path(worker["config_file"]).parent / "turns"
            self._wait_for(turns_dir / "1.json")
            self._wait_for(turns_dir / "runner.pid")
            pid = int((turns_dir / "runner.pid").read_text().strip())
            os.kill(pid, signal.SIGKILL)
            deadline = time.time() + 10
            while time.time() < deadline:
                try:
                    os.kill(pid, 0)
                    time.sleep(0.1)
                except ProcessLookupError:
                    break
            adapter = HerdrAdapter(self.coordinator, FakeHerdr())
            self.assertFalse(adapter.turns_runner_alive(worker["id"]))
            # A message arriving now restarts the runner, which resumes the session.
            self.assertEqual(adapter.answer_worker(worker["id"], "carry on"), "turned")
            self._wait_for(turns_dir / "2.json")
            runs = [json.loads(line) for line in argv_log.read_text().splitlines()]
            self.assertIn("--resume", runs[-1])
            self.assertEqual(runs[-1][-1], "carry on")
            restarted = self.state.load()["workers"][worker["id"]]
            self.assertTrue(restarted.get("turn_restarts"))
            self.coordinator.stop_turns(worker["id"])
            self._wait_for(Path(worker["exit_file"]))

    def test_a_project_pin_or_the_preference_chooses_the_mode(self) -> None:
        root = self.repo("pinned")
        project = self.coordinator.register_project("Pinned", str(root), project_id="pinned")
        self.assertEqual(self.coordinator._execution_mode(project, "herdr"), "session")
        self.write_preferences(execution={"turns": "on"})
        self.assertEqual(self.coordinator._execution_mode(project, "herdr"), "turns")
        self.assertEqual(self.coordinator._execution_mode({**project, "execution": "session"}, "herdr"), "session")
        self.assertEqual(self.coordinator._execution_mode(project, "external"), "session")

    def test_a_stop_lets_the_turn_in_progress_finish_and_keeps_its_record(self) -> None:
        self.write_preferences(execution={"turns": "on"})
        root = self.repo("graceful")
        project = self.coordinator.register_project("Graceful", str(root), project_id="graceful")
        task = self.coordinator.create_task(project["id"], "do the thing", agent="claude")
        bin_dir = self._fake_claude()
        # A turn that takes a moment: long enough to be mid-turn when stopped.
        script = bin_dir / "claude"
        script.write_text(script.read_text().replace("prompt = args[-1]\n", "import time; time.sleep(2)\nprompt = args[-1]\n"))
        env = {"PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}"}
        with mock.patch.dict(os.environ, env):
            worker = self.coordinator.launch_worker(task["id"], None, wait=False, agent="claude")
            turns_dir = Path(worker["config_file"]).parent / "turns"
            self._wait_for(turns_dir / "runner.pid")
            self.assertFalse((turns_dir / "1.json").exists())
            self.coordinator.stop_worker(worker["id"], "done with it")
        self.assertTrue((turns_dir / "1.json").exists(), "the turn in progress was allowed to end")
        record = json.loads(Path(worker["exit_file"]).read_text())
        self.assertEqual(record.get("returncode"), 0, record)
        self.assertNotEqual(self.state.load()["workers"][worker["id"]]["status"], "running")

    def test_a_report_from_inside_the_turn_leaves_its_own_runner_to_finish(self) -> None:
        """A worker's own report is part of the turn a stop would wait for.

        The foreman's `report result` released its own tab: it waited the
        whole grace on its own runner, then closed the pane on a turn still
        writing its record. From inside the turn, a stop is only left for the
        runner to find between turns.
        """
        self.write_preferences(execution={"turns": "on"})
        root = self.repo("selfreport")
        project = self.coordinator.register_project("SelfReport", str(root), project_id="selfreport")
        task = self.coordinator.create_task(project["id"], "do the thing", agent="claude")
        bin_dir = self._fake_claude()
        script = bin_dir / "claude"
        script.write_text(script.read_text().replace("prompt = args[-1]\n", "import time; time.sleep(2)\nprompt = args[-1]\n"))
        env = {"PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}"}
        with mock.patch.dict(os.environ, env):
            worker = self.coordinator.launch_worker(task["id"], None, wait=False, agent="claude")
            turns_dir = Path(worker["config_file"]).parent / "turns"
            self._wait_for(turns_dir / "runner.pid")
            pid = int((turns_dir / "runner.pid").read_text().strip())
            self.assertFalse(self.coordinator.inside_own_turn(worker))
            with mock.patch.dict(os.environ, {"HELM_WORKER_ID": worker["id"]}):
                self.assertTrue(self.coordinator.inside_own_turn(worker))
                started = time.monotonic()
                self.coordinator._let_turns_finish(worker)
                self.assertLess(time.monotonic() - started, 1.0, "waited on the turn this command is part of")
            self.assertTrue((turns_dir / "stop").exists())
            self.assertTrue(self.coordinator._pid_alive(pid), "the runner was left to finish its turn")
            # Left alone, the runner ends the turn, keeps its record, and exits.
            self._wait_for(turns_dir / "1.json")
            self._wait_for(Path(worker["exit_file"]))
            record = json.loads(Path(worker["exit_file"]).read_text())
            self.assertEqual(record.get("returncode"), 0, record)
            self.coordinator.poll_worker(worker["id"])
            self.assertNotEqual(self.state.load()["workers"][worker["id"]]["status"], "running")

    def _idle_turns_worker(self, name: str) -> tuple[dict, Path, Path]:
        """A turns worker whose first turn has ended, with its runner then killed."""
        self.write_preferences(execution={"turns": "on"})
        root = self.repo(name)
        project = self.coordinator.register_project(name.title(), str(root), project_id=name)
        task = self.coordinator.create_task(project["id"], "do the thing", agent="claude")
        bin_dir = self._fake_claude()
        worker = self.coordinator.launch_worker(task["id"], None, wait=False, agent="claude")
        turns_dir = Path(worker["config_file"]).parent / "turns"
        self._wait_for(turns_dir / "1.json")
        self._wait_for(turns_dir / "runner.pid")
        pid = int((turns_dir / "runner.pid").read_text().strip())
        os.kill(pid, signal.SIGKILL)
        deadline = time.time() + 10
        while time.time() < deadline:
            try:
                os.kill(pid, 0)
                time.sleep(0.1)
            except ProcessLookupError:
                break
        return worker, turns_dir, bin_dir

    def test_an_answer_to_an_idle_lead_whose_runner_is_gone_starts_a_turn_and_says_so(self) -> None:
        """The lost answer: reported as queued, while no turn would ever start.

        `worker answer` printed "queued as the prompt of its next turn; the
        runner starts it" for every turns delivery. Behind it, the runner's
        liveness was read from a pid -- the record's, which `adopt_worker_pid`
        fills with the first process naming the worker's state directory (a
        `tail -f` of its log will do) -- so a dead runner read as alive and was
        never restarted. A stop file left behind would have made a restarted
        one exit before reading its queue anyway. The lead sat idle for two
        hours on an answer it never received.
        """
        from helm import cli

        bin_dir = Path(self.temp.name) / "bin"
        env = {"PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}"}
        with mock.patch.dict(os.environ, env):
            worker, turns_dir, bin_dir = self._idle_turns_worker("lostanswer")
            # The runner is dead; a live, unrelated process now answers to the
            # pid on its record, and an old stop file sits in its directory.
            bystander = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)"])
            self.addCleanup(bystander.wait)
            self.addCleanup(bystander.kill)
            with self.coordinator.store.locked() as data:
                data["workers"][worker["id"]]["pid"] = bystander.pid
            stop = turns_dir / "stop"
            stop.write_text("old\n", encoding="utf-8")
            stale = time.time() - 3600
            os.utime(stop, (stale, stale))
            adapter = HerdrAdapter(self.coordinator, FakeHerdr())
            self.assertFalse(
                adapter.turns_runner_alive(worker["id"]),
                "a live pid that is not the runner was read as the runner",
            )
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                code = cli.main([
                    "--state-dir", str(self.state.directory),
                    "worker", "answer", worker["id"], "--text", "carry on with step two",
                ])
            self.assertEqual(code, 0, out.getvalue())
            self.assertIn("delivered: turn started by runner pid", out.getvalue())
            self.assertNotIn("the runner starts it", out.getvalue())
            self._wait_for(turns_dir / "2.json")
            runs = [json.loads(line) for line in (bin_dir / "claude-args.log").read_text().splitlines()]
            self.assertEqual(runs[-1][-1], "carry on with step two")
            self.assertFalse(stop.exists(), "the stale stop was cleared, not obeyed")
            self.coordinator.stop_turns(worker["id"])
            self._wait_for(Path(worker["exit_file"]))

    def test_an_answer_nothing_will_run_is_reported_as_not_started(self) -> None:
        """A stop in progress means no turn will start; the reply says that, and fails."""
        from helm import cli

        bin_dir = Path(self.temp.name) / "bin"
        env = {"PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}"}
        with mock.patch.dict(os.environ, env):
            worker, turns_dir, _ = self._idle_turns_worker("strandedanswer")
            self.coordinator.stop_turns(worker["id"])
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                code = cli.main([
                    "--state-dir", str(self.state.directory),
                    "worker", "answer", worker["id"], "--text", "are you there",
                ])
        self.assertEqual(code, 1, out.getvalue())
        self.assertIn("NOT started", out.getvalue())
        self.assertIn("a stop is in progress", out.getvalue())
        # Still recorded and still in its inbox: nothing was dropped, only not run.
        self.assertTrue(self.coordinator.turn_entry_queued(
            worker["id"],
            json.loads((turns_dir / "next.json").read_text())[-1]["id"],
        ))

    def test_a_prompt_queued_while_the_runner_takes_the_queue_is_not_lost(self) -> None:
        """The runner read the queue, then unlinked it; a prompt written between went with it."""
        from helm import cli

        root = self.repo("queuerace")
        project = self.coordinator.register_project("Queuerace", str(root), project_id="queuerace")
        task = self.coordinator.create_task(project["id"], "do the thing")
        worker = self.coordinator.prepare_external_worker(task["id"], [sys.executable, "-c", ""])
        turns_dir = self.coordinator.turns_dir(worker["id"])
        self.coordinator.deliver_turn(worker["id"], "first")
        state: dict = {"turn": 1, "history": []}
        late: list[threading.Thread] = []
        real_write = cli._write_private_text

        def write_then_race(path, content):
            # The moment between the runner's read and its unlink: another
            # Helm command queues an answer exactly now.
            if not late:
                thread = threading.Thread(
                    target=self.coordinator.deliver_turn, args=(worker["id"], "second")
                )
                late.append(thread)
                thread.start()
                time.sleep(0.3)
            real_write(path, content)

        with mock.patch.object(cli, "_write_private_text", side_effect=write_then_race):
            taken = cli._take_turn_prompt(turns_dir, state, turns_dir / "state.json")
        late[0].join(timeout=10)
        self.assertEqual(taken, "first")
        self.assertEqual(state["pending_prompt"], "first", "recorded before the queue went")
        queued = json.loads((turns_dir / "next.json").read_text())
        self.assertEqual([entry["text"] for entry in queued], ["second"])

    def test_a_second_runner_for_the_same_worker_leaves_without_an_exit_record(self) -> None:
        """A restart that races a live runner must not run turns beside it, or end the worker."""
        from helm import cli
        from helm.paths import hold_turns_runner_lock

        turns_dir = Path(self.temp.name) / "dup-turns"
        held = hold_turns_runner_lock(turns_dir)
        self.assertIsNotNone(held)
        try:
            config = {
                "turns_dir": str(turns_dir),
                "initial_prompt": "go",
                "turn_start": [sys.executable, "-c", "raise SystemExit(3)"],
                "turn_resume": [],
            }
            with self.assertRaises(cli._RunnerAlreadyRunning):
                cli._run_turns(config, self.temp.name, dict(os.environ), io.StringIO())
            self.assertFalse((turns_dir / "1.json").exists())
        finally:
            os.close(held)
