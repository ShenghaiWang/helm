"""Cost is read from the runtime's own transcript, not estimated."""

from __future__ import annotations

import contextlib
import io
import json
import os
import sys
import time
from pathlib import Path
from unittest import mock

from helm import cli, costs
from helm.herdr import HerdrAdapter
from tests.support import FakeHerdr, HelmTestCase


def _transcript(path: Path, session_id: str, turns: list[tuple[int, int, int, int]], model: str = "claude-x") -> None:
    lines = [json.dumps({"type": "summary", "sessionId": session_id})]
    lines.append("not json at all")
    lines.append(json.dumps({"type": "user", "sessionId": session_id, "message": {"role": "user", "content": "hi"}}))
    for i, (inp, out, cr, cw) in enumerate(turns):
        lines.append(json.dumps({
            "type": "assistant", "sessionId": session_id,
            "timestamp": f"2026-09-10T10:0{i}:00.000Z",
            "message": {"role": "assistant", "model": model, "usage": {
                "input_tokens": inp, "output_tokens": out,
                "cache_read_input_tokens": cr, "cache_creation_input_tokens": cw,
            }},
        }))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


class TranscriptUsageTests(HelmTestCase):
    def test_the_slug_is_the_cwd_with_everything_but_alphanumerics_dashed(self) -> None:
        self.assertEqual(
            costs.cwd_slug("/Users/x/projects/helm/state/worktrees/p/t-1"),
            "-Users-x-projects-helm-state-worktrees-p-t-1",
        )
        self.assertEqual(costs.cwd_slug("/Users/x/.claude"), "-Users-x--claude")

    def test_a_transcript_sums_its_assistant_turns_and_nothing_else(self) -> None:
        path = Path(self.temp.name) / "t.jsonl"
        _transcript(path, "s-1", [(10, 20, 300, 40), (5, 5, 100, 0)])
        usage = costs.transcript_usage(path)
        self.assertEqual(usage["turns"], 2)
        self.assertEqual(usage["input_tokens"], 15)
        self.assertEqual(usage["output_tokens"], 25)
        self.assertEqual(usage["cache_read_input_tokens"], 400)
        self.assertEqual(usage["cache_creation_input_tokens"], 40)
        self.assertEqual(usage["models"], ["claude-x"])
        self.assertEqual(usage["session_id"], "s-1")
        self.assertTrue(usage["readable"])
        # An unreadable transcript is zero and says so, never a guess.
        missing = costs.transcript_usage(Path(self.temp.name) / "nope.jsonl")
        self.assertFalse(missing["readable"])
        self.assertEqual(missing["turns"], 0)

    def test_a_worker_is_metered_by_session_id_or_by_its_cwd_since_launch(self) -> None:
        config = Path(self.temp.name) / "claude-config"
        workspace = Path(self.temp.name) / "ws"
        with mock.patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": str(config)}):
            directory = costs.transcript_dir(workspace)
            _transcript(directory / "old.jsonl", "old", [(1, 1, 1, 1)])
            stale = time.time() - 3600
            os.utime(directory / "old.jsonl", (stale, stale))
            _transcript(directory / "s-2.jsonl", "s-2", [(10, 10, 10, 10)])
            worker = {
                "id": "w-1", "agent_id": "claude", "workspace": str(workspace),
                "started_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - 120)),
            }
            # By session id: exactly that file.
            by_id = costs.worker_usage({**worker, "agent_session_id": "s-2"})
            self.assertTrue(by_id["metered"])
            self.assertEqual([t["session_id"] for t in by_id["transcripts"]], ["s-2"])
            # Without one: whatever this cwd wrote since launch, so the hour-old
            # transcript of an earlier session in the same worktree is not counted.
            scanned = costs.worker_usage(worker)
            self.assertEqual([t["session_id"] for t in scanned["transcripts"]], ["s-2"])
            self.assertEqual(scanned["input_tokens"], 10)
            # A runtime Helm cannot meter reports nothing rather than a guess.
            other = costs.worker_usage({**worker, "agent_id": "codex"})
            self.assertFalse(other["metered"])
            self.assertEqual(other["transcripts"], [])


class TaskUsageTests(HelmTestCase):
    def test_a_tasks_usage_includes_its_reviewers_and_the_session_id_is_captured(self) -> None:
        root = self.repo("metered")
        project = self.coordinator.register_project("Metered", str(root), project_id="metered")
        task = self.coordinator.create_task(project["id"], "cost something")
        herdr = FakeHerdr()
        adapter = HerdrAdapter(self.coordinator, herdr)
        adapter.ANSWER_SETTLE_SECONDS = 0
        worker = adapter.launch_task(task["id"], [sys.executable, "-c", ""], wait=False)
        with self.state.locked() as data:
            data["workers"][worker["id"]]["agent_id"] = "claude"
            data["workers"][worker["id"]]["agent"] = "claude"
        pane = self.state.load()["integrations"]["herdr"]["workers"][worker["id"]]["pane_id"]
        herdr.agent_session[pane] = "sess-worker"
        herdr.agent_status[pane] = "working"
        # Any look at the pane records the session id, once.
        adapter.answer_worker(worker["id"], "carry on")
        self.assertEqual(self.state.load()["workers"][worker["id"]]["agent_session_id"], "sess-worker")

        config = Path(self.temp.name) / "claude-config"
        with mock.patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": str(config)}):
            _transcript(costs.transcript_dir(worker["workspace"]) / "sess-worker.jsonl", "sess-worker", [(100, 50, 0, 0)])
            # A reviewer task on claude, with its own transcript, counts too.
            review = self.coordinator.create_task(
                project["id"], "review it", role="reviewer", reviews=task["id"], read_only=True
            )
            with self.state.locked() as data:
                data["workers"]["w-rev"] = {
                    "id": "w-rev", "project_id": project["id"], "task_id": review["id"],
                    "agent_id": "claude", "agent": "claude",
                    "workspace": str(Path(self.temp.name) / "rev-ws"),
                    "status": "completed", "started_at": "2026-09-10T00:00:00Z",
                    "agent_session_id": "sess-rev",
                }
            _transcript(
                costs.transcript_dir(Path(self.temp.name) / "rev-ws") / "sess-rev.jsonl",
                "sess-rev", [(7, 3, 0, 0)],
            )
            usage = self.coordinator.task_usage(task["id"])
            self.assertEqual(usage["total"]["input_tokens"], 107)
            without = self.coordinator.task_usage(task["id"], with_reviews=False)
            self.assertEqual(without["total"]["input_tokens"], 100)
            self.assertFalse(usage["total"]["cost_known"])

            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                code = cli.main(["--state-dir", str(self.state.directory), "task", "cost", task["id"]])
            self.assertEqual(code, 0)
            self.assertIn("total:", out.getvalue())
            self.assertIn("cost: not reported", out.getvalue())
