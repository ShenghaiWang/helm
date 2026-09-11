"""Open pull requests are read on their own, bounded, and quietly when offline."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path
from unittest import mock

from helm import cli
from tests.support import HelmTestCase


class PullRequestSyncTests(HelmTestCase):
    def _fake_gh(self, state: str) -> Path:
        bin_dir = Path(self.temp.name) / "bin"
        bin_dir.mkdir(exist_ok=True)
        gh = bin_dir / "gh"
        payload = json.dumps({
            "url": "https://example.test/pull/7", "state": state, "reviewDecision": "APPROVED",
            "mergeStateStatus": "CLEAN", "mergeCommit": {"oid": "abc123"}, "comments": [{"body": "nice"}],
        })
        gh.write_text(f"#!/bin/sh\nprintf '%s' '{payload}'\n")
        gh.chmod(0o755)
        return bin_dir

    def _open_pr_task(self, name: str) -> dict:
        root = self.repo(name)
        project = self.coordinator.register_project(name.title(), str(root), project_id=name, delivery_policy="pr")
        task = self.coordinator.create_task(project["id"], "ship it")
        code = (
            "from pathlib import Path; import subprocess; Path('c.txt').write_text('w'); "
            "subprocess.run(['git','add','c.txt'],check=True); subprocess.run(['git','commit','-qm','c'],check=True)"
        )
        self.coordinator.launch_worker(task["id"], [sys.executable, "-c", code])
        self.coordinator.record_pr_status(task["id"], state="open", url="https://example.test/pull/7")
        return self.state.load()["tasks"][task["id"]]

    def test_an_open_pr_that_merged_is_recorded_without_anyone_asking(self) -> None:
        task = self._open_pr_task("synced")
        self.assertEqual(task["status"], "pr-open")
        with mock.patch.dict(os.environ, {"PATH": f"{self._fake_gh('MERGED')}{os.pathsep}{os.environ['PATH']}"}):
            # Too soon after the last look: nothing is read.
            self.assertEqual(self.coordinator.sync_open_pull_requests()["checked"], [])
            outcome = self.coordinator.sync_open_pull_requests(min_interval_seconds=0)
        self.assertEqual(outcome["merged"], [task["id"]])
        after = self.state.load()["tasks"][task["id"]]
        self.assertEqual(after["status"], "pr-merged")
        self.assertEqual(after["delivery"]["merge_commit"], "abc123")

    def test_a_remote_that_cannot_be_read_is_skipped_quietly(self) -> None:
        task = self._open_pr_task("offline")
        bin_dir = Path(self.temp.name) / "nogh"
        bin_dir.mkdir()
        with mock.patch.dict(os.environ, {"PATH": str(bin_dir)}):
            outcome = self.coordinator.sync_open_pull_requests(min_interval_seconds=0)
        self.assertEqual(outcome["checked"], [])
        self.assertEqual(outcome["skipped"][0]["task_id"], task["id"])
        self.assertIn("gh is not installed", outcome["skipped"][0]["reason"])
        self.assertEqual(self.state.load()["tasks"][task["id"]]["status"], "pr-open")
        failing = Path(self.temp.name) / "badgh"
        failing.mkdir()
        (failing / "gh").write_text("#!/bin/sh\necho 'could not resolve host' >&2\nexit 1\n")
        (failing / "gh").chmod(0o755)
        with mock.patch.dict(os.environ, {"PATH": f"{failing}{os.pathsep}{os.environ['PATH']}"}):
            outcome = self.coordinator.sync_open_pull_requests(min_interval_seconds=0)
        self.assertEqual(outcome["checked"], [])
        self.assertIn("could not resolve host", outcome["skipped"][0]["reason"])

    def test_watch_and_the_by_hand_command_both_sync(self) -> None:
        task = self._open_pr_task("watched")
        with mock.patch.dict(os.environ, {"PATH": f"{self._fake_gh('OPEN')}{os.pathsep}{os.environ['PATH']}"}):
            from contextlib import redirect_stdout
            import io
            out = io.StringIO()
            with redirect_stdout(out):
                self.assertEqual(cli.main(["--state-dir", str(self.state.directory), "task", "pr-sync", task["id"]]), 0)
            self.assertIn("Synced PR", out.getvalue())
            # watch reads it too, once the interval has passed.
            data = self.state.load()
            data["tasks"][task["id"]]["delivery"]["last_checked_at"] = "2020-01-01T00:00:00Z"
            self.state.save(data)
            with mock.patch.object(type(self.coordinator), "sync_open_pull_requests", autospec=True,
                                   side_effect=lambda self_, **kw: {"checked": [task["id"]], "merged": [], "closed": [], "skipped": []}) as synced:
                out = io.StringIO()
                with redirect_stdout(out):
                    cli.main(["--state-dir", str(self.state.directory), "watch"])
            self.assertTrue(synced.called)
            self.assertIn("PR sync: 1 checked", out.getvalue())
