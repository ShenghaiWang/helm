"""Moving settled records out of the live state document -- see helm/archive.py."""

from __future__ import annotations

import contextlib
import copy
import shutil
from pathlib import Path
from typing import Any

from .. import archive
from ..errors import HelmError, SafetyError
from ..git import _git
from ..paths import canonical, overlaps
from ..values import now, task_owns_branch

#: Above this many bytes, or this many archivable tasks, the live document is
#: paying for records nothing can change, and `helm doctor` says so.
LIVE_DOCUMENT_WARN_BYTES = 16 * 1024 * 1024
ARCHIVABLE_WARN_COUNT = 50


class ArchiveMixin:
    """Archive tasks whose records can no longer change, and read them back."""

    # ---------- eligibility ----------

    def archivable_task_ids(
        self, data: dict[str, Any] | None = None, *, reconcile: bool = False
    ) -> list[str]:
        """Tasks whose record can no longer change.

        Cleanup has happened (`workspace_removed`), nothing is retained -- no
        branch, no worker directory -- no session is running, no escalation
        on the task is still open, and the task is not waiting on a human
        decision. A reviewer goes only with the task it reviewed. With
        `reconcile`, a branch or worker directory the record still claims is
        probed once and marked removed when it is gone -- the same
        reconciliation cleanup performs, for records that predate it.
        """
        data = self.store.load() if data is None else data
        tasks = data.get("tasks", {})
        running = {
            worker.get("task_id")
            for worker in data.get("workers", {}).values()
            if worker.get("status") == "running"
        }
        escalated: set[str] = set()
        with contextlib.suppress(HelmError, OSError):
            escalated = {
                str(message.get("task_id"))
                for message in self.open_escalations()
                if message.get("task_id")
            }
        eligible: set[str] = set()
        for task_id, task in tasks.items():
            if not task.get("workspace_removed"):
                continue
            if task.get("status") in archive.UNDECIDED_TASK_STATES:
                continue
            if task_id in running or task_id in escalated:
                continue
            if reconcile:
                self._reconcile_retained(data, task)
            if self.task_retained_resources(task, data):
                continue
            eligible.add(task_id)
        # A reviewer's record is part of the reviewed task's story: it leaves
        # only when that task does, and never before.
        settled = set(eligible)
        for task_id in list(eligible):
            reviewed = tasks[task_id].get("reviews")
            if reviewed and reviewed in tasks and reviewed not in settled:
                eligible.discard(task_id)
        return sorted(eligible)

    def _reconcile_retained(self, data: dict[str, Any], task: dict[str, Any]) -> None:
        """Mark a branch or worker directory removed when it is in fact gone."""
        project = data.get("projects", {}).get(task.get("project_id")) or {}
        if task_owns_branch(task) and not task.get("branch_removed") and project.get("root"):
            root = canonical(project["root"])
            with contextlib.suppress(HelmError, OSError):
                if root.is_dir():
                    listed = _git(root, "branch", "--list", task["branch"]).strip()
                    if not listed:
                        task["branch_removed"] = True
                        task["branch_removed_at"] = now()
        for worker in self._task_workers(data, task["id"]):
            config_file = worker.get("config_file")
            if not config_file or worker.get("directory_removed"):
                continue
            with contextlib.suppress(OSError):
                worker_dir = canonical(Path(config_file).parent)
                if overlaps(worker_dir, self.store.directory / "workers") and not worker_dir.exists():
                    worker["directory_removed"] = True
                    worker["directory_removed_at"] = now()

    # ---------- archiving ----------

    def archive_tasks(
        self,
        task_ids: list[str] | None = None,
        *,
        dry_run: bool = False,
        reconcile: bool = False,
    ) -> dict[str, Any]:
        """Move eligible tasks -- all of them, or the named ones -- into the archive.

        Files are written and read back before the live records leave the
        document, under the state lock; a write that fails leaves the live
        document exactly as it was.
        """
        if not dry_run:
            # Archiving is maintenance of the commander's own records.
            self.authority("archiving settled task records")
        with self.store.locked() as data:
            # A dry run reports and writes nothing, reconciliation included:
            # it works on a copy so the lock has nothing to save.
            working = copy.deepcopy(data) if dry_run else data
            eligible = self.archivable_task_ids(working, reconcile=reconcile)
            if task_ids is not None:
                wanted = set(task_ids)
                unknown = sorted(t for t in wanted if t not in data.get("tasks", {}))
                if unknown:
                    raise HelmError(f"unknown task(s): {', '.join(unknown)}")
                refused = sorted(t for t in wanted if t not in eligible)
                eligible = [t for t in eligible if t in wanted]
            else:
                refused = []
            if dry_run:
                return {"archived": [], "eligible": eligible, "refused": refused, "dry_run": True}
            if not eligible:
                return {"archived": [], "eligible": [], "refused": refused, "dry_run": False}
            tasks = data["tasks"]
            reviewers = {
                task_id: sorted(
                    other_id for other_id, other in tasks.items() if other.get("reviews") == task_id
                )
                for task_id in eligible
            }
            records = archive.extract_tasks(data, eligible, archived_at=now())
            for task_id, record in records.items():
                record["reviewer_task_ids"] = reviewers.get(task_id, [])
                archive.write_task(self.store.directory, record)
            return {"archived": eligible, "eligible": eligible, "refused": refused, "dry_run": False}

    def archived_task(self, task_id: str) -> dict[str, Any] | None:
        return archive.read_task(self.store.directory, task_id)

    def _task_anywhere(self, data: dict[str, Any], task_id: str) -> dict[str, Any]:
        """A live task, or the archived record's task; unknown otherwise."""
        task = data.get("tasks", {}).get(task_id)
        if task is not None:
            return task
        record = self.archived_task(task_id)
        if record is None:
            raise HelmError(f"unknown task: {task_id}")
        return record["task"]

    # ---------- measuring ----------

    def state_stats(self) -> dict[str, Any]:
        data = self.store.load()
        by_status: dict[str, int] = {}
        for task in data.get("tasks", {}).values():
            by_status[str(task.get("status"))] = by_status.get(str(task.get("status")), 0) + 1
        size = 0
        with contextlib.suppress(OSError):
            size = self.store.state_file.stat().st_size
        files, archive_bytes = archive.archive_size(self.store.directory)
        return {
            "state_file": str(self.store.state_file),
            "bytes": size,
            "projects": len(data.get("projects", {})),
            "tasks": len(data.get("tasks", {})),
            "tasks_by_status": dict(sorted(by_status.items())),
            "workers": len(data.get("workers", {})),
            "messages": len(data.get("messages", [])),
            "artifacts": len(data.get("artifacts", [])),
            "archivable": len(self.archivable_task_ids(data)),
            "archive_files": files,
            "archive_bytes": archive_bytes,
        }

    # ---------- projects ----------

    def remove_project(self, project_id: str) -> dict[str, Any]:
        self.authority("removing a project")
        """Forget a project whose work is over, archiving what it still holds.

        Refused while its directory is still a direct child of `projects/`
        -- discovery would register it again on the next command -- while a
        worker of it is running, or while any of its tasks cannot be
        archived. The project record and its project-level messages go to
        `archive/projects/<id>.json`; its status directory moves beside them.
        """
        data = self.store.load()
        project = self._project(data, project_id)
        configured = self.store.configured_root()
        if configured is not None and (configured / "projects" / project_id).exists():
            raise SafetyError(
                f"projects/{project_id} still exists; move or delete the directory first, "
                "or discovery registers the project again on the next command"
            )
        running = sorted(
            worker["id"]
            for worker in data.get("workers", {}).values()
            if worker.get("project_id") == project_id and worker.get("status") == "running"
        )
        if running:
            raise SafetyError(
                f"{project_id} still has running worker(s): {', '.join(running)}; stop them first"
            )
        owned = sorted(t for t, task in data.get("tasks", {}).items() if task.get("project_id") == project_id)
        root_missing = not Path(project.get("root") or "").exists()
        if root_missing and owned:
            # The repository itself is gone, so nothing its tasks claim on
            # disk -- a worktree under state/, a branch in that repository --
            # can still be there to shed. The records are reconciled here,
            # said so on the record, and their open asks answered by the
            # removal, exactly as cleanup would have answered them.
            with self.store.locked() as live:
                for task_id in owned:
                    task = live["tasks"].get(task_id)
                    if task is None:
                        continue
                    if not task.get("workspace_removed") and not Path(task.get("workspace") or "").exists():
                        task["workspace_removed"] = True
                        task["workspace_removed_at"] = now()
                    if task_owns_branch(task) and not task.get("branch_removed"):
                        task["branch_removed"] = True
                        task["branch_removed_at"] = now()
                    task["reconciled_by"] = "project remove: repository root missing"
                    # Worker directories are Helm's own residue under state/,
                    # shed the way cleanup sheds them.
                    with contextlib.suppress(HelmError, OSError):
                        self._remove_worker_directories_locked(live, task)
                open_asks = [
                    message for message in self.open_escalations(project_id)
                    if message.get("task_id") in owned
                ]
                for message in open_asks:
                    task = live["tasks"].get(message.get("task_id"))
                    worker = live["workers"].get(message.get("worker_id"))
                    if task is None:
                        continue
                    self._message(
                        live, project, task, worker, "answer",
                        "Resolved by project removal: the commander forgot this project, "
                        "so its escalation is no longer awaiting an answer.",
                        {"source": "project-remove"},
                    )
        archived = self.archive_tasks(owned, reconcile=True)["archived"] if owned else []
        remaining = sorted(set(owned) - set(archived))
        if remaining:
            shown = ", ".join(remaining[:5]) + (" …" if len(remaining) > 5 else "")
            raise SafetyError(
                f"{project_id} still has {len(remaining)} task(s) that cannot be archived "
                f"({shown}): clean them up, or settle what they are waiting on, first"
            )
        with self.store.locked() as live:
            project = live["projects"].pop(project_id, None)
            if project is None:
                raise HelmError(f"unknown project: {project_id}")
            messages = [m for m in live["messages"] if m.get("project_id") == project_id]
            live["messages"] = [m for m in live["messages"] if m.get("project_id") != project_id]
            artifacts = [a for a in live.get("artifacts", []) if a.get("project_id") == project_id]
            live["artifacts"] = [a for a in live.get("artifacts", []) if a.get("project_id") != project_id]
            herdr = live.get("integrations", {}).get("herdr", {})
            space = None
            if isinstance(herdr.get("projects"), dict):
                space = herdr["projects"].pop(project_id, None)
            record = {
                "version": 1,
                "archived_at": now(),
                "project": project,
                "messages": messages,
                "artifacts": artifacts,
                "herdr": space,
                "tasks": archived,
            }
            archive.write_project(self.store.directory, record)
        status_dir = self.store.directory / "projects" / project_id
        moved = None
        if status_dir.is_dir():
            target = archive.projects_dir(self.store.directory) / project_id
            with contextlib.suppress(OSError, shutil.Error):
                shutil.move(str(status_dir), str(target))
                moved = str(target)
        return {"project_id": project_id, "archived_tasks": archived, "status_moved_to": moved}
