"""A launched worker must not be reachable from the launcher's process tree."""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import time

from tests.support import HelmTestCase


class RunnerDetachTests(HelmTestCase):
    def test_worker_runner_is_reparented_away_from_the_launcher(self) -> None:
        # A caller that walks its own process tree by parent pid -- an agent
        # harness cancelling the tool call that ran `helm review` -- killed
        # the reviewer two seconds after launch. Being in a new session was
        # not enough; the runner has to leave the tree entirely.
        root = self.repo("detach")
        project = self.coordinator.register_project("Detach", str(root), project_id="detach")
        task = self.coordinator.create_task(project["id"], "sit still")
        worker = self.coordinator.launch_worker(
            task["id"], [sys.executable, "-c", "import time; time.sleep(30)"], wait=False
        )
        pid = worker["pid"]
        self.assertIsInstance(pid, int)
        try:
            os.kill(pid, 0)  # the recorded pid is the process that is running
            ppid = subprocess.run(
                ["ps", "-o", "ppid=", "-p", str(pid)], text=True, capture_output=True
            ).stdout.strip()
            self.assertTrue(ppid, "runner pid is not visible to ps")
            self.assertNotEqual(int(ppid), os.getpid())
            # Signalling the launcher's own process group must not touch it.
            with self.subTest("survives a signal to the launcher's group"):
                self.assertNotEqual(os.getpgid(pid), os.getpgid(os.getpid()))
            # The runner reaps its own worker child: the agent's parent is the
            # runner, not the launcher, so a walk from here finds neither.
            children = subprocess.run(
                ["pgrep", "-P", str(os.getpid())], text=True, capture_output=True
            ).stdout.split()
            self.assertNotIn(str(pid), children)
        finally:
            with self.subTest("stop still reaches the detached runner"):
                self.coordinator.stop_worker(worker["id"])
                for _ in range(50):
                    try:
                        os.kill(pid, 0)
                    except ProcessLookupError:
                        break
                    time.sleep(0.1)
                else:
                    os.kill(pid, signal.SIGKILL)
                    self.fail("stop_worker did not end the detached runner")
