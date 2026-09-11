"""Watching a worker's process, reading what it wrote, and ending it.

A mixin over `CoordinatorBase`, split out of `status` -- which had grown to
cover seven of these at once. Moved verbatim; it imports nothing from
`helm.core`.
"""

from __future__ import annotations

import contextlib
import datetime as _dt
import json
import os
import re
import signal
import time
from pathlib import Path
from typing import Any

from ..errors import HelmError, SafetyError
from ..paths import _write_private_text
from ..processes import _scan_worker_pid
from ..values import _safe_text, new_id, now
from .. import costs


class WorkersMixin:
    def _worker_log_path(self, worker_id: str) -> Path | None:
        data = self.store.load()
        worker = data.get("workers", {}).get(worker_id)
        if worker is None:
            raise HelmError(f"unknown worker: {worker_id}")
        path = Path(worker["log_file"]) if worker.get("log_file") else None
        return path if path is not None and path.exists() else None

    def worker_output_mark(self, worker_id: str) -> int:
        """Byte length of a worker's raw output log right now.

        A mark taken before something is asked of the worker, and handed back
        as ``since``, is how a reader tells this round's output from the last
        one's -- the log carries no timestamps to do it with.
        """
        path = self._worker_log_path(worker_id)
        if path is None:
            return 0
        with contextlib.suppress(OSError):
            return path.stat().st_size
        return 0

    def worker_output(self, worker_id: str, lines: int = 40, *, since: int = 0) -> list[str]:
        """Decoded tail of a worker's terminal output.

        A worker's log is a raw PTY capture full of escape sequences, so
        reading it needs stripping every time. Doing that by hand at each
        check is repeated work and repeated tokens; it belongs here once.

        ``since`` is a byte offset from ``worker_output_mark``. Starting
        mid-escape is safe: stripping and replacement decoding both tolerate a
        truncated head, and the cost of a mangled first line is far smaller
        than reading a previous round's output as if it were this one's.
        """
        path = self._worker_log_path(worker_id)
        if path is None:
            return []
        raw = path.read_bytes()
        if since > 0:
            raw = raw[min(since, len(raw)):]
        text = raw.decode("utf-8", "replace")
        # A CSI sequence may carry intermediate bytes before its final letter
        # -- `ESC [ 0 SP q` sets the cursor shape, and agent CLIs emit it
        # constantly. Omitting the space class left "[0 q" littered through
        # every decoded line, which is ugly in `helm tail` and worse in a
        # recovered review verdict, where the litter gets recorded as if the
        # reviewer had written it.
        clean = re.sub(
            r"\x1b\[[0-9;?]*[ -/]*[@-~]|\x1b\][^\x07]*\x07|\x1b[@-Z\\-_]"
            r"|[\x00-\x08\x0b\x0c\x0e-\x1f]",
            "",
            text,
        )
        kept = [line.rstrip() for line in clean.splitlines() if line.strip()]
        return kept[-max(1, lines):]

    #: Where the coordinator's messages to a worker land. Files, because a
    #: file is the same message whatever the worker's UI is doing: an agent
    #: reads it when it next acts, and reading it is an act Helm can see.
    INBOX_DIRNAME = "inbox"
    INBOX_READ_DIRNAME = "read"

    def _inbox_dir(self, worker_id: str) -> Path:
        return self.store.directory / "workers" / worker_id / self.INBOX_DIRNAME

    def leave_inbox_note(
        self, worker_id: str, text: str, *, note_id: str | None = None
    ) -> Path:
        """Write a message where the worker will find it whenever it next acts.

        The pane used to be the message: text typed into the agent's session,
        preceded by an Escape so the paste would not be read as an interrupt.
        The Escape *was* the interrupt -- it cancelled whatever tool call the
        agent was inside -- and a keystroke's meaning depends on UI state Helm
        can only guess at. So the note is written first and always, and the
        pane is at most a pointer to it.
        """
        data = self.store.load()
        if worker_id not in data.get("workers", {}):
            raise HelmError(f"unknown worker {worker_id}")
        inbox = self._inbox_dir(worker_id)
        inbox.mkdir(parents=True, exist_ok=True)
        os.chmod(inbox, 0o700)
        note = inbox / f"{note_id or new_id('m')}.md"
        _write_private_text(note, text.rstrip("\n") + "\n")
        return note

    def inbox_notes(
        self, worker_id: str, *, unread_only: bool = True
    ) -> list[dict[str, Any]]:
        """The worker's notes, oldest first; unread ones by default."""
        inbox = self._inbox_dir(worker_id)
        sources = [(inbox, False)]
        if not unread_only:
            sources.append((inbox / self.INBOX_READ_DIRNAME, True))
        notes: list[dict[str, Any]] = []
        for directory, read in sources:
            if not directory.is_dir():
                continue
            for path in directory.glob("*.md"):
                try:
                    stamp = path.stat().st_mtime
                    text = path.read_text(encoding="utf-8")
                except OSError:
                    continue
                notes.append({
                    "id": path.stem,
                    "path": str(path),
                    "created_at": _dt.datetime.fromtimestamp(
                        stamp, _dt.timezone.utc
                    ).strftime("%Y-%m-%dT%H:%M:%SZ"),
                    "age_seconds": max(0.0, time.time() - stamp),
                    "read": read,
                    "text": text,
                })
        notes.sort(key=lambda note: (note["created_at"], note["id"]))
        return notes

    def mark_inbox_read(self, worker_id: str, note_path: str | Path) -> bool:
        """Move one note under read/; True when it was unread until now."""
        source = Path(note_path)
        read_dir = self._inbox_dir(worker_id) / self.INBOX_READ_DIRNAME
        if source.parent != read_dir.parent or not source.is_file():
            return False
        try:
            read_dir.mkdir(parents=True, exist_ok=True)
            os.chmod(read_dir, 0o700)
            os.replace(source, read_dir / source.name)
        except OSError:
            return False
        return True

    INBOX_WATCH_MARKER = ".watch"

    def inbox_watch_live(self, worker_id: str, *, fresh_seconds: float = 90.0) -> bool:
        """Whether a watch loop on this inbox has called in recently.

        Evidence, not configuration: the marker is touched by `worker inbox
        --changes` running under the worker's own identity, so it is only ever
        true of a loop that is demonstrably running. A session that never
        armed one, or whose watch died with it, reads as unwatched and gets
        the pane wake instead.
        """
        marker = self._inbox_dir(worker_id) / self.INBOX_WATCH_MARKER
        try:
            return (time.time() - marker.stat().st_mtime) <= fresh_seconds
        except OSError:
            return False

    def read_inbox(self, worker_id: str, *, watch: bool = False) -> list[dict[str, Any]]:
        """Return the unread notes and mark them read.

        Reading is the worker's own act -- this runs under its identity from
        its own session -- so "read" is a fact here, where "delivered" used to
        be inferred from the pane's output growing after an Enter. A watch
        loop says so, and that touch is what `inbox_watch_live` reads.
        """
        if watch:
            with contextlib.suppress(OSError):
                inbox = self._inbox_dir(worker_id)
                inbox.mkdir(parents=True, exist_ok=True)
                os.chmod(inbox, 0o700)
                (inbox / self.INBOX_WATCH_MARKER).touch()
        notes = self.inbox_notes(worker_id)
        for note in notes:
            if self.mark_inbox_read(worker_id, note["path"]):
                note["read"] = True
                note["path"] = str(
                    self._inbox_dir(worker_id) / self.INBOX_READ_DIRNAME
                    / Path(note["path"]).name
                )
        return notes

    def wait_inbox(self, worker_id: str, timeout: float) -> list[dict[str, Any]]:
        """Block until a note arrives or the timeout passes; marks what it returns read."""
        deadline = time.monotonic() + max(0.0, float(timeout))
        while True:
            notes = self.read_inbox(worker_id)
            if notes or time.monotonic() >= deadline:
                return notes
            time.sleep(min(0.5, max(0.05, deadline - time.monotonic())))

    def oldest_unread_inbox_age(self, worker_id: str) -> float | None:
        """Seconds the oldest unread note has waited, or None when nothing waits."""
        notes = self.inbox_notes(worker_id)
        return max(note["age_seconds"] for note in notes) if notes else None

    def note_agent_session(self, worker_id: str, session_id: str) -> bool:
        """Record the runtime's own session id for a worker, once.

        Herdr learns it from the runtime's hook; it is what names the
        transcript `task_usage` reads. Recorded the first time it is seen,
        because a pane is released the moment a clean result lands and the
        id goes with it.
        """
        if not session_id:
            return False
        with self.store.locked() as data:
            worker = data.get("workers", {}).get(worker_id)
            if worker is None or worker.get("agent_session_id"):
                return False
            worker["agent_session_id"] = session_id
        return True

    def task_usage(self, task_id: str, *, with_reviews: bool = True) -> dict[str, Any]:
        """What a task's sessions consumed, from the runtimes' own transcripts.

        Every worker that ran the task, and by default every reviewer task
        that judged it, because a review is part of what the change cost.
        The foreman is not included: it spans tasks, and attributing its
        turns to one of them would be a guess.
        """
        data = self.store.load()
        if task_id not in data.get("tasks", {}):
            raise HelmError(f"unknown task {task_id}")
        task_ids = [task_id]
        if with_reviews:
            task_ids.extend(
                candidate_id
                for candidate_id, candidate in data.get("tasks", {}).items()
                if candidate.get("role") == "reviewer" and candidate.get("reviews") == task_id
            )
        workers = [
            worker for worker in data.get("workers", {}).values()
            if worker.get("task_id") in task_ids
        ]
        workers.sort(key=lambda worker: str(worker.get("started_at") or ""))
        entries = [costs.worker_usage(worker) for worker in workers]
        for entry, worker in zip(entries, workers):
            entry["task_id"] = worker.get("task_id")
            entry["role"] = (data["tasks"].get(worker.get("task_id")) or {}).get("role")
        return {"task_id": task_id, "workers": entries, "total": costs.sum_usage(entries)}

    def nudge_worker(self, worker_id: str, text: str = "") -> dict[str, Any]:
        """Ask a silent worker for a status push and record that we asked.

        One nudge, recorded: a second round of silence is a fault to report to
        a human, not something to keep poking at.
        """
        message = text or (
            "Helm sees no progress from you. Push a status message now with the "
            "reporting command in your context document, and a question or blocker "
            "if something is stopping you."
        )
        with self.store.locked() as data:
            worker = data["workers"].get(worker_id)
            if worker is None:
                raise HelmError(f"unknown worker: {worker_id}")
            worker["nudged_at"] = now()
        return {"worker_id": worker_id, "text": message}

    def reconcile_worker(self, worker_id: str, evidence: str) -> dict[str, Any]:
        """Set a live-but-recorded-dead worker's record back to running.

        The record and the session can disagree: a liveness probe that cannot
        see inside a pane marks the worker failed while the agent in it keeps
        working, proposes gates, and posts blockers. Every downstream guard
        then believes the record -- `task continue` refuses for want of a
        live foreman, gate decisions report undeliverable, and a coordinator
        acting on the record stops a healthy driver. One mislabelled row
        cascaded into a day of replacements before this command existed.

        Root-only, and it demands written evidence of life -- a message the
        worker posted after the row said failed, a pid, a pane -- because the
        opposite mistake (reviving a genuinely dead worker) recreates the very
        divergence being repaired. The evidence lands in the task's record, so
        a reconciliation is auditable the way a stop is.
        """
        self.authority("reconciling a worker record to running")
        evidence = _safe_text(evidence).strip()
        if not evidence:
            raise HelmError(
                "reconcile requires --evidence: what shows this session is "
                "alive despite the record (a message it posted after the row "
                "said failed, a live pid, a pane)"
            )
        with self.store.locked() as data:
            worker = (data.get("workers") or {}).get(worker_id)
            if worker is None:
                raise HelmError(f"unknown worker: {worker_id}")
            if worker.get("status") == "running":
                raise HelmError(f"worker {worker_id} is already recorded running")
            task = self._task(data, worker["task_id"])
            was_worker, was_task = worker.get("status"), task.get("status")
            worker["status"] = "running"
            for stale in ("exit_code", "stopped_at", "stop_reason"):
                worker.pop(stale, None)
            if task.get("status") in {"failed", "blocked"}:
                task["status"] = "running"
            project = self._project(data, task["project_id"])
            self._message(
                data, project, task, worker, "status",
                f"Commander reconciled worker {worker_id} from {was_worker} to "
                f"running (task {was_task} -> {task['status']}). Evidence: {evidence}",
                {"reconciled_from": was_worker},
            )
        return {"worker": worker_id, "was": was_worker, "task": task["id"]}

    def stop_worker(
        self, worker_id: str, reason: str = "", *, grace: float | None = None
    ) -> dict[str, Any]:
        """Stop a running worker and settle its task.

        Abandoning a task could not be expressed. Helm rightly refuses to tear
        down a project's space while a worker runs, and then offered no way to
        make one stop: the only exits were a worker finishing on its own, or a
        human killing a pane by hand -- which leaves the record saying
        `running` forever, with no command able to correct it. Anything keyed
        on a live worker is then wrong permanently, and a project whose
        foreman was killed that way could never be given another one.

        The record is settled whether or not the process could be signalled,
        because a stop nobody can record is the failure this fixes. The log
        and the worktree are left alone: they are the evidence for why the
        task was abandoned, and `helm task cleanup` removes them deliberately.
        """
        data = self.store.load()
        worker = data.get("workers", {}).get(worker_id)
        if worker is None:
            raise HelmError(f"unknown worker: {worker_id}")
        # Idempotent on purpose: stopping something already stopped is what a
        # person does when they are unsure whether the first one took.
        #
        # It still reconciles the exit record first. A worker that settled on
        # its own -- an agent that pushed a result and exited, or a pane a
        # human closed -- leaves `status` terminal and no exit record, and
        # `_session_still_live` reads an `external` worker with no exit record
        # as live forever. That made `helm task cleanup` permanently
        # impossible for exactly the workers most likely to need it, and
        # returning early here was the reason the documented repair ("end it
        # with helm worker stop") did nothing. Only reconcile when the process
        # is demonstrably gone, so this never papers over a live session.
        if worker.get("status") != "running":
            signalled = False
            if self._pid_alive(worker.get("pid")):
                # A settled worker whose session is still open: it pushed its
                # result and kept its pane. A stop is the request to end that
                # session, so end it. This used to leave a live process alone
                # and write nothing; the adapter closed the pane a moment
                # later, which took the process with it unrecorded, and the
                # cleanup that followed refused the task with the very command
                # that had just been run.
                signalled = self._terminate_process(worker.get("pid"), grace=grace)
            if not self._pid_alive(worker.get("pid")):
                self._record_worker_exit(worker, stopped=True, signalled=signalled)
            worker["signalled"] = signalled
            return worker
        detail = _safe_text(reason).strip() or "stopped by the coordinator"
        # A provider-launched worker never had its pid recorded, so this used
        # to signal nothing and "stopped" meant only that the record changed --
        # the agent kept running, invisible, and had to be killed by hand. Look
        # for it now, so a stop that says it stopped something did.
        if not worker.get("pid"):
            with contextlib.suppress(HelmError, SafetyError, OSError):
                self.adopt_worker_pid(worker_id)
            worker = self.store.load().get("workers", {}).get(worker_id, worker)
        signalled = self._terminate_process(worker.get("pid"), grace=grace)
        # Record the exit here, which is what `_session_still_live` reads
        # before `helm task cleanup` will touch a worktree. Its docstring
        # already said stopping "records the exit this looks for" -- it did
        # not, and the omission deadlocked cleanup: a Herdr-launched worker is
        # `external`, so with no exit record it counts as live forever and its
        # worktree could never be removed. Worktrees accumulated with no
        # supported way to shed them.
        #
        # A stop is the deliberate statement that this session is over, which
        # is exactly the fact the gate needs, so it is the honest place to
        # write it. The record says how it ended, so an exit Helm asserted is
        # never mistaken for one the runner observed.
        self._record_worker_exit(worker, stopped=True, signalled=signalled)
        stopped = self.mark_worker_lost(worker_id, detail, kind="stopped")
        stopped["signalled"] = signalled
        return stopped

    @staticmethod
    def _record_worker_exit(
        worker: dict[str, Any], *, stopped: bool, signalled: bool
    ) -> None:
        """Write the exit record `_session_still_live` reads before cleanup.

        Never overwrites one the runner wrote: a real exit carries the
        process's own returncode, and an exit Helm asserted must not be
        mistaken for one it observed. `stopped` marks the difference.
        """
        exit_file = worker.get("exit_file")
        if not exit_file:
            return
        path = Path(exit_file)
        if path.exists():
            return
        with contextlib.suppress(OSError):
            _write_private_text(
                path,
                json.dumps(
                    {"returncode": None, "stopped": stopped, "signalled": signalled}
                )
                + "\n",
            )

    def _terminate_process(self, pid: Any, *, grace: float | None = None) -> bool:
        """Ask a process to exit, then insist. False when there was none.

        A worker hosted in a Herdr pane has no pid Helm owns; closing its tab
        is the adapter's job, and this reporting False is how the caller knows
        the record was settled without a process being touched.
        """
        if not isinstance(pid, int) or pid <= 0:
            return False
        try:
            os.kill(pid, signal.SIGTERM)
        except (OSError, ProcessLookupError):
            return False
        deadline = time.monotonic() + (
            self.STOP_GRACE_SECONDS if grace is None else max(0.0, grace)
        )
        while time.monotonic() < deadline:
            if self._reaped(pid):
                return True
            time.sleep(0.1)
        with contextlib.suppress(OSError, ProcessLookupError):
            os.kill(pid, signal.SIGKILL)
        # Give the kill a moment to land so the record is not written while
        # the process is still visibly alive.
        for _ in range(20):
            if self._reaped(pid):
                break
            time.sleep(0.05)
        return True

    @staticmethod
    def _process_alive(pid: Any) -> bool:
        """Whether a pid Helm recorded still names a live process.

        Unknown means alive: a worker Helm cannot check is not evidence that
        it died, and calling a working agent dead is the more expensive
        mistake of the two.
        """
        if not isinstance(pid, int) or pid <= 0:
            return True
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        except OSError:
            return True
        return True

    @staticmethod
    def _reaped(pid: int) -> bool:
        """Whether the process is gone, reaping it first if it is ours.

        A terminated child that nobody has waited on is a zombie, and a
        zombie still answers signal 0 -- so a liveness check alone would wait
        out the whole grace period and then SIGKILL something already dead.
        """
        with contextlib.suppress(ChildProcessError, OSError):
            reaped, _ = os.waitpid(pid, os.WNOHANG)
            if reaped == pid:
                return True
        try:
            os.kill(pid, 0)
        except OSError:
            return True
        return False

    def adopt_worker_pid(self, worker_id: str) -> int | None:
        """Record the pid of a worker Helm launched through a provider.

        Helm forks a process worker and keeps its pid; a provider-launched one
        runs inside a pane Helm never owns, so its record kept `pid: None` and
        `_pid_alive` answered "dead" for it forever. That left the provider's
        view of the *pane* as the only liveness signal, and a pane is not a
        process -- see `_scan_worker_pid` for what that cost.

        Best-effort and idempotent: a pid already on the record is kept, and a
        failed scan leaves the record untouched rather than writing a guess.
        The pid is an extra witness, never a replacement for the provider's.
        """
        with self.store.locked() as data:
            worker = data.get("workers", {}).get(worker_id)
            if worker is None or worker.get("pid"):
                return worker.get("pid") if worker else None
            pid = _scan_worker_pid(worker_id, self.store.directory)
            if pid is None:
                return None
            worker["pid"] = pid
            self.store.save(data)
            return pid

    def mark_worker_orphaned(self, worker_id: str) -> dict[str, Any]:
        """Record that a worker's surface is gone while its process is not.

        Deliberately not a terminal state. The agent is still running and may
        still finish and report, so failing the task here would discard work
        that is happening -- and `lost` also abandons the task's open hold,
        which is exactly wrong for a session that can still spend it.

        What it does is make the split visible. Before this, a closed pane was
        recorded as `lost` and the surviving agent became unreachable: `worker
        answer` refused it as gone and `worker stop` had nothing to stop, so it
        could only be killed by hand. Naming the state is what lets those two
        commands act on it, and what tells the commander the difference between
        "this ended" and "this is running where nobody can see it".
        """
        with self.store.locked() as data:
            worker = data["workers"].get(worker_id)
            if worker is None:
                raise HelmError(f"unknown worker: {worker_id}")
            if worker.get("status") != "running" or worker.get("orphaned_at"):
                return worker
            worker["orphaned_at"] = now()
            task = self._task(data, worker["task_id"])
            project = self._project(data, worker["project_id"])
            self._message(
                data,
                project,
                task,
                worker,
                "status",
                (
                    f"Worker {worker_id} lost its pane but its process (pid "
                    f"{worker.get('pid')}) is still running. The task is NOT failed "
                    "and the agent may still report. It has no visible surface: read "
                    f"{worker.get('log_file')} to see what it is doing, or "
                    f"`helm worker stop {worker_id}` to end it."
                ),
                {"status": "running", "orphaned": True},
            )
            self.store.save(data)
            return worker

    def mark_worker_lost(
        self, worker_id: str, detail: str, *, kind: str = "lost"
    ) -> dict[str, Any]:
        """Durably fail an external assignment whose provider disappeared."""
        with self.store.locked() as data:
            worker = data["workers"].get(worker_id)
            if worker is None:
                raise HelmError(f"unknown worker: {worker_id}")
            if worker.get("status") != "running":
                return worker
            task = self._task(data, worker["task_id"])
            project = self._project(data, worker["project_id"])
            with contextlib.suppress(OSError, SafetyError):
                _write_private_text(
                    Path(worker["exit_file"]),
                    json.dumps({"returncode": 1, "error": _safe_text(detail)}) + "\n",
                )
            worker["status"] = "failed"
            worker["exit_code"] = 1
            worker["ended_at"] = now()
            task["status"] = "failed"
            self._abandon_open_hold(
                data, project, task, f"its session is gone: {detail}"
            )
            # A task abandoned on purpose and a task whose provider vanished
            # are both failures, and the record should not pretend otherwise
            # -- but it should say which one happened, because only one of
            # them is somebody's decision.
            headline = (
                f"Worker stopped: {detail}"
                if kind == "stopped"
                else f"External worker lost: {detail}"
            )
            self._message(data, project, task, worker, "failure", headline, {"stop_kind": kind})
            return worker
