"""Archived records: what leaves the live state document, and how it is read back.

The live document is read and rewritten by every command, so it can only
stay fast while it holds what can still change. A task that has been
cleaned up -- worktree, branch and worker directories all recorded as gone,
no session running -- can never change again, and neither can its workers,
its messages or its artifacts. Those records move here, one file per task,
where `helm inspect` and `helm task cost` still find them and nothing else
has to read them.

Nothing is deleted: an archive file is written and verified before its
records leave the live document, and a record whose task might still be
acted on -- an open escalation, a branch still held, a session still alive
-- is never eligible.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from .paths import _private_dir, _write_private_text

ARCHIVE_DIRNAME = "archive"

#: Task states in which a record can still be acted on by a human: the work
#: is waiting on a decision, so its record must stay where decisions are
#: read from, whatever cleanup has or has not happened.
UNDECIDED_TASK_STATES = frozenset({"approval-needed", "approved", "pr-open"})


def tasks_dir(state_dir: Path) -> Path:
    return Path(state_dir) / ARCHIVE_DIRNAME / "tasks"


def projects_dir(state_dir: Path) -> Path:
    return Path(state_dir) / ARCHIVE_DIRNAME / "projects"


def task_file(state_dir: Path, task_id: str) -> Path:
    return tasks_dir(state_dir) / f"{task_id}.json"


def project_file(state_dir: Path, project_id: str) -> Path:
    return projects_dir(state_dir) / f"{project_id}.json"


def extract_task(data: dict[str, Any], task_id: str, *, archived_at: str) -> dict[str, Any]:
    """Remove one task and everything keyed to it from the live document.

    Returns the archive record. The caller writes it; this only moves.
    """
    task = data["tasks"].pop(task_id)
    workers = {
        worker_id: worker
        for worker_id, worker in data["workers"].items()
        if worker.get("task_id") == task_id
    }
    for worker_id in workers:
        del data["workers"][worker_id]
    messages = [m for m in data["messages"] if m.get("task_id") == task_id]
    data["messages"] = [m for m in data["messages"] if m.get("task_id") != task_id]
    artifacts = [a for a in data.get("artifacts", []) if a.get("task_id") == task_id]
    data["artifacts"] = [a for a in data.get("artifacts", []) if a.get("task_id") != task_id]
    herdr = data.get("integrations", {}).get("herdr", {})
    for worker_id in workers:
        if isinstance(herdr.get("workers"), dict):
            herdr["workers"].pop(worker_id, None)
    return {
        "version": 1,
        "archived_at": archived_at,
        "task": task,
        "workers": workers,
        "messages": messages,
        "artifacts": artifacts,
    }


def extract_tasks(data: dict[str, Any], task_ids: list[str], *, archived_at: str) -> dict[str, dict[str, Any]]:
    """`extract_task` for many ids in one pass over the message list.

    The message list is the large collection, and removing tasks one at a
    time walks it once per task; a backfill of a thousand tasks would walk
    it a thousand times.
    """
    wanted = set(task_ids)
    records: dict[str, dict[str, Any]] = {}
    for task_id in task_ids:
        records[task_id] = {
            "version": 1,
            "archived_at": archived_at,
            "task": data["tasks"].pop(task_id),
            "workers": {},
            "messages": [],
            "artifacts": [],
        }
    for worker_id in [w for w, worker in data["workers"].items() if worker.get("task_id") in wanted]:
        worker = data["workers"].pop(worker_id)
        records[worker["task_id"]]["workers"][worker_id] = worker
        herdr = data.get("integrations", {}).get("herdr", {})
        if isinstance(herdr.get("workers"), dict):
            herdr["workers"].pop(worker_id, None)
    kept_messages = []
    for message in data["messages"]:
        target = message.get("task_id")
        if target in wanted:
            records[target]["messages"].append(message)
        else:
            kept_messages.append(message)
    data["messages"] = kept_messages
    kept_artifacts = []
    for artifact in data.get("artifacts", []):
        target = artifact.get("task_id")
        if target in wanted:
            records[target]["artifacts"].append(artifact)
        else:
            kept_artifacts.append(artifact)
    data["artifacts"] = kept_artifacts
    return records


def write_task(state_dir: Path, record: dict[str, Any]) -> Path:
    """Write one task's archive file, privately and atomically, and read it
    back before reporting success -- the live records are dropped only after
    this returns."""
    directory = tasks_dir(state_dir)
    _private_dir(Path(state_dir) / ARCHIVE_DIRNAME)
    _private_dir(directory)
    path = directory / f"{record['task']['id']}.json"
    text = json.dumps(record, indent=2, sort_keys=True) + "\n"
    _write_private_text(path, text)
    if json.loads(path.read_text(encoding="utf-8"))["task"]["id"] != record["task"]["id"]:
        raise OSError(f"archive file did not read back: {path}")
    return path


def write_project(state_dir: Path, record: dict[str, Any]) -> Path:
    directory = projects_dir(state_dir)
    _private_dir(Path(state_dir) / ARCHIVE_DIRNAME)
    _private_dir(directory)
    path = directory / f"{record['project']['id']}.json"
    _write_private_text(path, json.dumps(record, indent=2, sort_keys=True) + "\n")
    return path


def read_task(state_dir: Path, task_id: str) -> dict[str, Any] | None:
    path = task_file(state_dir, task_id)
    if not path.is_file():
        return None
    try:
        record = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return record if isinstance(record, dict) and isinstance(record.get("task"), dict) else None


def archived_task_ids(state_dir: Path, *, modified_since: float | None = None) -> list[str]:
    directory = tasks_dir(state_dir)
    if not directory.is_dir():
        return []
    found = []
    for path in directory.glob("t-*.json"):
        if modified_since is not None:
            try:
                if path.stat().st_mtime < modified_since:
                    continue
            except OSError:
                continue
        found.append(path.stem)
    return sorted(found)


def archive_size(state_dir: Path) -> tuple[int, int]:
    """(files, bytes) under the archive."""
    root = Path(state_dir) / ARCHIVE_DIRNAME
    if not root.is_dir():
        return 0, 0
    files = 0
    total = 0
    for dirpath, _dirs, names in os.walk(root):
        for name in names:
            files += 1
            try:
                total += (Path(dirpath) / name).stat().st_size
            except OSError:
                continue
    return files, total
