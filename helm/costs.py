"""What a worker's session cost, read from the runtime's own transcript.

Helm does not meter tokens itself -- it cannot see inside an agent's session
-- but Claude Code writes every assistant turn's usage into a transcript under
``~/.claude/projects/<cwd slug>/<session id>.jsonl``. That file is the
runtime's own record, so it is the evidence, not an estimate. Other runtimes
keep no such record Helm knows how to read, and a worker on one of them
reports no usage rather than a guess.

Tokens only. A price is a dated catalogue fact and does not belong in tracked
code; a run that carried its own ``total_cost_usd`` (Claude Code's print mode
reports one) is recorded as such and summed separately.
"""

from __future__ import annotations

import datetime as _dt
import json
import os
import re
from pathlib import Path
from typing import Any

USAGE_FIELDS = (
    "input_tokens",
    "output_tokens",
    "cache_read_input_tokens",
    "cache_creation_input_tokens",
)


def transcript_root() -> Path:
    override = os.environ.get("CLAUDE_CONFIG_DIR", "").strip()
    base = Path(override) if override else Path.home() / ".claude"
    return base / "projects"


def cwd_slug(cwd: str | Path) -> str:
    """Claude Code names a project's transcript directory after its cwd."""
    return re.sub(r"[^A-Za-z0-9]", "-", str(Path(cwd)))


def transcript_dir(cwd: str | Path) -> Path:
    return transcript_root() / cwd_slug(cwd)


def _parse_stamp(value: Any) -> float | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        return _dt.datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def transcript_usage(path: Path) -> dict[str, Any]:
    """Sum one transcript's assistant turns. Missing or unreadable is zero, said."""
    totals: dict[str, Any] = {field: 0 for field in USAGE_FIELDS}
    totals.update({
        "path": str(path),
        "turns": 0,
        "models": [],
        "session_id": None,
        "first_at": None,
        "last_at": None,
        "readable": True,
    })
    models: set[str] = set()
    try:
        with path.open(encoding="utf-8", errors="replace") as handle:
            for line in handle:
                try:
                    record = json.loads(line)
                except ValueError:
                    continue
                if not isinstance(record, dict):
                    continue
                if totals["session_id"] is None and isinstance(record.get("sessionId"), str):
                    totals["session_id"] = record["sessionId"]
                message = record.get("message")
                usage = message.get("usage") if isinstance(message, dict) else None
                if not isinstance(usage, dict):
                    continue
                totals["turns"] += 1
                for field in USAGE_FIELDS:
                    value = usage.get(field)
                    if isinstance(value, (int, float)):
                        totals[field] += int(value)
                model = message.get("model") if isinstance(message, dict) else None
                if isinstance(model, str) and model:
                    models.add(model)
                stamp = _parse_stamp(record.get("timestamp"))
                if stamp is not None:
                    totals["first_at"] = stamp if totals["first_at"] is None else min(totals["first_at"], stamp)
                    totals["last_at"] = stamp if totals["last_at"] is None else max(totals["last_at"], stamp)
    except OSError:
        totals["readable"] = False
    totals["models"] = sorted(models)
    return totals


def find_transcripts(
    cwd: str | Path, *, session_id: str | None = None, since: float | None = None
) -> list[Path]:
    """The transcripts a session in this cwd could have written.

    A known session id names exactly one file. Without one, every transcript
    in the cwd's directory modified at or after ``since`` is a candidate --
    a worktree is used by one worker at a time, so that is usually one file.
    """
    directory = transcript_dir(cwd)
    if session_id:
        candidate = directory / f"{session_id}.jsonl"
        return [candidate] if candidate.is_file() else []
    if not directory.is_dir():
        return []
    found = []
    for path in directory.glob("*.jsonl"):
        try:
            if since is not None and path.stat().st_mtime < since:
                continue
        except OSError:
            continue
        found.append(path)
    return sorted(found)


def worker_transcripts(worker: dict[str, Any]) -> list[Path]:
    """The transcripts a worker's own session wrote, by its cwd and session id."""
    if (worker.get("agent_id") or worker.get("agent")) != "claude":
        return []
    since = _parse_stamp(worker.get("started_at"))
    if since is not None:
        # Clock skew between the launch record and the transcript's first write
        # is seconds; a minute of slack costs nothing and loses nothing.
        since -= 60
    return find_transcripts(
        worker.get("workspace") or "", session_id=worker.get("agent_session_id"), since=since
    )


def worker_usage(worker: dict[str, Any]) -> dict[str, Any]:
    """A worker's token usage from its runtime's transcript, or an honest blank."""
    result: dict[str, Any] = {
        "worker_id": worker.get("id"),
        "agent": worker.get("agent_id") or worker.get("agent"),
        "transcripts": [],
        "turns": 0,
        "models": [],
        "cost_usd": worker.get("cost_usd"),
        "metered": False,
    }
    for field in USAGE_FIELDS:
        result[field] = 0
    if (worker.get("agent_id") or worker.get("agent")) != "claude":
        return result
    paths = worker_transcripts(worker)
    models: set[str] = set()
    for path in paths:
        usage = transcript_usage(path)
        result["transcripts"].append(usage)
        result["turns"] += usage["turns"]
        for field in USAGE_FIELDS:
            result[field] += usage[field]
        models.update(usage["models"])
    result["models"] = sorted(models)
    result["metered"] = bool(paths)
    return result


def sum_usage(entries: list[dict[str, Any]]) -> dict[str, Any]:
    total: dict[str, Any] = {field: 0 for field in USAGE_FIELDS}
    total["turns"] = 0
    total["cost_usd"] = 0.0
    total["cost_known"] = False
    models: set[str] = set()
    for entry in entries:
        for field in USAGE_FIELDS:
            total[field] += int(entry.get(field) or 0)
        total["turns"] += int(entry.get("turns") or 0)
        cost = entry.get("cost_usd")
        if isinstance(cost, (int, float)):
            total["cost_usd"] += float(cost)
            total["cost_known"] = True
        models.update(entry.get("models") or [])
    total["models"] = sorted(models)
    return total
