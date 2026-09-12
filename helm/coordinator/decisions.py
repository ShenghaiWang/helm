"""The decisions a commander owes an answer to: raising them, and clearing them.

A mixin over `CoordinatorBase`, split out of `status` -- which had grown to
cover seven of these at once. Moved verbatim; it imports nothing from
`helm.core`.
"""

from __future__ import annotations

import contextlib
from pathlib import Path
from typing import Any

from ..errors import HelmError
from ..paths import canonical, overlaps
from ..values import (
    COMMANDER_ASK_KIND,
    COMMANDER_ASK_REASONS,
    DELIVERED_TASK_STATES,
    DELIVERY_DECISION_KIND,
    DELIVERY_DECISION_PROJECT_TEXT,
    DELIVERY_DECISION_TASK_TEXT,
    FAILURE_ACTION_KIND,
    FINALIZATION_ACTION_KIND,
    FOLLOW_UP_ACTION_KIND,
    WORKTREELESS_ROLES,
    _safe_text,
    finalization_text,
    new_id,
    now,
    project_glyph,
    task_owns_branch,
)


class DecisionsMixin:
    def record_project_action_item(
        self,
        project_id: str,
        text: str,
        *,
        source: str = "helm",
        task_id: str | None = None,
        key: str | None = None,
        kind: str = FOLLOW_UP_ACTION_KIND,
    ) -> dict[str, Any]:
        """Record commander-visible follow-up that needs a decision or task.

        Situation lines say what happened. Action items say what still needs a
        human or a new piece of work. Keeping those separate prevents a
        non-blocking review caveat from being buried inside a long outcome.

        ``kind`` separates a free-text follow-up from a gate Helm raises and
        can later answer by itself. A typed item is deduplicated by what it is
        about rather than by its wording, because Helm may raise the same gate
        from several paths -- a worker result, a foreman's final report, a
        foreman standing down -- and three copies of one decision is the same
        noise as none.
        """
        summary = _safe_text(text).strip()
        if not summary:
            raise HelmError("an action item is required")
        if len(summary) > self.SITUATION_LINE_LIMIT:
            raise HelmError(
                f"an action item must be concise: {len(summary)} characters given, "
                f"limit {self.SITUATION_LINE_LIMIT}"
            )
        task = _safe_text(task_id).strip() if task_id else None
        prefix = _safe_text(source).strip() or "helm"
        marker = _safe_text(key).strip() if key else None
        label = _safe_text(kind).strip() or FOLLOW_UP_ACTION_KIND
        with self._status_transaction(project_id) as status:
            for item in status["action_items"]:
                if item.get("status", "open") != "open":
                    continue
                # Keyed items are one per key: the same hold asking twice is one
                # thing for the commander to decide, not two.
                if marker and item.get("key") == marker:
                    return item
                if item.get("kind", FOLLOW_UP_ACTION_KIND) != label:
                    continue
                # A gate Helm raises itself is one per task, however many
                # paths reach it; a free-text follow-up is deduped only
                # when it is genuinely the same note.
                if label != FOLLOW_UP_ACTION_KIND:
                    if item.get("task_id") == task:
                        return item
                    continue
                if (
                    item.get("text") == summary
                    and item.get("task_id") == task
                    and item.get("source") == prefix
                ):
                    return item
            item = {
                "id": new_id("i"),
                "at": now(),
                "text": summary,
                "source": prefix,
                "task_id": task,
                # What this item is *about*, so it can be closed when that thing
                # resolves. An unkeyed "Authorize or refuse" line had no way
                # back: it stayed open after the action it asked about succeeded.
                "key": marker,
                # What kind of item this is: only a gate Helm raises for
                # itself may be auto-resolved. Helm knows when a delivery
                # decision was taken; it cannot know whether somebody's
                # written-down caveat was dealt with.
                "kind": label,
                "status": "open",
            }
            status["action_items"].append(item)
        return item

    def resolve_project_action_items(
        self, project_id: str, key: str, *, outcome: str = "resolved"
    ) -> list[dict[str, Any]]:
        """Close the commander's items for one thing that has now resolved."""
        marker = _safe_text(key).strip()
        if not marker:
            return []
        closed: list[dict[str, Any]] = []
        with self._status_transaction(project_id) as status:
            for item in status["action_items"]:
                if item.get("key") == marker and item.get("status", "open") == "open":
                    item["status"] = outcome
                    item["resolved_at"] = now()
                    closed.append(dict(item))
        return closed

    def resolve_action_item(
        self, project_id: str, item_id: str, *, note: str = ""
    ) -> dict[str, Any]:
        """Close one commander-visible item by hand, on the commander's word.

        Helm closes the gates it raised for itself when their task moves on;
        a free-text follow-up is somebody's judgement about later, and only a
        human knows whether it was dealt with. So this is root-only, like the
        approvals: an agent that could close a decision has decided it.
        """
        self.authority("closing a follow-up")
        marker = _safe_text(item_id).strip()
        if not marker:
            raise HelmError("an action item id is required")
        with self._status_transaction(project_id) as status:
            for item in status["action_items"]:
                if item.get("id") != marker:
                    continue
                if item.get("status", "open") != "open":
                    raise HelmError(f"{marker} is already {item.get('status')}")
                item["status"] = "resolved"
                item["resolved_at"] = now()
                item["resolved_by"] = "commander"
                item["resolved_reason"] = _safe_text(note).strip()[:120] or "closed by the commander"
                return dict(item)
        raise HelmError(f"unknown action item {marker} on {project_id}")

    def record_commander_ask(
        self,
        reason: str,
        text: str,
        *,
        project_id: str | None = None,
        task_id: str | None = None,
    ) -> dict[str, Any]:
        """Record that the coordinator put a question to the commander.

        Marked rather than derived, and that is a real limitation stated
        openly: the question is asked in whatever harness the coordinator runs
        under, which Helm cannot observe. A `failed-task` decision can be
        derived from the task's own status; this cannot be derived from
        anything, so it is only as complete as the coordinator is honest.

        It exists because Helm could measure what its workers cost a human and
        was blind to what its coordinator cost one. Every worker blocker,
        question and approval request is already in the record; a coordinator
        asking the commander eight questions in an evening left no trace at
        all. Under any evaluation that counts human attention, that is the
        wrong half to be blind to.
        """
        if reason not in COMMANDER_ASK_REASONS:
            raise HelmError(
                f"unknown ask reason: {reason} "
                f"(expected one of {', '.join(COMMANDER_ASK_REASONS)})"
            )
        with self.store.locked() as data:
            # The project is optional, and that is not a convenience. The
            # asks that cost the commander most are often the ones belonging
            # to no project at all -- "which of these should I pick up next",
            # "is this approach right" -- and requiring a project id would
            # have silently excluded exactly the coordinator-overhead half
            # this record exists to measure. Helm itself is not a project
            # under `projects/`, so its own work could not be recorded either.
            task = self._task(data, task_id) if task_id else None
            project = (
                self._project(data, project_id)
                if project_id
                else {"id": task["project_id"]} if task else {"id": None}
            )
            return self._message(
                data,
                project,
                task,
                None,
                COMMANDER_ASK_KIND,
                text,
                {"reason": reason},
            )

    def commander_asks(self, project_id: str | None = None) -> dict[str, Any]:
        """What has been asked of the commander, by reason and by provenance.

        Two provenances, kept apart rather than summed into one flattering
        number. `recorded` is what the coordinator marked as it asked.
        `derived` is what the state already proves a human was asked for --
        a decided gate, a released approval -- and needs nobody's honesty.

        They are reported separately because a total would hide the thing
        worth knowing: if `recorded` is far below `derived`, the coordinator is
        not marking its asks, and every rate computed from it is wrong in the
        direction that flatters Helm.
        """
        data = self.store.load()
        counts = {reason: 0 for reason in COMMANDER_ASK_REASONS}
        for message in data.get("messages", []):
            if message.get("kind") != COMMANDER_ASK_KIND:
                continue
            if project_id is not None and message.get("project_id") != project_id:
                continue
            reason = (message.get("payload") or {}).get("reason")
            if reason in counts:
                counts[reason] += 1

        gates_decided = 0
        approvals = 0
        for task in data.get("tasks", {}).values():
            if project_id is not None and task.get("project_id") != project_id:
                continue
            for gate in (task.get("gates") or {}).values():
                # A decided gate carries `confirmed_at`, or `skipped` set --
                # there is no `status` field. Reading one that does not exist
                # returns a confident zero, which is the worst possible answer
                # for a metric: it says "the commander was never asked".
                if not isinstance(gate, dict):
                    continue
                if gate.get("confirmed_at") or gate.get("skipped"):
                    gates_decided += 1
            for hold in task.get("holds") or []:
                if isinstance(hold, dict) and hold.get("status") != "waiting":
                    approvals += 1
        return {
            "recorded": counts,
            "recorded_total": sum(counts.values()),
            "derived": {
                "gates_decided": gates_decided,
                "approvals_answered": approvals,
            },
            "derived_total": gates_decided + approvals,
        }

    def open_action_items(self, project_id: str | None = None) -> list[dict[str, Any]]:
        """Every commander-visible item still waiting on a human, project-labelled.

        `helm project status` shows one project's items to whoever already knows
        to look there. This is what lets the default status view say a decision
        is pending without the reader having to go project by project.
        """
        data = self.store.load()
        items: list[dict[str, Any]] = []
        for project in sorted(data.get("projects", {}).values(), key=lambda p: p["id"]):
            if project_id is not None and project["id"] != project_id:
                continue
            with contextlib.suppress(HelmError, OSError):
                self.resolve_delivery_decisions(project["id"], data=data)
                self.refresh_finalization_decisions(project["id"], data=data)
                self.refresh_failure_decisions(project["id"], data=data)
            status = self._load_status(project["id"])
            for entry in status.get("action_items", []):
                if entry.get("status", "open") != "open":
                    continue
                items.append({
                    **entry,
                    "kind": entry.get("kind", FOLLOW_UP_ACTION_KIND),
                    "project_id": project["id"],
                    "project_name": project.get("name", project["id"]),
                    "glyph": project_glyph(project.get("color", "")),
                })
        return items

    @staticmethod
    def _action_item_from_summary(text: str) -> str | None:
        summary = _safe_text(text).strip()
        lowered = summary.lower()
        markers = (
            "follow-up needed",
            "needs follow-up",
            "requires follow-up",
            "action required",
            "needs commander decision",
            "needs human decision",
        )
        return summary if any(marker in lowered for marker in markers) else None

    @staticmethod
    def _action_item_from_payload(payload: dict[str, Any] | None) -> str | None:
        if not isinstance(payload, dict):
            return None
        for key in ("action_item", "follow_up", "followup"):
            value = payload.get(key)
            if isinstance(value, str) and value.strip():
                return _safe_text(value).strip()
        if payload.get("needs_action") is True or payload.get("action_required") is True:
            value = payload.get("summary") or payload.get("text")
            if isinstance(value, str) and value.strip():
                return _safe_text(value).strip()
        return None

    @staticmethod
    def _decided(gate: Any) -> bool:
        """A gate the commander has actually ruled on, either way."""
        return bool(gate) and (gate.get("confirmed_at") is not None or gate.get("skipped"))

    @staticmethod
    def task_delivery_resolved(task: dict[str, Any]) -> bool:
        """Whether a task's outcome has actually been settled.

        Delivered by a merge or a merged PR, or explicitly cleaned up. Nothing
        else counts -- in particular not `completed`, and not the absence of a
        worker or a pane. A task whose worker finished and whose tab was closed
        looks quiet from every direction and is still a change nobody decided
        anything about.
        """
        if task.get("status") in DELIVERED_TASK_STATES:
            return True
        return bool(task.get("workspace_removed"))

    def unresolved_delivery_tasks(
        self, project_id: str, data: dict[str, Any] | None = None
    ) -> list[dict[str, Any]]:
        """This project's task work whose outcome nobody has decided yet.

        Bookkeeping roles are excluded for the same reason they are left out of
        `unmerged`: a foreman or a reviewer produces no branch and no artifact,
        so it has nothing to deliver and must never be the reason a project
        looks unfinished.
        """
        data = self.store.load() if data is None else data
        return [
            task
            for task in data.get("tasks", {}).values()
            if task.get("project_id") == project_id
            and task.get("role") not in WORKTREELESS_ROLES
            and not self.task_delivery_resolved(task)
        ]

    def record_delivery_decision(
        self,
        project_id: str,
        *,
        task_id: str | None = None,
        source: str = "helm",
    ) -> dict[str, Any] | None:
        """Raise the commander-visible gate on an outcome nobody has acted on.

        A worker result is a milestone, and the thing that acts on it is either
        the project's foreman or a human. When there is no foreman left to
        drive it, the decision has to be written down where a fresh coordinator
        will find it, because the alternative is a finished branch that only
        the conversation remembers.
        """
        text = (
            DELIVERY_DECISION_TASK_TEXT if task_id else DELIVERY_DECISION_PROJECT_TEXT
        )
        return self.record_project_action_item(
            project_id,
            text,
            source=source,
            task_id=task_id,
            kind=DELIVERY_DECISION_KIND,
        )

    def resolve_delivery_decisions(
        self,
        project_id: str,
        *,
        task_id: str | None = None,
        reason: str = "",
        data: dict[str, Any] | None = None,
    ) -> list[dict[str, Any]]:
        """Close delivery gates whose decision has since been taken.

        Derived rather than flagged: an item linked to a task is answered once
        that task is delivered or cleaned up, and a project-scoped one is
        answered once no unresolved task work remains. `task_id` closes that
        task's item outright, for the decision that leaves the work unresolved
        on purpose -- continuing it puts the task straight back into a state
        this would otherwise still consider open.

        Only Helm's own delivery gate is touched. A free-text follow-up is
        somebody's judgement about what still needs doing, and Helm has no way
        to know it has been done.
        """
        status = self._load_status(project_id)
        pending = [
            item
            for item in status.get("action_items", [])
            if item.get("status", "open") == "open"
            and item.get("kind") == DELIVERY_DECISION_KIND
        ]
        if not pending:
            return []
        data = self.store.load() if data is None else data
        tasks = data.get("tasks", {})
        outstanding = self.unresolved_delivery_tasks(project_id, data)
        resolved: list[dict[str, Any]] = []
        for item in pending:
            linked = item.get("task_id")
            if linked and task_id and linked == task_id:
                why = reason or "decided"
            elif linked:
                task = tasks.get(linked)
                if task is not None and not self.task_delivery_resolved(task):
                    continue
                why = reason or (
                    _safe_text(task.get("status")) if task else "task no longer recorded"
                )
            else:
                if outstanding:
                    continue
                why = reason or "no unresolved task work remains"
            item["status"] = "resolved"
            item["resolved_at"] = now()
            item["resolved_reason"] = _safe_text(why)[:120]
            resolved.append(item)
        if resolved:
            self._save_status(project_id, status)
        return resolved

    def raise_delivery_decision_for_project(
        self,
        project_id: str,
        *,
        data: dict[str, Any] | None = None,
        source: str = "helm",
    ) -> dict[str, Any] | None:
        """Hand this project's undecided work to the commander.

        Used where a driver stops -- a foreman's terminal report, a foreman
        standing down -- because from that moment nobody is going to act on the
        outcome unless the record says so. One unresolved task is named; several
        stay project-scoped rather than picking one arbitrarily.
        """
        outstanding = self.unresolved_delivery_tasks(project_id, data)
        if not outstanding:
            return None
        linked = outstanding[0]["id"] if len(outstanding) == 1 else None
        return self.record_delivery_decision(
            project_id, task_id=linked, source=source
        )

    def task_retained_resources(
        self, task: dict[str, Any], data: dict[str, Any]
    ) -> list[str]:
        """What this task still holds, read from Helm's own record.

        Deliberately state-driven rather than a live look at the disk. Probing
        git and the filesystem on every `helm status` and `helm watch` costs a
        scan per delivered task, and -- worse -- makes every way of *failing*
        to see a resource look exactly like the resource being gone: a project
        root that has moved, an unreadable checkout, a git that errors would
        each have quietly closed this gate on work that is still there.

        So a resource is held until Helm records letting go of it, which only
        `helm task cleanup` does. Cleanup is also the reconciliation point for
        anything removed outside Helm: it already marks an absent worktree,
        branch, or worker directory as removed. Being wrong in this direction
        costs one line asking about a task that is already clean, and it is
        answered by running the cleanup that was going to be run anyway.

        Concrete about which of the three: a worktree, a branch and a worker
        directory are different things to let go of, and the two a plain
        cleanup can leave behind -- an unmerged branch, a directory belonging
        to a session still alive -- are the ones a commander needs named.
        """
        retained: list[str] = []
        if task.get("workspace") and not task.get("workspace_removed"):
            retained.append("its task worktree")
        if task_owns_branch(task) and not task.get("branch_removed"):
            retained.append(f"its task branch {task['branch']}")
        directories = 0
        for worker in self._task_workers(data, task["id"]):
            config_file = worker.get("config_file")
            if not config_file or worker.get("directory_removed"):
                continue
            # Ownership still has to hold: a directory outside Helm's own
            # state is not Helm's to shed, so it is not Helm's to count. That
            # is a question about the path, not about what is on disk now --
            # requiring it to exist would put the probe straight back.
            with contextlib.suppress(OSError):
                worker_dir = canonical(Path(config_file).parent)
                if overlaps(worker_dir, self.store.directory / "workers"):
                    directories += 1
        if directories:
            retained.append(f"{directories} worker directory/directories")
        return retained

    def refresh_finalization_decisions(
        self, project_id: str, data: dict[str, Any] | None = None
    ) -> dict[str, list[dict[str, Any]]]:
        """Keep the post-delivery cleanup gate in step with what is on disk.

        Derived, like the delivery gate above it, so it is idempotent: running
        it twice raises one item, and it closes itself the moment the task's
        residue is gone. A task holding nothing never gets an item at all --
        a gate that fires on healthy work is the noise that teaches a reader
        to skip the list.

        This raises a decision; it never acts on one. Cleanup stays the
        explicit command it always was, with its dirty, unmerged and
        live-session refusals untouched.
        """
        data = self.store.load() if data is None else data
        raised: list[dict[str, Any]] = []
        resolved: list[dict[str, Any]] = []
        wanted: dict[str, str] = {}
        for task in data.get("tasks", {}).values():
            if task.get("project_id") != project_id:
                continue
            if task.get("status") not in DELIVERED_TASK_STATES:
                continue
            retained = self.task_retained_resources(task, data)
            if retained:
                wanted[task["id"]] = finalization_text(task["id"], retained)
        status = self._load_status(project_id)
        dirty = False
        for item in status.get("action_items", []):
            if item.get("status", "open") != "open":
                continue
            if item.get("kind") != FINALIZATION_ACTION_KIND:
                continue
            linked = item.get("task_id")
            text = wanted.pop(linked, None) if linked else None
            if text is None:
                item["status"] = "resolved"
                item["resolved_at"] = now()
                item["resolved_reason"] = "nothing retained"
                resolved.append(item)
                dirty = True
            elif item.get("text") != text:
                # What is retained changes as pieces are shed -- a branch
                # deleted while the worktree stays. A stale list is worse than
                # none, because it is the part a reader acts on. The item keeps
                # its id: this is the same decision, said more accurately, and
                # a reader who noted the id must not have it renumbered.
                item["text"] = text
                item["updated_at"] = now()
                dirty = True
        if dirty:
            self._save_status(project_id, status)
        for task_id, text in wanted.items():
            item = self.record_project_action_item(
                project_id,
                text,
                source="helm",
                task_id=task_id,
                kind=FINALIZATION_ACTION_KIND,
            )
            if item is not None:
                raised.append(item)
        return {"raised": raised, "resolved": resolved}

    def refresh_failure_decisions(
        self, project_id: str, data: dict[str, Any] | None = None
    ) -> dict[str, list[dict[str, Any]]]:
        """Raise a decision for every failed task, however it failed.

        Derived rather than recorded at the moment of failure, because a task
        reaches `failed` by two routes and only one of them was ever surfaced.
        A worker that *reports* a `failure` goes through the event path, which
        now records it. A worker whose session simply ends -- killed, exited
        non-zero, lost -- is settled by observation through an internal
        message that never touches that path, so it raised nothing at all.
        That is the larger half in practice: the failures already on this root
        are almost all the second kind, filed under a heading in `helm project
        status` that nothing points at.

        Deriving it covers both without threading the project's file lock
        through a path that already holds the state lock, and is idempotent:
        running it twice raises one item, and it resolves itself when the task
        stops being failed -- retried, continued, or cleaned up.
        """
        data = self.store.load() if data is None else data
        raised: list[dict[str, Any]] = []
        resolved: list[dict[str, Any]] = []
        wanted: dict[str, str] = {}
        for task in data.get("tasks", {}).values():
            if task.get("project_id") != project_id:
                continue
            state = task.get("status")
            if state != "failed":
                continue
            # A failed foreman or reviewer is not the commander's decision. The
            # item offers retry, continue or cleanup, and none of those applies
            # to a role that owns no worktree: a failed review round is re-run
            # by the loop that started it, and a project without a driver gets
            # one appointed by the next command that starts work. On this root
            # they were 63 of the 69 open failure items -- most of the backlog
            # was about tasks nothing could be done with.
            if task.get("role") in WORKTREELESS_ROLES:
                continue
            # A failed task whose workspace has been released is settled, even
            # though it is still `failed`. The item offers three ways out --
            # retry, continue, clean up -- and releasing the worktree removes
            # all of them: `continue` reuses the worktree and branch, so with
            # those gone there is no round to run, and cleanup has already
            # happened. The docstring above promised these resolve once a task
            # is cleaned up, but cleanup never touches `status`, so the
            # condition could not fire and every cleaned failure stayed open
            # forever. On this root that was 253 unanswerable items -- more
            # than half the standing backlog -- and it grew with every cleanup.
            # An attention list nobody can empty is one nobody reads.
            if task.get("workspace_removed"):
                continue
            brief = _safe_text(task.get("brief", "")).strip().splitlines()
            opening = brief[0][:120] if brief else "no brief recorded"
            wanted[task["id"]] = (
                f"Task {task['id']} FAILED and needs a decision -- retry it, "
                f"continue it with a new worker, or clean it up: {opening}"
            )
        status = self._load_status(project_id)
        dirty = False
        for item in status.get("action_items", []):
            if item.get("status", "open") != "open":
                continue
            if item.get("kind") != FAILURE_ACTION_KIND:
                continue
            linked = item.get("task_id")
            if linked and wanted.pop(linked, None) is not None:
                continue
            item["status"] = "resolved"
            item["resolved_at"] = now()
            # Say which of the two it was. A cleaned-up failure is still
            # `failed`, so calling it "no longer failed" would be a small lie
            # in the one record a later reader uses to reconstruct what
            # happened here.
            linked_task = data.get("tasks", {}).get(linked) if linked else None
            item["resolved_reason"] = (
                "task failed and its workspace has been released; nothing left to decide"
                if (linked_task or {}).get("status") == "failed"
                else "task is no longer failed"
            )
            resolved.append(item)
            dirty = True
        if dirty:
            self._save_status(project_id, status)
        for task_id, text in wanted.items():
            item = self.record_project_action_item(
                project_id,
                text,
                source="helm",
                task_id=task_id,
                kind=FAILURE_ACTION_KIND,
            )
            if item is not None:
                raised.append(item)
        return {"raised": raised, "resolved": resolved}
