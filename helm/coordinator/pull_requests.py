"""Keeping a task's pull-request record in step with the remote.

`pr-open` is deliberately not final: checks, review comments and human
replies can still arrive. But nothing used to read the remote unless someone
ran `helm task pr-sync` by hand, so a PR that merged hours ago still read as
open, kept its cleanup decision from being raised, and held its worktree --
eighteen hours and 28 GB, once. So the sync is a step `helm watch` and the
watchdog take on their own, bounded to one read per task per interval, and
quiet about a remote it cannot reach: an offline laptop is not an event.

The same read is the PR watch. A lead under turns exists only inside a turn,
so a red check or a reviewer's thread reached nobody until someone looked;
the watch compares each open PR with what it saw last (`pr_watch`) and keeps
one event per task for the CLI to deliver into the lead that owns it.
"""

from __future__ import annotations

import contextlib
import datetime as _dt
import json
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any

from ..errors import HelmError, SafetyError
from ..naming import task_name
from ..values import PR_WATCH_INSTRUCTIONS, PR_WATCH_QUIET_INSTRUCTIONS, PROVENANCE_MARKER, now
from .pr_watch import (
    ACTIONABLE_KINDS,
    PR_WATCH_FIELDS,
    REVIEW_THREADS_QUERY,
    parse_review_threads,
    pr_watch_changes,
    pr_watch_fingerprint,
    watch_error_kind,
)

#: How often the automatic sync reads one task's PR. Ten minutes is well
#: inside the time a merged PR used to sit unnoticed, and well outside the
#: cadence at which a forge would mind being asked.
PR_SYNC_INTERVAL_SECONDS = 600.0

#: How often the PR watch reads one open PR's checks and review threads. A
#: lead waiting on CI hears about a red check within a couple of minutes, and
#: a dozen open PRs cost a dozen reads every two minutes, not every poll.
PR_WATCH_INTERVAL_SECONDS = 120.0

#: Bounds on what the watch keeps per task beyond its snapshot.
_WATCH_MAX_CHANGES = 20
_WATCH_MAX_DELIVERED = 10


def _epoch(stamp: Any) -> float:
    if not isinstance(stamp, str) or not stamp:
        return 0.0
    try:
        return _dt.datetime.fromisoformat(stamp.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return 0.0


class PullRequestsMixin:
    def read_pull_request(self, url: str, *, cwd: Path, fields: str = "") -> dict[str, Any]:
        """What the forge says about a PR, through gh. Raises when it cannot say."""
        if shutil.which("gh") is None:
            raise HelmError("gh is not installed; record PR observations with helm task pr-status")
        result = subprocess.run(
            [
                "gh", "pr", "view", url, "--json",
                fields or "url,state,reviewDecision,mergeStateStatus,mergeCommit,headRefOid,comments,body",
            ],
            cwd=str(cwd), text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, check=False, timeout=60,
        )
        if result.returncode != 0:
            raise HelmError(result.stdout.strip() or "gh pr view failed")
        try:
            payload = json.loads(result.stdout or "{}")
        except ValueError as exc:
            raise HelmError(f"gh pr view returned something other than JSON: {exc}") from exc
        return payload if isinstance(payload, dict) else {}

    def read_review_threads(self, url: str, *, cwd: Path) -> list[dict[str, Any]] | None:
        """The PR's review threads, resolved or not; None when they cannot be read.

        Never raises: the threads are what makes a review comment actionable,
        but a forge that answers `pr view` and not GraphQL still has checks
        and a decision worth reporting.
        """
        match = re.match(r"https?://([^/]+)/([^/]+)/([^/]+)/pull/(\d+)", url.strip())
        if match is None or shutil.which("gh") is None:
            return None
        host, owner, name, number = match.groups()
        command = ["gh", "api", "graphql"]
        if host.lower() != "github.com":
            command += ["--hostname", host]
        command += [
            "-f", f"query={REVIEW_THREADS_QUERY}",
            "-F", f"owner={owner}", "-F", f"name={name}", "-F", f"number={number}",
        ]
        try:
            result = subprocess.run(
                command, cwd=str(cwd), text=True, stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL, check=False, timeout=60,
            )
            if result.returncode != 0:
                return None
            return parse_review_threads(json.loads(result.stdout or "{}"))
        except (OSError, subprocess.SubprocessError, ValueError):
            return None

    def sync_pull_request(
        self, task_id: str, *, payload: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        """Read the task's PR with gh and record the observed delivery state.

        `payload` is a read the caller already made -- the watch's -- so one
        pass costs the forge one read, not two.
        """
        data = self.store.load()
        task = self._task(data, task_id)
        project = self._project(data, task["project_id"])
        delivery = task.get("delivery") or {}
        url = str(delivery.get("url") or "").strip()
        if not url:
            raise HelmError(
                "task has no recorded PR URL; record it with helm task pr-status --state open --url ..."
            )
        if payload is None:
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
            # What the forge says the PR's head was: with a merge, the commit
            # cleanup treats as delivered, and nothing beyond it.
            head_commit=str(payload.get("headRefOid") or ""),
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
        self,
        *,
        min_interval_seconds: float = PR_SYNC_INTERVAL_SECONDS,
        now_epoch: float | None = None,
        watch_interval_seconds: float = PR_WATCH_INTERVAL_SECONDS,
    ) -> dict[str, Any]:
        """Read every `pr-open` task's PR that is due a look, and watch it.

        Two clocks, one read. The PR WATCH reads each open PR at most every
        `watch_interval_seconds` -- checks, review decision, review threads,
        comments -- and when what it sees changed in a way a lead has to know
        about, keeps ONE coalesced event on the task for the caller to
        deliver to the task's lead (`events` in the result). Under turns a
        lead only exists inside a turn, so nothing else would wake it when CI
        goes red or a reviewer writes. The SYNC records the merge state
        through `record_pr_status` every `min_interval_seconds`, or at once
        when the watch sees the PR merged or closed; a merge is recorded
        exactly as `helm task pr-sync` records it, which raises the cleanup
        decision and lets the project's space close.

        Quiet by design: a PR that cannot be read now -- no network, a URL
        the forge rejects -- is listed as skipped and tried again next
        interval. gh missing or logged out is recorded on the task and
        surfaced to the commander once, because that one does not mend
        itself. An event that could not be delivered stays on the task and
        comes back in `events` once the watch interval has passed again.
        """
        import time

        current = time.time() if now_epoch is None else now_epoch
        stamp = _dt.datetime.fromtimestamp(current, _dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        data = self.store.load()
        due: list[tuple[str, bool]] = []
        waiting: list[str] = []
        for task_id, task in data.get("tasks", {}).items():
            if task.get("status") != "pr-open":
                continue
            delivery = task.get("delivery") or {}
            if not str(delivery.get("url") or "").strip():
                continue
            watch = delivery.get("watch") or {}
            sync_due = current - _epoch(delivery.get("last_checked_at")) >= min_interval_seconds
            last_look = max(_epoch(watch.get("last_polled_at")), _epoch(watch.get("since")))
            if sync_due or current - last_look >= watch_interval_seconds:
                due.append((task_id, sync_due))
            elif watch.get("outbox") and (
                current - _epoch(watch["outbox"].get("attempted_at")) >= watch_interval_seconds
            ):
                waiting.append(task_id)
        outcome: dict[str, Any] = {
            "checked": [], "merged": [], "closed": [], "skipped": [], "watched": [], "events": [],
        }
        for task_id in sorted(waiting):
            event = self.pr_watch_event(task_id)
            if event is not None:
                outcome["events"].append(event)
        if due and shutil.which("gh") is None:
            for task_id, _sync_due in due:
                self._note_pr_watch_error(task_id, "gh is not installed", stamp)
            outcome["skipped"] = [{"task_id": t, "reason": "gh is not installed"} for t, _s in due]
            return outcome
        for task_id, sync_due in sorted(due):
            task = data["tasks"][task_id]
            url = str((task.get("delivery") or {}).get("url") or "").strip()
            project = data.get("projects", {}).get(task.get("project_id")) or {}
            cwd = Path(project.get("root") or ".")
            try:
                payload = self.read_pull_request(url, cwd=cwd, fields=PR_WATCH_FIELDS)
            except (HelmError, SafetyError, OSError, subprocess.SubprocessError) as exc:
                reason = str(exc)[:200]
                self._note_pr_watch_error(task_id, reason, stamp)
                outcome["skipped"].append({"task_id": task_id, "reason": reason})
                continue
            threads = self.read_review_threads(url, cwd=cwd)
            event = self._observe_pull_request(task_id, payload, threads, stamp)
            outcome["watched"].append(task_id)
            forge_state = str(payload.get("state") or "OPEN").upper()
            if sync_due or forge_state in {"MERGED", "CLOSED"}:
                try:
                    synced = self.sync_pull_request(task_id, payload=payload)
                except (HelmError, SafetyError, OSError, subprocess.SubprocessError) as exc:
                    outcome["skipped"].append({"task_id": task_id, "reason": str(exc)[:200]})
                else:
                    outcome["checked"].append(task_id)
                    if synced.get("status") == "pr-merged":
                        outcome["merged"].append(task_id)
                    elif (synced.get("delivery") or {}).get("state") == "pr-closed":
                        outcome["closed"].append(task_id)
            if event is not None:
                outcome["events"].append(event)
        return outcome

    def _observe_pull_request(
        self,
        task_id: str,
        payload: dict[str, Any],
        threads: list[dict[str, Any]] | None,
        stamp: str,
    ) -> dict[str, Any] | None:
        """Store what the watch saw; return the task's undelivered event, if any.

        Debounced on the fingerprint: an unchanged PR changes nothing, and a
        fingerprint already delivered is never queued again. Changes seen
        while an earlier event is still undelivered join that event, so the
        lead gets one message, not a backlog.

        A PR registered before the watch existed has no `since`. It is read
        once silently, as a baseline: what it said before was somebody's to
        read already, and waking a lead for every old PR at once on upgrade
        would be a burst nobody asked for.
        """
        with self.store.locked() as data:
            task = data.get("tasks", {}).get(task_id)
            if task is None:
                return None
            delivery = task.setdefault("delivery", {})
            watch = delivery.setdefault("watch", {})
            baseline_only = "since" not in watch
            snapshot, changes, kinds = pr_watch_changes(watch.get("snapshot"), payload, threads)
            fingerprint = pr_watch_fingerprint(snapshot)
            watch["last_polled_at"] = stamp
            for key in ("error", "error_kind", "error_at"):
                watch.pop(key, None)
            if fingerprint != watch.get("fingerprint"):
                watch["snapshot"] = snapshot
                watch["fingerprint"] = fingerprint
                delivered = {entry.get("fingerprint") for entry in watch.get("delivered") or []}
                if changes and not baseline_only and fingerprint not in delivered:
                    outbox = watch.get("outbox") or {"since": stamp, "changes": [], "kinds": []}
                    outbox["changes"] = (list(outbox.get("changes") or []) + changes)[-_WATCH_MAX_CHANGES:]
                    outbox["kinds"] = sorted(set(outbox.get("kinds") or []) | kinds)
                    outbox["fingerprint"] = fingerprint
                    outbox.pop("attempted_at", None)
                    watch["outbox"] = outbox
            if baseline_only:
                watch["since"] = stamp
        return self.pr_watch_event(task_id)

    def pr_watch_event(self, task_id: str) -> dict[str, Any] | None:
        """The task's queued PR event, composed as the message its lead receives."""
        data = self.store.load()
        task = data.get("tasks", {}).get(task_id)
        if task is None:
            return None
        delivery = task.get("delivery") or {}
        outbox = (delivery.get("watch") or {}).get("outbox")
        if not outbox or not outbox.get("changes"):
            return None
        url = str(delivery.get("url") or "")
        kinds = set(outbox.get("kinds") or [])
        name = task_name(task, fallback=task_id)
        lines = [f"{name}: pull request {url} changed (task {task_id}, seen by Helm's PR watch):"]
        lines += [f"- {change}" for change in outbox["changes"]]
        lines.append(PR_WATCH_INSTRUCTIONS if kinds & ACTIONABLE_KINDS else PR_WATCH_QUIET_INSTRUCTIONS)
        return {
            "task_id": task_id,
            "project_id": task.get("project_id"),
            "url": url,
            "fingerprint": outbox.get("fingerprint"),
            "kinds": sorted(kinds),
            "changes": list(outbox["changes"]),
            "text": "\n".join(lines),
        }

    def mark_pr_watch_event(
        self,
        task_id: str,
        fingerprint: str,
        *,
        lead_id: str = "",
        outcome: str = "",
        delivered: bool = True,
    ) -> None:
        """Record what became of a PR event: delivered (cleared) or attempted (retried later)."""
        with self.store.locked() as data:
            task = data.get("tasks", {}).get(task_id)
            if task is None:
                return
            watch = (task.get("delivery") or {}).get("watch")
            if not isinstance(watch, dict):
                return
            outbox = watch.get("outbox") or {}
            if not delivered:
                if outbox:
                    outbox["attempted_at"] = now()
                    outbox["last_failure"] = str(outcome)[:200]
                return
            if outbox.get("fingerprint") == fingerprint:
                watch.pop("outbox", None)
            history = list(watch.get("delivered") or [])
            history.append({"at": now(), "fingerprint": fingerprint, "lead": lead_id, "outcome": outcome})
            watch["delivered"] = history[-_WATCH_MAX_DELIVERED:]

    def _note_pr_watch_error(self, task_id: str, reason: str, stamp: str) -> None:
        """Record why the watch could not read a PR; tell the commander once.

        Recorded on the task every time, with the interval clock advanced so a
        failing read is not retried on every poll. A missing or logged-out gh
        is surfaced to the commander the first time it is seen, because it
        does not mend itself and nothing about the PR reaches anyone until it
        is fixed; a forge that is merely unreachable is an offline laptop and
        stays quiet.
        """
        kind = watch_error_kind(reason)
        with self.store.locked() as data:
            task = data.get("tasks", {}).get(task_id)
            if task is None:
                return
            delivery = task.setdefault("delivery", {})
            watch = delivery.setdefault("watch", {})
            first = watch.get("error_kind") != kind
            watch["error"] = reason
            watch["error_kind"] = kind
            watch["last_polled_at"] = stamp
            if first:
                watch["error_at"] = stamp
            project_id = str(task.get("project_id") or "")
            url = str(delivery.get("url") or "")
        if first and kind in {"missing", "unauthenticated"} and project_id:
            remedy = "gh is installed" if kind == "missing" else "gh auth login is run"
            with contextlib.suppress(HelmError, OSError):
                self.record_situation(
                    project_id,
                    f"PR watch cannot read {url} for task {task_id} ({reason[:100]}); "
                    f"its CI and review changes reach nobody until {remedy}. Helm retries on its own.",
                    surface=True,
                    task_id=task_id,
                )
