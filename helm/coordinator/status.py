"""The project-status half of the Coordinator: records, gates, foremen, workers.

A mixin over `CoordinatorBase`; it resolves everything through `self` at
runtime, so it imports nothing from `helm.core`.
"""

from __future__ import annotations

import contextlib
import copy
import datetime as _dt
import fcntl
import hashlib
import json
import os
import re
import signal
import sys
import time
from pathlib import Path
from typing import Any, Iterator, Sequence

from ..authority import AUTHORITY_ENV, Authority
from ..errors import HelmError, SafetyError
from ..paths import _private_dir, _write_private_text, canonical, overlaps
from ..processes import _process_parents, _scan_worker_pid
from ..values import (
    DELIVERED_TASK_STATES,
    DELIVERY_DECISION_KIND,
    DELIVERY_DECISION_PROJECT_TEXT,
    DELIVERY_DECISION_TASK_TEXT,
    FAILURE_ACTION_KIND,
    FINALIZATION_ACTION_KIND,
    FOLLOW_UP_ACTION_KIND,
    FOREMAN_DOMAIN,
    FOREMAN_RULES,
    GATE_ACTION_KINDS,
    GATE_TYPES,
    HEALTHY_WORKER_VERDICTS,
    REQUIREMENT_GATE_KIND,
    SOLUTION_GATE_KIND,
    WORKTREELESS_ROLES,
    _TERMINAL_WORKER_TASK_STATES,
    _safe_text,
    _validate_project_id,
    finalization_text,
    new_id,
    now,
    project_glyph,
    task_owns_branch,
)


class StatusMixin:
    # ---------- project status ----------

    def _status_path(self, project_id: str) -> Path:
        directory = self.store.directory / "projects" / _validate_project_id(project_id)
        _private_dir(directory.parent)
        directory.mkdir(parents=True, exist_ok=True)
        os.chmod(directory, 0o700)
        return directory / "status.json"

    def _load_status(self, project_id: str) -> dict[str, Any]:
        path = self._status_path(project_id)
        if not path.exists():
            return {"situation": [], "action_items": [], "history": [], "evidence": {}}
        try:
            loaded = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            return {"situation": [], "action_items": [], "history": [], "evidence": {}}
        loaded.setdefault("situation", [])
        loaded.setdefault("action_items", [])
        loaded.setdefault("history", [])
        loaded.setdefault("evidence", {})
        return loaded

    def _save_status(self, project_id: str, payload: dict[str, Any]) -> None:
        _write_private_text(
            self._status_path(project_id), json.dumps(payload, indent=2) + "\n"
        )

    @contextlib.contextmanager
    def _status_transaction(self, project_id: str) -> Iterator[dict[str, Any]]:
        """Read, change and write a project's record without losing a concurrent write.

        The state file has a lock; this file did not, and every writer here was
        a read-modify-write. A release and a result landing together could each
        load the same record and save over the other's change -- the authorized
        decision or its outcome vanishing with nothing to say it had. Its own
        lock, because it is its own file: taking the state lock instead would
        invert the order these are already acquired in elsewhere.
        """
        path = self._status_path(project_id)
        lock_path = path.with_suffix(".lock")
        with lock_path.open("a+", encoding="utf-8") as lock:
            os.chmod(lock_path, 0o600)
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
            try:
                payload = self._load_status(project_id)
                yield payload
                self._save_status(project_id, payload)
            finally:
                fcntl.flock(lock.fileno(), fcntl.LOCK_UN)

    def record_situation(
        self,
        project_id: str,
        line: str,
        *,
        supersedes: str = "",
        surface: bool = False,
        task_id: str = "",
    ) -> dict[str, Any]:
        """Append one line of context Helm cannot derive.

        ``surface`` marks the line as one the commander is owed rather than one
        they may go looking for. It changes nothing about how the line is
        stored; it changes what `pending_updates` is allowed to do with it.

        The only growing part of the record, so it is the part with a limit:
        entries beyond the most recent few roll into history that the status
        view does not load, and an entry may mark an earlier one superseded
        rather than sitting beside it and contradicting it.

        An over-long note is refused rather than trimmed. It used to be
        silently cut at the limit, which destroyed exactly the wrong thing:
        the instruction for what to do next goes at the end of a note, so
        every long entry lost its point and still looked complete. A foreman
        read one of those, found no goal in it, and started the wrong work --
        the record failing at the one job it exists for.
        """
        text = _safe_text(line).strip()
        if not text:
            raise HelmError("a situation line is required")
        if len(text) > self.SITUATION_LINE_LIMIT:
            raise HelmError(
                f"a situation note is one line of context, not a document: "
                f"{len(text)} characters given, limit {self.SITUATION_LINE_LIMIT}. "
                "Split it into separate notes -- put the decision in one and what "
                "to do next in another -- or write the detail into the project's "
                "own files and reference it from here."
            )
        with self._status_transaction(project_id) as status:
            for entry in status["situation"]:
                if supersedes and entry.get("id") == supersedes:
                    entry["superseded_by"] = now()
            entry = {"id": new_id("s"), "at": now(), "text": text}
            if task_id:
                # Which task raised the line. Without it a report can only be
                # matched back by parsing its own prose, so nothing downstream
                # can tell a live escalation from one already answered.
                entry["task_id"] = task_id
            if surface:
                entry["surface"] = True
            status["situation"].append(entry)
            live = [e for e in status["situation"] if not e.get("superseded_by")]
            if len(live) > self.SITUATION_KEPT:
                excess = len(live) - self.SITUATION_KEPT
                rolled = live[:excess]
                status["history"].extend(rolled)
                rolled_ids = {e["id"] for e in rolled}
                status["situation"] = [
                    e for e in status["situation"] if e["id"] not in rolled_ids
                ]
            recorded = status["situation"][-1]
        return recorded


    def compose_outcome_handoff(
        self, worker_id: str, data: dict[str, Any] | None = None
    ) -> dict[str, Any] | None:
        """The coordinator-facing notice for a worker's terminal report.

        Writing the outcome into the project's record makes it survivable;
        this is what makes it *arrive*. The two are not the same guarantee, and
        only the second one is any use to a coordinator that is not, at that
        moment, reading a status file: a result printed into the worker's own
        pane vanishes with the pane, and everything downstream -- releasing the
        tab, closing the space -- is designed to make that pane vanish.

        Returns None when the worker has said nothing terminal, so callers can
        route unconditionally without deciding what counts.
        """
        data = self.store.load() if data is None else data
        worker = data.get("workers", {}).get(worker_id)
        if worker is None:
            return None
        task = data.get("tasks", {}).get(worker.get("task_id"))
        project = data.get("projects", {}).get(worker.get("project_id"))
        if task is None or project is None:
            return None
        report = None
        for message in data.get("messages", []):
            if (
                message.get("worker_id") == worker_id
                and message.get("kind") in self.TERMINAL_REPORT_KINDS
            ):
                report = message
        if report is None:
            return None
        decision = ""
        decision_id = ""
        for item in self._load_status(project["id"]).get("action_items", []):
            if item.get("status", "open") != "open":
                continue
            if item.get("kind") != DELIVERY_DECISION_KIND:
                continue
            if item.get("task_id") in (None, task["id"]):
                decision = _safe_text(item.get("text", ""))
                decision_id = _safe_text(item.get("id", ""))
        summary = _safe_text(report.get("text", "")).strip()[:600]
        label = f"{project_glyph(project.get('color', ''))} {project.get('name', project['id'])}"
        lines = [
            f"FINAL OUTCOME {label} ({project['id']}) task={task['id']} "
            f"[{task.get('status')}] branch={task.get('branch') or '-'} "
            f"worker={worker_id} {report['kind']}: {summary}",
        ]
        if decision:
            lines.append(
                f"DECISION NEEDED: {decision}. Read it with "
                f"`helm task outcome {task['id']}`."
            )
        return {
            "worker_id": worker_id,
            "project_id": project["id"],
            "project_name": project.get("name", project["id"]),
            "project_color": project.get("color", ""),
            "task_id": task["id"],
            "task_status": task.get("status"),
            "kind": report["kind"],
            "summary": summary,
            "decision": decision,
            "decision_id": decision_id,
            "text": "\n".join(lines),
        }

    def outcome_reached_the_record(self, notice: dict[str, Any] | None) -> bool:
        """Whether the durable record actually holds this outcome, by reading it.

        The durable write happens in the unlocked effects pass, where a failure
        -- a full disk, a refused over-long line, a permission change -- is
        suppressed so it cannot cost the worker its message. That makes
        "durable" something to verify rather than assume: claiming the channel
        because a notice had text would let the tab be released and the space
        closed on an outcome the record never received, which is precisely the
        disappearance this routing exists to stop.
        """
        if not notice:
            return False
        project_id = notice.get("project_id")
        task_id = notice.get("task_id")
        if not project_id:
            return False
        try:
            status = self._load_status(project_id)
        except (HelmError, OSError):
            return False
        for item in status.get("action_items", []):
            if item.get("status", "open") != "open":
                continue
            if item.get("kind") != DELIVERY_DECISION_KIND:
                continue
            if item.get("task_id") in (None, task_id):
                return True
        marker = f"task {task_id}"
        entries = list(status.get("situation", [])) + list(status.get("history", []))
        return any(marker in _safe_text(entry.get("text", "")) for entry in entries)

    def record_outcome_handoff(
        self, worker_id: str, channels: Sequence[str], notice: dict[str, Any] | None = None
    ) -> dict[str, Any] | None:
        """Record where a terminal outcome was actually delivered.

        A route nobody recorded cannot be checked, and the check is the point:
        no tab closes and no space closes until this says the outcome reached
        somewhere that outlives the pane.
        """
        with self.store.locked() as data:
            worker = data.get("workers", {}).get(worker_id)
            if worker is None:
                return None
            handoff = {
                "at": now(),
                "channels": sorted({_safe_text(c) for c in channels if c}),
                "task_id": (notice or {}).get("task_id"),
                "decision_id": (notice or {}).get("decision_id", ""),
            }
            worker["outcome_handoff"] = handoff
            return dict(handoff)

    @staticmethod
    def outcome_handoff_done(worker: dict[str, Any]) -> bool:
        """Whether this worker's outcome has reached a surface that outlives it."""
        handoff = worker.get("outcome_handoff") or {}
        return bool(handoff.get("channels"))


    @staticmethod
    def _summary_payload(payload: dict[str, Any] | None) -> bool:
        if not isinstance(payload, dict):
            return False
        return any(
            payload.get(key) is True
            for key in ("summary", "outcome_summary", "report_to_foreman", "report_to_helm")
        )


    def _situation_line(self, prefix: str, summary: str) -> str:
        """Fit a generated report into one situation line.

        `record_situation` refuses an over-long note rather than cutting it,
        because a human-written note keeps its point at the end. This one is
        generated, and the full text is already durable in the message record,
        so trimming the mirrored copy loses nothing and keeps the summary from
        being dropped entirely for being long.
        """
        room = self.SITUATION_LINE_LIMIT - len(prefix)
        if room <= 0:
            return prefix[: self.SITUATION_LINE_LIMIT]
        if len(summary) <= room:
            return f"{prefix}{summary}"
        return f"{prefix}{summary[: max(1, room - 1)]}…"


    def record_task_progress_summary(
        self,
        task_id: str,
        text: str,
        *,
        source: str = "helm",
    ) -> dict[str, Any]:
        """Append one commander-facing progress line for a task.

        Worker messages are the full event stream. This is the curated line a
        future coordinator or commander should see in `helm project status`
        without opening panes or reconstructing a long review loop.
        """
        data = self.store.load()
        task = self._task(data, task_id)
        project = self._project(data, task["project_id"])
        summary = _safe_text(text).strip()
        if not summary:
            raise HelmError("progress summary is required")
        prefix = _safe_text(source).strip() or "helm"
        entry = self.record_situation(
            project["id"],
            f"{prefix}: task {task_id} [{task['status']}] {summary}",
        )
        action_item = self._action_item_from_summary(summary)
        if action_item:
            self.record_project_action_item(
                project["id"], action_item, source=prefix, task_id=task_id
            )
        return entry

    def capture_evidence(self, worker_id: str) -> dict[str, Any] | None:
        """Snapshot why a worker failed, before anything that holds it closes.

        A pane is only the evidence because nothing else keeps it. Written here
        first, the diagnosis outlives the tab -- which is what lets a finished
        pane close without losing the reason it failed.
        """
        data = self.store.load()
        worker = data.get("workers", {}).get(worker_id)
        if worker is None:
            return None
        task = data.get("tasks", {}).get(worker.get("task_id"))
        if task is None or task.get("status") not in {"failed", "blocked", "approval-needed"}:
            return None
        entry = {
            "worker_id": worker_id,
            "task_id": task["id"],
            "task_status": task["status"],
            "brief": _safe_text(task.get("brief", "")).strip().splitlines()[0][:180],
            "branch": task.get("branch"),
            "workspace": task.get("workspace"),
            "captured_at": now(),
            "signatures": self.worker_failures(worker_id),
            "tail": self.worker_output(worker_id, lines=25),
            "messages": [
                _safe_text(m.get("text", ""))[:300]
                for m in data.get("messages", [])
                if m.get("worker_id") == worker_id
                and m.get("kind") in {"blocker", "failure", "result"}
            ][-3:],
        }
        with self._status_transaction(worker["project_id"]) as status:
            status["evidence"][worker_id] = entry
        return entry


    def project_status(self, project_id: str) -> dict[str, Any]:
        """Everything a coordinator needs to take this project over mid-stream.

        Derived state is recomputed rather than appended, so it cannot grow;
        evidence is dropped once its task resolves, because a diagnosis for
        finished work is clutter, not history.
        """
        data = self.store.load()
        project = self._project(data, project_id)
        # Derived, so a gate answered by a path that never ran this check --
        # or by state changed outside Helm -- still reads as answered here.
        with contextlib.suppress(HelmError, OSError):
            self.resolve_delivery_decisions(project_id, data=data)
            self.refresh_finalization_decisions(project_id, data=data)
            self.refresh_failure_decisions(project_id, data=data)
        tasks = [t for t in data.get("tasks", {}).values() if t["project_id"] == project_id]

        def still_worth_keeping(entry: dict[str, Any]) -> bool:
            """Attention is derived from the live record, never accumulated.

            A diagnosis for finished work is clutter, and so is the pane capture
            from a pause that has since been decided and acted on: it was listed
            as an unresolved item after the authorized action succeeded, which
            trains the reader to skip the list.
            """
            task = data.get("tasks", {}).get(entry.get("task_id")) or {}
            state = task.get("status") or ""
            if state in {"merged", "pr-merged"}:
                return False
            if state in {"failed", "blocked"}:
                return True
            return self.task_hold(task) is not None

        with self._status_transaction(project_id) as status:
            pruned = {
                worker_id: entry
                for worker_id, entry in status["evidence"].items()
                if still_worth_keeping(entry)
            }
            status["evidence"] = pruned
            status_snapshot = json.loads(json.dumps(status))
        status = status_snapshot
        health = [h for h in self.worker_health() if h["project_id"] == project_id]
        return {
            "project": {"id": project_id, "name": project.get("name"),
                        "glyph": project_glyph(project.get("color", ""))},
            "counts": {
                state: sum(1 for t in tasks if t.get("status") == state)
                for state in sorted({t.get("status", "?") for t in tasks})
            },
            # `working`, `driving` and `quiet` all mean the worker's own log is
            # MOVING -- it is demonstrably alive and busy, it simply has not
            # pushed a protocol message lately. That is health information, not
            # something a human can act on, and putting it in an attention list
            # is how the list stops being read: in one session these fired eight
            # times against workers that were mid-compile or writing to disk that
            # very second, and every one cost a verification round to dismiss. A
            # signal that is usually wrong teaches the reader to skim the one
            # time it is right. `stalled` is the verdict that means gone dark and
            # it stays, as do every genuine fault and every ask.
            "needs_attention": [
                h for h in health if h["verdict"] not in HEALTHY_WORKER_VERDICTS
            ],
            "unmerged": [
                {"task_id": t["id"], "status": t["status"], "branch": t.get("branch"),
                 "brief": _safe_text(t.get("brief", "")).strip().splitlines()[0][:120]}
                for t in tasks
                if t.get("status") in {"completed", "approved", "approval-needed", "pr-open"}
                # A foreman produces no change, so it is never work to merge.
                and t.get("role") != "foreman"
            ],
            "grants": [g for g in self.list_approval_grants()
                       if g["project_id"] in (None, project_id)],
            "action_items": [
                {**e, "kind": e.get("kind", FOLLOW_UP_ACTION_KIND)}
                for e in status.get("action_items", [])
                if e.get("status", "open") == "open"
            ],
            "situation": [e for e in status["situation"] if not e.get("superseded_by")],
            "pending_requests": self.pending_foreman_requests(project_id, data=data),
            "evidence": list(pruned.values()),
            "history_entries": len(status["history"]),
        }

    def acknowledge_updates(
        self, project_id: str, entry_ids: list[str] | None = None
    ) -> list[dict[str, Any]]:
        """Record that an owed report was RELAYED, not merely read.

        The distinction is the whole point. Reading is done by an agent, and
        an agent's output reaches a commander only if the agent passes it on;
        a report consumed by a read that nobody acted on is a report lost with
        the record claiming otherwise. So relaying becomes an explicit act,
        and an unacknowledged report keeps coming back.

        Who acknowledged is recorded too, so "the coordinator saw it" and "the
        commander was told" stop being the same sentence in the record.
        """
        identity = self.caller_identity()
        acknowledged: list[dict[str, Any]] = []
        wanted = set(entry_ids or [])
        with self._status_transaction(project_id) as status:
            for entry in status.get("situation", []):
                if not entry.get("surface") or entry.get("acknowledged_at"):
                    continue
                if wanted and entry.get("id") not in wanted:
                    continue
                entry["acknowledged_at"] = now()
                entry["acknowledged_by"] = identity.get("role") or "unknown"
                acknowledged.append(entry)
        return acknowledged

    def project_updates_for_watch(
        self,
        project_id: str | None = None,
        *,
        mark_seen: bool = True,
        limit_per_project: int = 3,
    ) -> list[dict[str, Any]]:
        """Return project situation lines not yet surfaced by ``helm watch``.

        Foremen record commander-facing progress in a project's status record,
        but a quiet foreman can fail to relay that line back into the root
        session. ``helm watch`` is the session-facing attention surface, so it
        has to bridge that gap without replaying the whole status forever.

        A long-lived local root can already have a backlog the first time this
        feature runs. Show the latest few lines, then mark the whole backlog as
        seen so the commander gets the current state rather than a transcript.
        """
        data = self.store.load()
        projects = [
            project
            for project in data.get("projects", {}).values()
            if project_id is None or project["id"] == project_id
        ]
        updates: list[dict[str, Any]] = []
        seen_at = now()
        limit = max(1, limit_per_project)
        for project in sorted(projects, key=lambda p: p["id"]):
            # Derived first, so a gate already answered by a path that never
            # ran this check -- or by state changed outside Helm -- reads as
            # answered here rather than being surfaced again.
            with contextlib.suppress(HelmError, OSError):
                self.resolve_delivery_decisions(project["id"], data=data)
                self.refresh_finalization_decisions(project["id"], data=data)
                self.refresh_failure_decisions(project["id"], data=data)
            # One transaction per project: this marks what it surfaced, so a
            # concurrent release writing the same record cannot lose either
            # change.
            with self._status_transaction(project["id"]) as status:
                # A follow-up is news, so it is shown once. A delivery decision
                # is a gate: it keeps showing until somebody answers it,
                # because a gate surfaced once and then hidden is how finished
                # work stops being anybody's problem.
                action_items = [
                    entry
                    for entry in status.get("action_items", [])
                    if entry.get("status", "open") == "open"
                    and (
                        not entry.get("surfaced_at")
                        or entry.get("kind") in GATE_ACTION_KINDS
                    )
                ]
                # An owed report keeps coming back until somebody ACKNOWLEDGES
                # it, not merely until something reads it. "Surfaced" was set
                # by any read, and the reader here is an agent whose output a
                # commander may never see -- so a terminal report was
                # permanently consumed by a process that piped it to a filter
                # and dropped it, with the commander never told. Two commands
                # draining one queue made it worse: whichever ran first won.
                # Routine lines keep show-once; repeating those is the noise
                # that teaches a reader to skip the section.
                pending = [
                    entry
                    for entry in status.get("situation", [])
                    if not entry.get("superseded_by")
                    # A foreman that escalated and has since been replaced is
                    # answered by the replacement; keep the record, drop the
                    # summons.
                    and not self._superseded_foreman_report(data, entry)
                    and (
                        not entry.get("acknowledged_at")
                        if entry.get("surface")
                        else not entry.get("surfaced_at")
                    )
                ]
                if not pending and not action_items:
                    continue
                label = {
                    "project_id": project["id"],
                    "project_name": project.get("name", project["id"]),
                    "glyph": project_glyph(project.get("color", "")),
                }
                for entry in action_items:
                    gate = entry.get("kind") == DELIVERY_DECISION_KIND
                    marker = "DECISION REQUIRED" if gate else "ACTION REQUIRED"
                    names = f" (task {entry['task_id']})" if entry.get("task_id") else ""
                    updates.append({
                        **label,
                        "id": entry["id"],
                        "at": entry.get("at"),
                        "text": f"{marker}: {entry.get('text', '')}{names}",
                        "kind": "action",
                    })
                # The per-project limit exists so a long-quiet root gets the
                # current state instead of a transcript. It must not apply to a
                # terminal report: those are shown in full, however many
                # arrived, and only routine lines compete for the remaining
                # room. Marking an unshown result seen is how a result is lost,
                # and a lost result is indistinguishable from a project that
                # never reported at all.
                owed = [
                    entry for entry in pending
                    if entry.get("surface") and not entry.get("acknowledged_at")
                ]
                routine = [entry for entry in pending if not entry.get("surface")]
                room = max(0, limit - len(owed))
                kept = routine[-room:] if room else []
                shown = sorted(owed + kept, key=lambda e: pending.index(e))
                hidden = len(routine) - len(kept)
                if hidden:
                    updates.append({
                        **label,
                        "id": f"{project['id']}:surface-backlog",
                        "at": shown[0].get("at"),
                        "text": (
                            f"{hidden} older project update(s) marked surfaced; "
                            f"showing latest {len(shown)}"
                        ),
                        "kind": "situation",
                    })
                for entry in shown:
                    updates.append({
                        **label,
                        "id": entry["id"],
                        "at": entry.get("at"),
                        "text": entry.get("text", ""),
                        "kind": "situation",
                        # Whether this is a report the commander is OWED, or an
                        # ordinary progress line. A caller that must not nag --
                        # an unattended watch, say -- needs to tell them apart:
                        # an owed report clears when it is acknowledged, while a
                        # routine line has nothing to clear it and would repeat
                        # for the life of the root.
                        "owed": bool(entry.get("surface"))
                        and not self._superseded_foreman_report(data, entry),
                    })
                if mark_seen:
                    for entry in action_items:
                        entry["surfaced_at"] = seen_at
                    for entry in pending:
                        entry["surfaced_at"] = seen_at
        return updates


    def reflection_evidence(self, since_hours: float = 24.0) -> dict[str, Any]:
        """Assemble what actually happened, for an agent to reflect on.

        Helm gathers facts; it does not draw conclusions. Judging whether a
        pattern is a defect worth fixing needs reading, and a script that
        guessed would produce noise nobody acts on. What it can do reliably is
        surface the evidence a reflection would otherwise have to dig for.
        """
        data = self.store.load()
        cutoff = _dt.datetime.now(_dt.timezone.utc).timestamp() - since_hours * 3600

        def recent(stamp: str | None) -> bool:
            if not stamp:
                return False
            with contextlib.suppress(ValueError):
                return _dt.datetime.fromisoformat(
                    stamp.replace("Z", "+00:00")
                ).timestamp() >= cutoff
            return False

        tasks = [t for t in data.get("tasks", {}).values() if recent(t.get("created_at"))]
        messages = [m for m in data.get("messages", []) if recent(m.get("created_at"))]
        by_kind: dict[str, int] = {}
        for message in messages:
            by_kind[message.get("kind", "?")] = by_kind.get(message.get("kind", "?"), 0) + 1
        failures = [
            {"task_id": m.get("task_id"), "text": _safe_text(m.get("text", ""))[:300]}
            for m in messages
            if m.get("kind") in {"failure", "blocker"}
        ]
        return {
            "window_hours": since_hours,
            "tasks_created": len(tasks),
            "task_states": {
                state: sum(1 for t in tasks if t.get("status") == state)
                for state in sorted({t.get("status", "?") for t in tasks})
            },
            "message_counts": by_kind,
            "failures_and_blockers": failures,
            "tasks_without_domain": [
                t["id"] for t in tasks if not t.get("domain")
            ],
            "unmerged_completed": [
                t["id"] for t in tasks
                if t.get("status") in {"completed", "approved", "approval-needed"}
            ],
            "health": self.worker_health(),
            "prompt": (
                "Reflect on this: which of these were caused by Helm rather than by "
                "the work? Look for anything you did more than twice by hand, any "
                "failure a check could have caught earlier, and any knowledge produced "
                "that has nowhere durable to live. Propose improvements; do not "
                "implement them here."
            ),
        }
