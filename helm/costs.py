"""What a worker's session cost, read from the runtime's own transcript.

Helm does not meter tokens itself -- it cannot see inside an agent's session
-- but Claude Code writes every assistant turn's usage into a transcript under
``~/.claude/projects/<cwd slug>/<session id>.jsonl``. That file is the
runtime's own record, so it is the evidence, not an estimate. Other runtimes
keep no such record Helm knows how to read, and a worker on one of them
reports no usage rather than a guess.

Tokens first. A price is a dated catalogue fact and does not belong in tracked
code, so dollars come from two places only: a run that carried its own
``total_cost_usd`` (Claude Code's print mode reports one) is recorded as such,
and otherwise the root's own ``model.prices`` preference prices the tokens per
model. A model with no price, or cache tokens with no cache rate, leaves the
figure blank rather than guessed.
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
        "by_model": {},
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
                model = message.get("model") if isinstance(message, dict) else None
                model = model if isinstance(model, str) and model else ""
                bucket = totals["by_model"].setdefault(model, {field: 0 for field in USAGE_FIELDS})
                for field in USAGE_FIELDS:
                    value = usage.get(field)
                    if isinstance(value, (int, float)):
                        totals[field] += int(value)
                        bucket[field] += int(value)
                if model:
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
        "cost_source": "reported" if isinstance(worker.get("cost_usd"), (int, float)) else None,
        "by_model": {},
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
        _merge_by_model(result["by_model"], usage.get("by_model", {}))
        models.update(usage["models"])
    result["models"] = sorted(models)
    result["metered"] = bool(paths)
    return result


def _merge_by_model(into: dict[str, dict[str, int]], more: dict[str, dict[str, int]]) -> None:
    for model, tokens in more.items():
        bucket = into.setdefault(model, {field: 0 for field in USAGE_FIELDS})
        for field in USAGE_FIELDS:
            bucket[field] += int(tokens.get(field) or 0)


def price_usage(by_model: dict[str, dict[str, int]], price_for) -> dict[str, Any]:
    """Dollars for tokens, per model, from the root's own price list.

    Honest about its gaps: the figure is given only when every model that
    spent tokens has a price, and every kind of token it spent has a rate.
    A partial sum would read as the whole cost of the task.
    """
    total = 0.0
    priced: list[str] = []
    unpriced: list[str] = []
    for model, tokens in sorted(by_model.items()):
        spent = {field: int(tokens.get(field) or 0) for field in USAGE_FIELDS}
        if not any(spent.values()):
            continue
        rates = price_for(model) if model else None
        if rates is None:
            unpriced.append(model or "unknown model")
            continue
        needed = {
            "input_tokens": "in",
            "output_tokens": "out",
            "cache_read_input_tokens": "cache_read",
            "cache_creation_input_tokens": "cache_write",
        }
        missing = [name for field, name in needed.items() if spent[field] and name not in rates]
        if missing:
            unpriced.append(f"{model} (no {', '.join(missing)} rate)")
            continue
        total += sum(spent[field] * rates.get(name, 0.0) / 1_000_000 for field, name in needed.items())
        priced.append(model)
    return {
        "cost_usd": round(total, 4) if priced and not unpriced else None,
        "priced": priced,
        "unpriced": unpriced,
    }


def sum_usage(entries: list[dict[str, Any]]) -> dict[str, Any]:
    total: dict[str, Any] = {field: 0 for field in USAGE_FIELDS}
    total["turns"] = 0
    total["cost_usd"] = 0.0
    total["cost_known"] = False
    total["by_model"] = {}
    total["priced"] = 0
    total["unpriced_models"] = []
    models: set[str] = set()
    unpriced: set[str] = set()
    for entry in entries:
        for field in USAGE_FIELDS:
            total[field] += int(entry.get(field) or 0)
        total["turns"] += int(entry.get("turns") or 0)
        _merge_by_model(total["by_model"], entry.get("by_model", {}))
        cost = entry.get("cost_usd")
        if isinstance(cost, (int, float)):
            total["cost_usd"] += float(cost)
            total["cost_known"] = True
            if entry.get("cost_source") == "priced":
                total["priced"] += 1
        unpriced.update(entry.get("unpriced_models") or [])
        models.update(entry.get("models") or [])
    total["models"] = sorted(models)
    total["unpriced_models"] = sorted(unpriced)
    total["cost_usd"] = round(total["cost_usd"], 4)
    return total
