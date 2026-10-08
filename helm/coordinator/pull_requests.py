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

#: How long one pass holds a queued event while it delivers it. Another pass
#: that finds the claim younger than this leaves the event alone, so two
#: loops running side by side deliver it once.
PR_WATCH_CLAIM_SECONDS = 300.0

#: How long every PR read stops after the forge says Helm is asking too often.
PR_WATCH_RATE_LIMIT_BACKOFF_SECONDS = 900.0

#: The root's budget for leads the PR watch appoints: this many attempts in
#: any rolling window, across every pass and every loop that runs one.
PR_WATCH_APPOINTMENTS_PER_WINDOW = 2
PR_WATCH_APPOINTMENT_WINDOW_SECONDS = 600.0

#: A web address, before anything is handed to gh.
_PR_URL = re.compile(r"https?://[^\s/]+/\S+$")


def _epoch(stamp: Any) -> float:
    if not isinstance(stamp, str) or not stamp:
        return 0.0
    try:
        return _dt.datetime.fromisoformat(stamp.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return 0.0


def _stamp(epoch: float) -> str:
    return _dt.datetime.fromtimestamp(epoch, _dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class PullRequestsMixin:
    def read_pull_request(self, url: str, *, cwd: Path, fields: str = "") -> dict[str, Any]:
        """What the forge says about a PR, through gh. Raises when it cannot say."""
        url = str(url or "").strip()
        if not _PR_URL.match(url):
            # A recorded URL is data. One that is not a web address must never
            # reach gh, where a leading dash would be read as an option.
            raise HelmError(f"not a pull request URL gh can read: {url[:80]!r}")
        if shutil.which("gh") is None:
            raise HelmError("gh is not installed; record PR observations with helm task pr-status")
        result = subprocess.run(
            [
                "gh", "pr", "view", "--json",
                fields or "url,state,reviewDecision,mergeStateStatus,mergeCommit,headRefOid,comments,body",
                "--", url,
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
        and a decision worth reporting. A rate limit is the one failure
        that is not left at that: it starts the same backoff as a rate-limited
        `pr view`, and the pass stops at the next PR.
        """
        import time

        match = re.match(r"https?://([^/]+)/([^/]+)/([^/]+)/pull/(\d+)", url.strip())
        if match is None or shutil.which("gh") is None:
            return None
        host, owner, name, number = match.groups()
        command = ["gh", "api", "graphql"]
        if host.lower() != "github.com":
            command += ["--hostname", host]
        # `-f` is a literal string. `-F` would read `@path` as a file and
        # coerce the value's type, so it is kept for the one field that is a
        # number and the regex has already proved to be digits.
        command += [
            "-f", f"query={REVIEW_THREADS_QUERY}",
            "-f", f"owner={owner}", "-f", f"name={name}", "-F", f"number={number}",
        ]
        try:
            result = subprocess.run(
                command, cwd=str(cwd), text=True, stdout=subprocess.PIPE,
                stderr=subprocess.PIPE, check=False, timeout=60,
            )
            # The answer carries reviewers' own words, so it is never searched
            # as text: a thread saying "rate limit" is a comment, not a limit.
            # A limit is gh's error output on a failed call, or a structured
            # `errors[].type == "RATE_LIMITED"` in the parsed answer.
            try:
                payload = json.loads(result.stdout or "{}")
            except ValueError:
                payload = None
            errors = payload.get("errors") if isinstance(payload, dict) else None
            structured = any(
                isinstance(error, dict) and error.get("type") == "RATE_LIMITED"
                for error in (errors if isinstance(errors, list) else [])
            )
            failed = result.returncode != 0
            if structured or (failed and watch_error_kind(result.stderr or "") == "rate-limited"):
                self._back_off_pr_reads("GraphQL review threads: rate limited", time.time())
                self._pr_threads_rate_limited = True
                return None
            if failed or payload is None:
                return None
            return parse_review_threads(payload)
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
        about, queues the change on the task for the caller to deliver to the
        task's lead (`events` in the result). Under turns a lead only exists
        inside a turn, so nothing else would wake it when CI goes red or a
        reviewer writes. The SYNC records the merge state through
        `record_pr_status` every `min_interval_seconds`, or at once when the
        watch sees the PR merged or closed; a merge is recorded exactly as
        `helm task pr-sync` records it, which raises the cleanup decision and
        lets the project's space close.

        Several loops run this side by side -- the watchdog, `pending
        --changes`, `helm watch` -- so a PR is CLAIMED under the store lock
        before it is read: the pass that claims it is the only one that reads
        it this interval. Delivery claims the queued event the same way.

        Quiet by design: a PR that cannot be read now -- no network, a URL
        the forge rejects -- is listed as skipped and tried again next
        interval. gh missing or logged out is recorded on the task and
        surfaced to the commander once, because that one does not mend
        itself. A rate limit stops the whole pass and every read for a while,
        because asking again is what makes it worse.
        """
        import time

        current = time.time() if now_epoch is None else now_epoch
        stamp = _stamp(current)
        due: list[tuple[str, bool, str, str]] = []
        waiting: list[str] = []
        outcome: dict[str, Any] = {
            "checked": [], "merged": [], "closed": [], "skipped": [], "watched": [], "events": [],
            "rate_limited": False,
        }
        with self.store.locked() as data:
            limit = (data.get("integrations") or {}).get("pr_watch") or {}
            backing_off = current < _epoch(limit.get("backoff_until"))
            outcome["rate_limited"] = backing_off
            for task_id, task in sorted(data.get("tasks", {}).items()):
                if task.get("status") != "pr-open":
                    continue
                delivery = task.get("delivery") or {}
                url = str(delivery.get("url") or "").strip()
                if not url:
                    continue
                watch = delivery.get("watch") or {}
                sync_due = current - _epoch(delivery.get("last_checked_at")) >= min_interval_seconds
                last_look = max(_epoch(watch.get("last_polled_at")), _epoch(watch.get("since")))
                if not backing_off and (sync_due or current - last_look >= watch_interval_seconds):
                    # The claim: whichever pass writes this first reads the PR.
                    claimed = delivery.setdefault("watch", {})
                    claimed["last_polled_at"] = stamp
                    if sync_due:
                        delivery["last_checked_at"] = stamp
                    project = data.get("projects", {}).get(task.get("project_id")) or {}
                    due.append((task_id, sync_due, url, str(project.get("root") or ".")))
                    continue
                outbox = watch.get("outbox") or {}
                if (
                    outbox.get("items")
                    and current - _epoch(outbox.get("claimed_at")) >= PR_WATCH_CLAIM_SECONDS
                    and current - _epoch(outbox.get("attempted_at")) >= watch_interval_seconds
                ):
                    waiting.append(task_id)
        for task_id in waiting:
            event = self.pr_watch_event(task_id)
            if event is not None:
                outcome["events"].append(event)
        if due and shutil.which("gh") is None:
            for task_id, _sync_due, _url, _root in due:
                self._note_pr_watch_error(task_id, "gh is not installed", stamp)
            outcome["skipped"] = [{"task_id": t, "reason": "gh is not installed"} for t, *_ in due]
            return outcome
        for index, (task_id, sync_due, url, root) in enumerate(due):
            cwd = Path(root)
            try:
                payload = self.read_pull_request(url, cwd=cwd, fields=PR_WATCH_FIELDS)
            except (HelmError, SafetyError, OSError, subprocess.SubprocessError) as exc:
                reason = str(exc)[:200]
                self._note_pr_watch_error(task_id, reason, stamp)
                outcome["skipped"].append({"task_id": task_id, "reason": reason})
                if watch_error_kind(reason) == "rate-limited":
                    self._back_off_pr_reads(reason, current)
                    outcome["rate_limited"] = True
                    outcome["skipped"].extend(
                        {"task_id": rest, "reason": "not read: the forge is rate-limiting gh"}
                        for rest, *_ in due[index + 1:]
                    )
                    break
                continue
            self._pr_threads_rate_limited = False
            threads = self.read_review_threads(url, cwd=cwd)
            event = self._observe_pull_request(task_id, payload, threads, stamp)
            outcome["watched"].append(task_id)
            stop_after = bool(getattr(self, "_pr_threads_rate_limited", False))
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
                        # Composed again: the task's status moved, and whether
                        # a lead may be appointed for it depends on that.
                        event = self.pr_watch_event(task_id)
            if event is not None:
                outcome["events"].append(event)
            if stop_after:
                # The threads read was rate-limited: this PR's checks are
                # recorded, and nothing more is asked of the forge.
                outcome["rate_limited"] = True
                outcome["skipped"].extend(
                    {"task_id": rest, "reason": "not read: the forge is rate-limiting gh"}
                    for rest, *_ in due[index + 1:]
                )
                break
        return outcome

    def _back_off_pr_reads(self, reason: str, current: float) -> None:
        """Stop every PR read until the forge has had time to forgive us."""
        with self.store.locked() as data:
            limit = data.setdefault("integrations", {}).setdefault("pr_watch", {})
            limit["backoff_until"] = _stamp(current + PR_WATCH_RATE_LIMIT_BACKOFF_SECONDS)
            limit["reason"] = reason[:200]

    def reserve_pr_watch_appointment(self, now_epoch: float | None = None) -> bool:
        """Spend one of the root's PR-watch lead appointments, or say there is none left.

        A budget per root, not per pass: `pending --changes` runs a pass every
        few seconds, so a per-pass cap still let a burst of leaderless red PRs
        start a lead every few seconds. At most
        `PR_WATCH_APPOINTMENTS_PER_WINDOW` attempts in any rolling
        `PR_WATCH_APPOINTMENT_WINDOW_SECONDS`, counted when the attempt is
        made -- an appointment that then fails still spent its place, so a
        launch that keeps failing is not retried for every queued event.
        """
        import time

        current = time.time() if now_epoch is None else now_epoch
        with self.store.locked() as data:
            limit = data.setdefault("integrations", {}).setdefault("pr_watch", {})
            recent = [
                stamp for stamp in limit.get("appointments") or []
                if current - _epoch(stamp) < PR_WATCH_APPOINTMENT_WINDOW_SECONDS
            ]
            granted = len(recent) < PR_WATCH_APPOINTMENTS_PER_WINDOW
            if granted:
                recent.append(_stamp(current))
            limit["appointments"] = recent[-PR_WATCH_APPOINTMENTS_PER_WINDOW:]
            return granted

    def _observe_pull_request(
        self,
        task_id: str,
        payload: dict[str, Any],
        threads: list[dict[str, Any]] | None,
        stamp: str,
    ) -> dict[str, Any] | None:
        """Store what the watch saw; return the task's queued event, if any.

        Debounced on the fingerprint: an unchanged PR changes nothing, and a
        fingerprint already delivered is never queued again. Changes seen
        while an earlier event is still queued join it, each with its own id,
        so delivering the earlier part removes exactly what was delivered.

        The first read is news only for a PR registered while the watch
        existed (`registered`, set by `_start_pr_watch`). Any other watch with
        no snapshot yet -- a PR recorded before this, or a watch that began
        life as an error note -- is read once silently, as a baseline: what it
        said before was somebody's to read already, and waking a lead for
        every old PR at once on upgrade is a burst nobody asked for.
        """
        with self.store.locked() as data:
            task = data.get("tasks", {}).get(task_id)
            if task is None:
                return None
            delivery = task.setdefault("delivery", {})
            watch = delivery.setdefault("watch", {})
            baseline_only = watch.get("snapshot") is None and not watch.get("registered")
            snapshot, changes, _kinds = pr_watch_changes(watch.get("snapshot"), payload, threads)
            fingerprint = pr_watch_fingerprint(snapshot)
            watch["last_polled_at"] = stamp
            watch.setdefault("since", stamp)
            for key in ("error", "error_kind", "error_at"):
                watch.pop(key, None)
            if fingerprint != watch.get("fingerprint"):
                watch["snapshot"] = snapshot
                watch["fingerprint"] = fingerprint
                delivered = {entry.get("fingerprint") for entry in watch.get("delivered") or []}
                if changes and not baseline_only and fingerprint not in delivered:
                    outbox = watch.get("outbox") or {"since": stamp, "next": 1, "items": []}
                    items = list(outbox.get("items") or [])
                    for change in changes:
                        items.append({"id": int(outbox.get("next") or 1), **change})
                        outbox["next"] = int(outbox.get("next") or 1) + 1
                    outbox["items"] = items[-_WATCH_MAX_CHANGES:]
                    outbox["fingerprint"] = fingerprint
                    outbox.pop("attempted_at", None)
                    watch["outbox"] = outbox
        return self.pr_watch_event(task_id)

    @staticmethod
    def _compose_pr_event(task: dict[str, Any]) -> dict[str, Any] | None:
        delivery = task.get("delivery") or {}
        outbox = (delivery.get("watch") or {}).get("outbox") or {}
        items = [item for item in outbox.get("items") or [] if isinstance(item, dict)]
        if not items:
            return None
        url = str(delivery.get("url") or "")
        kinds = {str(item.get("kind") or "") for item in items}
        actionable = bool(kinds & ACTIONABLE_KINDS)
        name = task_name(task, fallback=task["id"])
        lines = [f"{name}: pull request {url} changed (task {task['id']}, seen by Helm's PR watch):"]
        lines += [f"- {item.get('text')}" for item in items]
        lines.append(PR_WATCH_INSTRUCTIONS if actionable else PR_WATCH_QUIET_INSTRUCTIONS)
        return {
            "task_id": task["id"],
            "project_id": task.get("project_id"),
            "task_status": task.get("status"),
            "url": url,
            "fingerprint": outbox.get("fingerprint"),
            "kinds": sorted(kinds),
            "actionable": actionable,
            "changes": [str(item.get("text")) for item in items],
            "change_ids": [item.get("id") for item in items],
            "text": "\n".join(lines),
        }

    def pr_watch_event(self, task_id: str) -> dict[str, Any] | None:
        """The task's queued PR event, composed as the message its lead receives."""
        task = self.store.load().get("tasks", {}).get(task_id)
        return None if task is None else self._compose_pr_event(task)

    def claim_pr_watch_event(self, task_id: str) -> dict[str, Any] | None:
        """Take the task's queued event for delivery, or None if another pass holds it.

        Read and claimed under one lock, so of two passes delivering at once
        exactly one gets the event. The claim lapses after
        `PR_WATCH_CLAIM_SECONDS`, so a pass that died mid-delivery does not
        hold it forever.
        """
        import time

        with self.store.locked() as data:
            task = data.get("tasks", {}).get(task_id)
            if task is None:
                return None
            event = self._compose_pr_event(task)
            if event is None:
                return None
            outbox = task["delivery"]["watch"]["outbox"]
            if time.time() - _epoch(outbox.get("claimed_at")) < PR_WATCH_CLAIM_SECONDS:
                return None
            outbox["claimed_at"] = now()
            return event

    def mark_pr_watch_event(
        self,
        task_id: str,
        change_ids: list[Any],
        *,
        result: str = "delivered",
        lead_id: str = "",
        outcome: str = "",
    ) -> bool:
        """Settle a claimed event: `delivered`, `failed` (retry later) or `deferred`.

        Delivered removes exactly the changes that went out; anything that
        arrived while they were being delivered stays queued for next time.
        For `deferred`, returns whether this is the first deferral in the
        current budget window -- the only one worth a line.
        """
        import time

        with self.store.locked() as data:
            task = data.get("tasks", {}).get(task_id)
            if task is None:
                return False
            watch = (task.get("delivery") or {}).get("watch")
            if not isinstance(watch, dict):
                return False
            outbox = watch.get("outbox") or {}
            outbox.pop("claimed_at", None)
            if result == "deferred":
                # Said once per budget window, not on every pass that finds
                # the budget still spent.
                said = _epoch(outbox.get("deferred_at"))
                if time.time() - said < PR_WATCH_APPOINTMENT_WINDOW_SECONDS:
                    return False
                outbox["deferred_at"] = now()
                return True
            if result == "failed":
                if outbox:
                    outbox["attempted_at"] = now()
                    outbox["last_failure"] = str(outcome)[:200]
                return False
            sent = set(change_ids)
            remaining = [item for item in outbox.get("items") or [] if item.get("id") not in sent]
            history = list(watch.get("delivered") or [])
            history.append({
                "at": now(), "fingerprint": outbox.get("fingerprint"),
                "changes": len(sent), "lead": lead_id, "outcome": outcome,
            })
            watch["delivered"] = history[-_WATCH_MAX_DELIVERED:]
            if remaining:
                outbox["items"] = remaining
            else:
                watch.pop("outbox", None)
            return True

    def _note_pr_watch_error(self, task_id: str, reason: str, stamp: str) -> None:
        """Record why the watch could not read a PR; tell the commander once.

        Recorded on the task every time. A missing or logged-out gh is
        surfaced to the commander the first time it is seen, because it does
        not mend itself and nothing about the PR reaches anyone until it is
        fixed; a forge that is merely unreachable is an offline laptop and
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
