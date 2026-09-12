"""The ledger: what each task cost and what came of it, from Helm's own records.

Helm's value is argued in prose and measured nowhere a commander can look,
which is how a question like "is the protocol worth it" turns into an
expensive replay experiment. The records already hold the answer per task:
when it was created and when it reported, what it cost in tokens, how many
review rounds it took and what they caught, how many times a human had to
step in. This lays them out for a window, live and archived tasks alike.
"""

from __future__ import annotations

import contextlib
import datetime as _dt
import statistics
import time
from typing import Any

from .. import archive, costs
from ..values import DELIVERED_TASK_STATES


def _epoch(stamp: Any) -> float | None:
    if not isinstance(stamp, str) or not stamp:
        return None
    try:
        return _dt.datetime.fromisoformat(stamp.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


class LedgerMixin:
    def ledger(self, *, days: float = 7.0, project_id: str | None = None) -> dict[str, Any]:
        """Every worker task created inside the window, with what it cost and gave."""
        cutoff = time.time() - days * 86400
        data = self.store.load()
        rows: list[dict[str, Any]] = []
        seen: set[str] = set()
        # Live tasks, with their messages and reviewers from the live document.
        reviewers_of: dict[str, list[dict[str, Any]]] = {}
        for task in data.get("tasks", {}).values():
            if task.get("role") == "reviewer" and task.get("reviews"):
                reviewers_of.setdefault(task["reviews"], []).append(task)
        messages_of: dict[str, list[dict[str, Any]]] = {}
        for message in data.get("messages", []):
            if message.get("task_id"):
                messages_of.setdefault(message["task_id"], []).append(message)
        for task_id, task in data.get("tasks", {}).items():
            if not self._ledger_wants(task, cutoff, project_id):
                continue
            reviewers = reviewers_of.get(task_id, [])
            review_results = [
                m for reviewer in reviewers
                for m in messages_of.get(reviewer["id"], []) if m.get("kind") == "result"
            ]
            rows.append(self._ledger_row(task, messages_of.get(task_id, []), review_results, rounds=len(reviewers)))
            seen.add(task_id)
        # Archived tasks created inside the window: their records are whole.
        for task_id in archive.archived_task_ids(self.store.directory):
            if task_id in seen:
                continue
            record = archive.read_task(self.store.directory, task_id)
            if record is None or not self._ledger_wants(record["task"], cutoff, project_id):
                continue
            review_results = []
            reviewer_ids = record.get("reviewer_task_ids", [])
            for reviewer_id in reviewer_ids:
                reviewer = archive.read_task(self.store.directory, reviewer_id)
                if reviewer is not None:
                    review_results.extend(m for m in reviewer.get("messages", []) if m.get("kind") == "result")
            rows.append(self._ledger_row(
                record["task"], record.get("messages", []), review_results,
                rounds=len(reviewer_ids), archived=True,
            ))
        # Creation time first; on a tie an archived row precedes a live one,
        # because a task that has been cleaned up and archived is the older
        # of the two, and the id last so the order is the same on every read.
        rows.sort(key=lambda row: (row["created_at"] or "", 0 if row.get("archived") else 1, row["task_id"]))
        return {"days": days, "project_id": project_id, "rows": rows, "totals": self._ledger_totals(rows)}

    @staticmethod
    def _ledger_wants(task: dict[str, Any], cutoff: float, project_id: str | None) -> bool:
        if task.get("role") != "worker" or task.get("read_only"):
            return False
        if project_id and task.get("project_id") != project_id:
            return False
        created = _epoch(task.get("created_at"))
        return created is not None and created >= cutoff

    def _ledger_row(
        self,
        task: dict[str, Any],
        messages: list[dict[str, Any]],
        review_results: list[dict[str, Any]],
        *,
        rounds: int = 0,
        archived: bool = False,
    ) -> dict[str, Any]:
        created = _epoch(task.get("created_at"))
        results = sorted(
            (m for m in messages if m.get("kind") == "result"), key=lambda m: m.get("created_at") or ""
        )
        first_result = _epoch(results[0].get("created_at")) if results else None
        minutes_to_result = (
            round((first_result - created) / 60, 1) if created is not None and first_result is not None else None
        )
        kinds = [m.get("kind") for m in messages]
        catches = 0
        not_followed = 0
        for m in review_results:
            text = str(m.get("text") or "")
            if not text.lstrip().upper().startswith("CHANGES-REQUESTED"):
                continue
            catches += 1
            with contextlib.suppress(Exception):
                if self.learning_not_followed(text, task.get("domain")):
                    not_followed += 1
        usage = {
            "turns": 0, "cost_usd": None, "cost_source": None, "input_tokens": 0,
            "output_tokens": 0, "cache_read_input_tokens": 0,
        }
        try:
            total = self.task_usage(task["id"])["total"]
            usage = {
                "turns": total.get("turns", 0),
                "cost_usd": total.get("cost_usd") if total.get("cost_known") else None,
                "cost_source": (
                    None if not total.get("cost_known")
                    else "priced" if total.get("priced") else "reported"
                ),
                "input_tokens": total.get("input_tokens", 0),
                "output_tokens": total.get("output_tokens", 0),
                "cache_read_input_tokens": total.get("cache_read_input_tokens", 0),
            }
        except Exception:  # noqa: BLE001 - a row without a cost is still a row
            pass
        delivery = task.get("delivery") or {}
        return {
            "task_id": task["id"],
            "project_id": task.get("project_id"),
            "ticket": task.get("ticket"),
            "shape": task.get("shape") or "standard",
            "agent": task.get("agent_id") or task.get("agent"),
            "model": task.get("model"),
            "effort": task.get("effort"),
            "created_at": task.get("created_at"),
            "status": task.get("status"),
            "context_kb": round((task.get("context_bytes") or 0) / 1000, 1) if task.get("context_bytes") else None,
            # A task's status is the authority once it is delivered: a record
            # written before the merge path advanced its delivery state still
            # says "worktree", and that must never outrank "merged".
            "delivery": (
                task.get("status")
                if task.get("status") in DELIVERED_TASK_STATES
                else delivery.get("state") or task.get("status")
            ),
            "minutes_to_result": minutes_to_result,
            "rounds": len(task.get("rounds") or []) + 1,
            # One reviewer task is one round; its result messages are what
            # it said, and a reviewer can say more than one thing.
            "review_rounds": rounds,
            "review_catches": catches,
            "learning_not_followed": not_followed,
            "questions": kinds.count("question"),
            "blockers": kinds.count("blocker"),
            "approvals": kinds.count("approval") + kinds.count("approval-needed"),
            "archived": archived,
            **usage,
        }

    @staticmethod
    def _ledger_totals(rows: list[dict[str, Any]]) -> dict[str, Any]:
        delivered = sum(1 for r in rows if r["delivery"] in {"merged", "pr-merged"})
        times = [r["minutes_to_result"] for r in rows if isinstance(r["minutes_to_result"], (int, float))]
        costs_known = [r["cost_usd"] for r in rows if isinstance(r["cost_usd"], (int, float))]
        return {
            "tasks": len(rows),
            "delivered": delivered,
            "failed": sum(1 for r in rows if r["status"] == "failed"),
            "median_minutes_to_result": round(statistics.median(times), 1) if times else None,
            "review_rounds": sum(r["review_rounds"] for r in rows),
            "review_catches": sum(r["review_catches"] for r in rows),
            "learning_not_followed": sum(r.get("learning_not_followed", 0) for r in rows),
            "questions": sum(r["questions"] for r in rows),
            "approvals": sum(r["approvals"] for r in rows),
            "output_tokens": sum(r["output_tokens"] for r in rows),
            "cache_read_input_tokens": sum(r["cache_read_input_tokens"] for r in rows),
            "cost_usd": round(sum(costs_known), 2) if costs_known else None,
            "cost_known_for": len(costs_known),
            "priced_for": sum(1 for r in rows if r.get("cost_source") == "priced"),
        }
