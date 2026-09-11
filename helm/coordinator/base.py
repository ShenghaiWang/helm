"""The base every Coordinator mixin sits on: the record primitives."""

from __future__ import annotations

from typing import Any

from .. import preferences as prefs
from ..errors import HelmError
from ..state import StateStore
from ..values import _safe_text, new_id, now


class CoordinatorBase:
    def __init__(self, store: StateStore | None = None):
        self.store = store or StateStore()
        #: Set by `use_preferences` to pin the preferences every method below
        #: reads. None means "resolve them normally", which is what every
        #: ordinary command wants.
        self._preferences_source: prefs.Preferences | None = None

    def _project(self, data: dict[str, Any], project_id: str) -> dict[str, Any]:
        project = data["projects"].get(project_id)
        if project is None:
            raise HelmError(f"unknown project: {project_id}")
        return project

    def _task(self, data: dict[str, Any], task_id: str) -> dict[str, Any]:
        task = data["tasks"].get(task_id)
        if task is None:
            raise HelmError(f"unknown task: {task_id}")
        return task

    def _message(
        self,
        data: dict[str, Any],
        project: dict[str, Any],
        task: dict[str, Any] | None,
        worker: dict[str, Any] | None,
        kind: str,
        text: str,
        payload: dict[str, Any] | None = None,
        *,
        status: str | None = None,
    ) -> dict[str, Any]:
        message = {
            "id": new_id("m"),
            "project_id": project["id"],
            "task_id": task["id"] if task else None,
            "worker_id": worker["id"] if worker else None,
            "kind": kind,
            "status": status,
            "text": _safe_text(text),
            "payload": payload or {},
            "created_at": now(),
        }
        data["messages"].append(message)
        return message

    @staticmethod
    def _task_workers(data: dict[str, Any], task_id: str) -> list[dict[str, Any]]:
        return [worker for worker in data["workers"].values() if worker.get("task_id") == task_id]
