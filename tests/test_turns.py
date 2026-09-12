"""Turn-based execution: prompts become turns of one resumed session; nothing is typed."""

from __future__ import annotations

import json
import os
import signal
import sys
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
            context = json.loads(Path(worker["context_file"]).read_text())
            self.assertEqual(context["execution"]["mode"], "turns")
            self.assertIn("end your turn", context["execution"]["rules"])
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
