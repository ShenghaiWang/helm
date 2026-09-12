"""Decision hygiene: close the items nothing can act on any more.

`helm status` carried 147 "decisions and follow-ups" on one root, most of
them derived long ago from tasks that have since failed, been cleaned up and
archived. An item about a task nobody can reach is not a decision, it is
noise -- and a list that is mostly noise trains the reader to skip the one
item that needs them.

The typed gates Helm raises for itself (delivery, finalization, failure) are
derived, so re-running their refreshers closes what has moved on. What they
cannot see is a task that has *left the live document*: the archive takes a
settled task's record with it, and an item still pointing at that task has
nothing left to decide. Those are resolved here as "task archived", whatever
their kind -- a free-text follow-up included, because the work it annotated
is gone. Free-text follow-ups on live work are left alone: only a human knows
whether a caveat was dealt with.
"""

from __future__ import annotations

import contextlib
from typing import Any

from ..errors import HelmError
from ..values import now

ARCHIVED_REASON = "task archived"


class TidyMixin:
    def tidy_decisions(
        self, project_id: str | None = None, *, dry_run: bool = False
    ) -> dict[str, Any]:
        """Resolve open action items whose subject is gone or has moved on.

        Returns what was resolved, item by item, and how many open items were
        kept because they still point at something a human can act on.
        """
        data = self.store.load()
        live_tasks = data.get("tasks", {})
        if project_id is not None and project_id not in data.get("projects", {}):
            raise HelmError(f"unknown project: {project_id}")
        projects = [project_id] if project_id else sorted(data.get("projects", {}))
        resolved: list[dict[str, Any]] = []
        kept = 0
        for pid in projects:
            if not dry_run:
                # The derived gates first: a delivery decided elsewhere, a
                # cleanup already run, a failure retried. Each refresher is
                # idempotent and writes only when something changed.
                with contextlib.suppress(HelmError, OSError):
                    self.resolve_delivery_decisions(pid, data=data)
                    self.refresh_finalization_decisions(pid, data=data)
                    self.refresh_failure_decisions(pid, data=data)
            status = self._load_status(pid)
            changed = False
            for item in status.get("action_items", []):
                if item.get("status", "open") != "open":
                    continue
                task_id = item.get("task_id")
                if not task_id or task_id in live_tasks:
                    kept += 1
                    continue
                resolved.append({
                    "project_id": pid,
                    "id": item.get("id"),
                    "kind": item.get("kind"),
                    "task_id": task_id,
                    "reason": ARCHIVED_REASON,
                })
                if dry_run:
                    continue
                item["status"] = "resolved"
                item["resolved_at"] = now()
                item["resolved_reason"] = ARCHIVED_REASON
                changed = True
            if changed:
                with self._status_transaction(pid) as fresh:
                    # Re-apply onto the locked record rather than saving the
                    # copy read above, so a result landing meanwhile is kept.
                    done = {entry["id"] for entry in resolved if entry["project_id"] == pid}
                    for item in fresh.get("action_items", []):
                        if item.get("id") in done and item.get("status", "open") == "open":
                            item["status"] = "resolved"
                            item["resolved_at"] = now()
                            item["resolved_reason"] = ARCHIVED_REASON
        return {"resolved": resolved, "kept": kept, "dry_run": dry_run}
