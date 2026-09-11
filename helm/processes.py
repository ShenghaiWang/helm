"""Reading the process table: ancestry and the pid of a worker Helm did not fork."""

from __future__ import annotations

import subprocess
from pathlib import Path


def _process_parents(pid: int) -> list[int]:
    """The pid chain above `pid`, oldest last, or [] when it cannot be read.

    Ancestry is the part of an agent's identity it cannot edit. A worker can
    unset a variable; it cannot make itself not be the child of the runner Helm
    started for it.
    """
    try:
        listing = subprocess.run(
            ["ps", "-Ao", "pid=,ppid="],
            check=True,
            text=True,
            capture_output=True,
            timeout=10,
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return []
    parents: dict[int, int] = {}
    for line in listing.splitlines():
        fields = line.split()
        if len(fields) >= 2 and fields[0].isdigit() and fields[1].isdigit():
            parents[int(fields[0])] = int(fields[1])
    chain: list[int] = []
    current = parents.get(pid)
    seen: set[int] = set()
    while current and current > 1 and current not in seen:
        seen.add(current)
        chain.append(current)
        current = parents.get(current)
    return chain


def _scan_worker_pid(worker_id: str, state_dir: Path) -> int | None:
    """Find the running process of a worker Helm did not fork itself.

    A Herdr-launched worker starts inside a pane, so Helm never holds its pid
    and the record keeps `pid: None` -- which makes `_pid_alive` answer "dead"
    for every one of them. Liveness then rests entirely on asking the provider
    whether the *pane* still exists, and a pane is not a process. Closing one
    left an agent running that Helm had already recorded as failed: it could
    no longer be addressed, `worker answer` refused it as gone, and `worker
    stop` had nothing to stop. The process had to be killed by hand.

    The match is the worker's own state path, not its bare id and not the
    executable name. Every runtime is handed `state/workers/<id>/context.json`
    in its prompt or as an `--add-dir`, so the path is in the agent's argv --
    while `helm worker answer <id>` carries only the bare id, and matching that
    would let a coordinator command masquerade as its own worker.

    Best-effort by design: an unreadable process table returns None and leaves
    the caller exactly where it was before. This narrows a blind spot; it does
    not become a second source of truth.
    """
    marker = str(Path(state_dir) / "workers" / worker_id)
    try:
        listing = subprocess.run(
            ["ps", "-Ao", "pid=,command="],
            check=True,
            text=True,
            capture_output=True,
            timeout=10,
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return None
    for line in listing.splitlines():
        pid_text, _, command = line.strip().partition(" ")
        if not pid_text.isdigit() or marker not in command:
            continue
        # The scan runs from a coordinator that may itself be inspecting this
        # worker; its own `ps`/`grep` line contains the marker too.
        if any(tool in command for tool in (" grep ", "ps -Ao", "/ps ")):
            continue
        return int(pid_text)
    return None
