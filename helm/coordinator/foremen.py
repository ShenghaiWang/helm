"""Appointing, briefing and standing down a project's one foreman.

A mixin over `CoordinatorBase`, split out of `status` -- which had grown to
cover seven of these at once. Moved verbatim; it imports nothing from
`helm.core`.
"""

from __future__ import annotations

import contextlib
import copy
import json
import re
import time
from typing import Any

from ..errors import HelmError, SafetyError
from ..paths import canonical
from ..values import FOREMAN_DOMAIN, FOREMAN_RULES, _TERMINAL_WORKER_TASK_STATES, now


class ForemenMixin:
    @staticmethod
    def _live_foreman_task_in(data: dict[str, Any], project_id: str) -> dict[str, Any] | None:
        """The task record of the project's running foreman, from state in hand.

        Gates live on the foreman's own driving task, not on the worker task
        they eventually clear -- one foreman drives one requirement/solution
        cycle at a time, and a worker task only ever consumes that decision.
        """
        for worker in data.get("workers", {}).values():
            if worker.get("project_id") != project_id or worker.get("status") != "running":
                continue
            task = data.get("tasks", {}).get(worker.get("task_id"))
            if (task or {}).get("role") == "foreman":
                return task
        return None

    @staticmethod
    def _live_foreman_in(data: dict[str, Any], project_id: str) -> dict[str, Any] | None:
        """The project's running foreman, read from state already in hand.

        `foreman_for` re-reads the store, which is wrong for a caller that is
        holding the lock: it would answer from the copy on disk rather than the
        one about to be written.
        """
        for worker in data.get("workers", {}).values():
            if worker.get("project_id") != project_id or worker.get("status") != "running":
                continue
            task = data.get("tasks", {}).get(worker.get("task_id"))
            if (task or {}).get("role") == "foreman":
                return worker
        return None

    @staticmethod
    def _superseded_foreman_report(
        data: dict[str, Any], entry: dict[str, Any]
    ) -> bool:
        """True when this report came from a foreman that has been replaced.

        A foreman that escalates ends `blocked`, and that record is permanent
        evidence of what happened -- rightly. But appointing its replacement
        IS the answer to the escalation, so continuing to present it as
        needing a human turns the attention list into a list of things
        already dealt with. Seven such entries on one project trained the
        reader to skim past the two that were real.
        """
        task_id = entry.get("task_id")
        if not task_id:
            return False
        task = data.get("tasks", {}).get(task_id)
        if not task or task.get("role") != "foreman":
            return False
        if task.get("status") not in {"blocked", "failed"}:
            return False
        # WHAT ANSWERS THE ESCALATION IS THAT A REPLACEMENT WAS APPOINTED --
        # not that it is still running. The first version of this required a
        # LIVE successor, which held only while one happened to be driving:
        # a foreman that took over, finished the work and stood down left every
        # blocker behind it summoning a human again. Six such entries, aged 7
        # to 18 hours, were still on the attention list the morning after the
        # work they described had been completed and merged.
        #
        # So: a successor in ANY state supersedes. Live is one case; created
        # later is the other, and `created_at` decides it. The tie that
        # timestamps cannot resolve -- two foremen appointed in the same second
        # -- is why the live test is kept rather than replaced.
        # SUCCESSION IS ORDERED BY `created_at`, and the mapping's own order
        # cannot substitute for it: state is written with `sort_keys=True`, so
        # `data["tasks"]` comes back in ALPHABETICAL ID ORDER. Task ids are
        # random hex, which makes that ordering random -- an insertion-order
        # reading of it looked right and failed intermittently.
        #
        # `created_at` has one-second resolution, so two foremen appointed in
        # the same second tie and neither reads as later. That is why the LIVE
        # test below is kept as well as the timestamp: in the tie that matters
        # -- a replacement appointed moments after the escalation -- the
        # replacement is still running.
        created_at = task.get("created_at") or ""
        for other in data.get("tasks", {}).values():
            if other.get("role") != "foreman":
                continue
            if other.get("project_id") != task.get("project_id"):
                continue
            if other.get("id") == task_id:
                continue
            if other.get("status") in {"created", "allocated", "running"}:
                return True
            # A successor that took over and has SINCE FINISHED answers the
            # escalation just as well as one still running. Requiring a live
            # replacement was the original defect: every blocker behind a
            # foreman that did the work and stood down started summoning again.
            if (other.get("created_at") or "") > created_at:
                return True
        return False

    def pending_foreman_requests(
        self, project_id: str, *, data: dict[str, Any] | None = None
    ) -> list[dict[str, Any]]:
        """Requests routed to this project's foreman that it has not acted on.

        ``helm route`` records the commander's request as an ``answer`` on the
        foreman's own task, then relies on the foreman reading it when it comes
        up. That read had nowhere to happen: the record a foreman is told to
        re-read is built from tasks, situation, action items and evidence, and
        never looked at its own inbound messages. A foreman appointed by the
        same ``route`` call was the case that always lost -- its brief is
        composed while it is being appointed, which is strictly before the
        request is recorded, so the request could appear in neither. It came
        up, correctly saw nothing in flight, and stood down while the commander
        had been told the request was safely recorded.

        Pending is derived rather than marked, for the same reason the rest of
        this record is: a ``seen`` flag would be written by whoever happened to
        read the status, and the root reads it far more often than the foreman
        does. A request counts as acted on once the foreman itself has spoken
        after it arrived -- any push of its own is evidence it read the record
        that carried the request. Nothing is stored, so this cannot drift.
        """
        data = data if data is not None else self.store.load()
        foreman_tasks = {
            task["id"]
            for task in data.get("tasks", {}).values()
            if task.get("project_id") == project_id and task.get("role") == "foreman"
        }
        if not foreman_tasks:
            return []
        # Only a live foreman can still act on one. A stood-down foreman's
        # unread request is not pending on anybody -- it is lost, and it
        # surfaces to the commander as an undriven project instead.
        live = self.foreman_for(project_id, data=data)
        if live is None:
            return []
        # Ordered by position, not by `created_at`. Timestamps here are
        # second-resolution, and a foreman answering promptly lands its reply
        # inside the same second as the request -- which read as "already
        # replied" and hid the request, reintroducing the bug this exists to
        # close. Append order is the actual sequence and has no ties.
        messages = data.get("messages", [])
        replied_at = max(
            (
                index
                for index, message in enumerate(messages)
                if message.get("worker_id") == live["id"]
                and message.get("kind") != self.ANSWER_MESSAGE_KIND
            ),
            default=-1,
        )
        return [
            {
                "message_id": message["id"],
                "task_id": message.get("task_id"),
                "at": message.get("created_at"),
                "text": message.get("text", ""),
            }
            for index, message in enumerate(messages)
            if index > replied_at
            and message.get("kind") == self.ANSWER_MESSAGE_KIND
            and message.get("task_id") in foreman_tasks
            # An `answer` is how `route` records a commander's request, and it
            # is also how several internal paths record a notice -- cleanup
            # writes one per worker to say an escalation is settled. Those are
            # statements, not questions: nobody is waiting on the foreman to
            # act on them, and counting them told the commander a foreman was
            # ignoring instructions it had never been given. A single sweep of
            # this root produced 3,980 of them. The writers already mark
            # themselves in the payload, so this only had to start reading it.
            and not (message.get("payload") or {}).get("source")
        ]

    def foreman_brief(self, project_id: str, *, request: str | None = None) -> str:
        """The role document a project's foreman is started with.

        A foreman is a bounded delegate, not a second coordinator: it drives
        loops inside one project so Helm is not the thing running them, while
        every protected action and the approval gate stay at the root.
        """
        data = self.store.load()
        project = self._project(data, project_id)
        status = self.project_status(project_id)
        lines = [
            FOREMAN_RULES,
            "",
            f"PROJECT: {project.get('name')} ({project_id})",
        ]
        # A foreman appointed *by* a `route` call is the case that always lost
        # the request: its brief is composed here, during appointment, which is
        # strictly before `route` can record the request against a worker that
        # does not exist yet. So the request is passed in and written into the
        # document directly. Nothing derived, nothing to read later, nothing to
        # race: the agent cannot come up without it in front of it.
        pending = list(status["pending_requests"])
        if request and (request or "").strip():
            pending.insert(0, {"at": now(), "text": request})
        if pending:
            # First, and before the state of play, because it is the only part
            # of this document that is someone waiting on an answer. A foreman
            # whose brief opened with a quiet project read "nothing to drive"
            # and stood down on top of an unread request.
            lines.append("")
            lines.append(
                "REQUESTS ROUTED TO YOU -- ACT ON THESE. The commander sent "
                "these to this project and is waiting; they are not a summary "
                "of past work. Do not stand down while one is unanswered:"
            )
            for entry in pending:
                lines.append(f"- [{entry['at'][:10]}] {entry['text']}")
        lines.extend([
            "",
            "CURRENT STATE OF PLAY (re-read it with `helm project status "
            f"{project_id}` rather than trusting this snapshot):",
        ])
        for entry in status["situation"]:
            lines.append(f"- {entry['at'][:10]} {entry['text']}")
        if status["needs_attention"]:
            lines.append("")
            lines.append("NEEDS ATTENTION NOW:")
            for entry in status["needs_attention"]:
                lines.append(
                    f"- {entry['worker_id']} [{entry['verdict']}] {entry['detail']}"
                )
        if status["unmerged"]:
            lines.append("")
            lines.append("UNMERGED WORK (you may drive it; only the human merges):")
            for entry in status["unmerged"]:
                lines.append(f"- [{entry['status']}] {entry['task_id']} {entry['brief']}")
        return "\n".join(lines)

    def create_foreman_task(
        self,
        project_id: str,
        *,
        agent: str | None = None,
        model: str | None = None,
        effort: str | None = None,
        request: str | None = None,
    ) -> dict[str, Any]:
        """Create the task a project's foreman runs as.

        It gets one domain, and it is about driving rather than about the work
        it drives: how to brief a worker, when to answer instead of escalate,
        and what a review is worth. The work itself gets its own domain
        resolved per task, so the foreman's domain must not be the work's --
        a driver carrying `software-delivery` would leak it into every task it
        creates, including the ones that are not code.
        """
        brief = self.foreman_brief(project_id, request=request)
        return self.create_task(
            project_id,
            brief,
            agent=agent,
            model=model,
            effort=effort,
            domain=FOREMAN_DOMAIN,
            role="foreman",
        )

    def project_wants_foreman(self, project_id: str) -> bool:
        """Whether this project runs with a foreman. Default: yes.

        Every project that gets work gets a driver, because the alternative is
        the coordinator remembering to appoint one at the right moment -- and
        a rule that depends on remembering is exactly the failure this exists
        to remove.

        A project that genuinely does not want one says `"foreman": false` in
        its own `.helm/project.json`. That is the whole of what a project may
        say on the subject: it asks for a driver or declines one, and never
        says what the driver may do, because authority is Helm's and a project
        file is untrusted guidance.
        """
        data = self.store.load()
        project = self._project(data, project_id)
        if isinstance(project.get("foreman"), bool):
            return project["foreman"]
        root = canonical(project["root"])
        if not (root / ".helm" / "project.json").exists():
            return True
        with contextlib.suppress(HelmError, SafetyError, OSError):
            return bool(self._discovery_settings(root).get("foreman", True))
        return True

    def foreman_for(
        self, project_id: str, *, data: dict[str, Any] | None = None
    ) -> dict[str, Any] | None:
        """The live foreman for a project, if it already has one.

        One project, one foreman. Two drivers answering the same worker is
        worse than none: the worker gets contradictory instructions and each
        foreman thinks the other's answer was its own.

        Takes an optional snapshot for the same reason its caller above does:
        asked once per project inside a loop, its own `load` was re-parsing
        the whole document per project to answer a question the caller had
        already read the state to ask.
        """
        data = data if data is not None else self.store.load()
        for worker in data.get("workers", {}).values():
            if worker.get("project_id") != project_id or worker.get("status") != "running":
                continue
            task = data.get("tasks", {}).get(worker.get("task_id"))
            if (task or {}).get("role") == "foreman":
                return dict(worker)
        return None

    def stand_down_idle_foreman(self, project_id: str) -> dict[str, Any] | None:
        """Let a project's foreman finish once there is nothing left to drive.

        A foreman was appointed once and never terminated, while releasing a
        project's space requires that no worker in the project is running. So
        for any project with a foreman -- which is every project by default --
        "a finished project releases its space" could never happen. A guarantee
        that cannot fire is worse than no guarantee: it reads as automatic
        cleanup while spaces accumulate for every project ever touched.

        Standing down is safe precisely because a foreman is not supposed to
        carry anything: re-appointment is automatic on the next command that
        starts work, and a fresh foreman is designed to take a project over
        from its status record rather than from a conversation it can no longer
        read. An approval-needed task is deliberately not a reason to stay --
        it is waiting on a human, not on a driver.
        """
        data = self.store.load()
        foreman_tasks = {
            task["id"]
            for task in data.get("tasks", {}).values()
            if task.get("project_id") == project_id and task.get("role") == "foreman"
        }
        if not foreman_tasks:
            return None
        for task in data.get("tasks", {}).values():
            if task.get("project_id") != project_id or task["id"] in foreman_tasks:
                continue
            if task.get("status") in self._DRIVEN_TASK_STATES:
                return None
        candidate = None
        for worker in data.get("workers", {}).values():
            if worker.get("project_id") != project_id or worker.get("status") != "running":
                continue
            if worker.get("task_id") in foreman_tasks:
                candidate = worker
            else:
                # Something it is driving is still alive; it is not idle.
                return None
        if candidate is None:
            return None
        self._terminate_process(candidate.get("pid"))
        with self.store.locked() as locked:
            worker = locked["workers"][candidate["id"]]
            task = self._task(locked, worker["task_id"])
            project = self._project(locked, project_id)
            worker["status"] = "completed"
            worker["exit_code"] = 0
            worker["ended_at"] = now()
            if task["status"] not in _TERMINAL_WORKER_TASK_STATES:
                task["status"] = "completed"
            self._message(
                locked, project, task, worker, "status",
                "Foreman stood down: nothing left to drive",
                {"status": "completed"},
            )
            # "Nothing left to drive" is not the same as "nothing left to
            # decide": a completed task is not a driven state, so a project
            # whose work finished and was never merged stands its foreman down
            # and would otherwise leave that branch with nobody responsible
            # for it at all.
            with contextlib.suppress(HelmError, OSError):
                self.raise_delivery_decision_for_project(
                    project_id, data=locked, source="Foreman stood down"
                )
            return dict(worker)
