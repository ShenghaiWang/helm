"""Keeping a task's pull-request record in step with the remote.

`pr-open` is deliberately not final: checks, review comments and human
replies can still arrive. But nothing used to read the remote unless someone
ran `helm task pr-sync` by hand, so a PR that merged hours ago still read as
open, kept its cleanup decision from being raised, and held its worktree --
eighteen hours and 28 GB, once. So the sync is a step `helm watch` and the
watchdog take on their own, bounded to one read per task per interval, and
quiet about a remote it cannot reach: an offline laptop is not an event.
"""

from __future__ import annotations

import contextlib
import datetime as _dt
import json
import shutil
import subprocess
from pathlib import Path
from typing import Any

from ..errors import HelmError, SafetyError
from ..values import PROVENANCE_MARKER

#: How often the automatic sync reads one task's PR. Ten minutes is well
#: inside the time a merged PR used to sit unnoticed, and well outside the
#: cadence at which a forge would mind being asked.
PR_SYNC_INTERVAL_SECONDS = 600.0


def _epoch(stamp: Any) -> float:
    if not isinstance(stamp, str) or not stamp:
        return 0.0
    try:
        return _dt.datetime.fromisoformat(stamp.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return 0.0


class PullRequestsMixin:
    def read_pull_request(self, url: str, *, cwd: Path) -> dict[str, Any]:
        """What the forge says about a PR, through gh. Raises when it cannot say."""
        if shutil.which("gh") is None:
            raise HelmError("gh is not installed; record PR observations with helm task pr-status")
        result = subprocess.run(
            ["gh", "pr", "view", url, "--json", "url,state,reviewDecision,mergeStateStatus,mergeCommit,comments,body"],
            cwd=str(cwd), text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, check=False, timeout=60,
        )
        if result.returncode != 0:
            raise HelmError(result.stdout.strip() or "gh pr view failed")
        try:
            payload = json.loads(result.stdout or "{}")
        except ValueError as exc:
            raise HelmError(f"gh pr view returned something other than JSON: {exc}") from exc
        return payload if isinstance(payload, dict) else {}

    def sync_pull_request(self, task_id: str) -> dict[str, Any]:
        """Read the task's PR with gh and record the observed delivery state."""
        data = self.store.load()
        task = self._task(data, task_id)
        project = self._project(data, task["project_id"])
        delivery = task.get("delivery") or {}
        url = str(delivery.get("url") or "").strip()
        if not url:
            raise HelmError(
                "task has no recorded PR URL; record it with helm task pr-status --state open --url ..."
            )
        payload = self.read_pull_request(url, cwd=Path(project["root"]))
        state = str(payload.get("state") or "OPEN").lower()
        merge_commit = payload.get("mergeCommit") or {}
        if isinstance(merge_commit, dict):
            merge_commit = str(merge_commit.get("oid") or "")
        comments = payload.get("comments") or []
        self._note_provenance(task_id, project, str(payload.get("body") or ""))
        return self.record_pr_status(
            task_id,
            state="merged" if state == "merged" else "closed" if state == "closed" else "open",
            url=str(payload.get("url") or url),
            comments=len(comments) if isinstance(comments, list) else None,
            checks=str(payload.get("mergeStateStatus") or ""),
            review_decision=str(payload.get("reviewDecision") or ""),
            merge_commit=str(merge_commit or ""),
        )

    def _note_provenance(self, task_id: str, project: dict[str, Any], body: str) -> None:
        """Record whether the PR body carries Helm's provenance block; say so once.

        Helm writes nothing to the PR. A body without the block is noted on
        the project's record the first time it is seen, with the command that
        prints the block for the foreman to add.
        """
        present = PROVENANCE_MARKER in body
        with self.store.locked() as data:
            task = data.get("tasks", {}).get(task_id)
            if task is None:
                return
            delivery = task.setdefault("delivery", {})
            before = delivery.get("provenance")
            delivery["provenance"] = "present" if present else "missing"
        if not present and before != "missing":
            with contextlib.suppress(HelmError, OSError):
                self.record_situation(
                    project["id"],
                    f"PR for task {task_id} carries no provenance block; "
                    f"helm task provenance {task_id} prints it for the PR body",
                )

    def sync_open_pull_requests(
        self, *, min_interval_seconds: float = PR_SYNC_INTERVAL_SECONDS, now_epoch: float | None = None
    ) -> dict[str, Any]:
        """Read every `pr-open` task's PR that has not been read lately.

        Quiet by design: a task whose PR cannot be read now -- no gh, no
        network, a URL the forge rejects -- is listed as skipped with the
        reason and tried again next time, and nothing about the record
        changes. A merge is recorded exactly as `helm task pr-sync` records
        it, which raises the cleanup decision and lets the project's space
        close.
        """
        import time

        current = time.time() if now_epoch is None else now_epoch
        data = self.store.load()
        due = []
        for task_id, task in data.get("tasks", {}).items():
            if task.get("status") != "pr-open":
                continue
            delivery = task.get("delivery") or {}
            if not str(delivery.get("url") or "").strip():
                continue
            if current - _epoch(delivery.get("last_checked_at")) < min_interval_seconds:
                continue
            due.append(task_id)
        outcome: dict[str, Any] = {"checked": [], "merged": [], "closed": [], "skipped": []}
        if due and shutil.which("gh") is None:
            outcome["skipped"] = [{"task_id": t, "reason": "gh is not installed"} for t in due]
            return outcome
        for task_id in sorted(due):
            try:
                synced = self.sync_pull_request(task_id)
            except (HelmError, SafetyError, OSError, subprocess.SubprocessError) as exc:
                outcome["skipped"].append({"task_id": task_id, "reason": str(exc)[:200]})
                continue
            outcome["checked"].append(task_id)
            if synced.get("status") == "pr-merged":
                outcome["merged"].append(task_id)
            elif (synced.get("delivery") or {}).get("state") == "pr-closed":
                outcome["closed"].append(task_id)
        return outcome
