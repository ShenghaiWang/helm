"""Path canonicalisation, containment checks and Helm-private file helpers."""
from __future__ import annotations

import contextlib
import hashlib
import os
import tempfile
from pathlib import Path

from .errors import SafetyError


def canonical(path: str | os.PathLike[str]) -> Path:
    return Path(path).expanduser().resolve(strict=False)


def _private_dir(path: Path) -> Path:
    """Create/tighten a Helm-private directory without relying on umask."""
    if path.is_symlink():
        raise SafetyError(f"Helm-private directory must not be a symlink: {path}")
    path.mkdir(parents=True, exist_ok=True)
    os.chmod(path, 0o700)
    return path


def _private_file(path: Path) -> Path:
    """Tighten a retained private file and reject path substitution."""
    if path.is_symlink():
        raise SafetyError(f"Helm-private file must not be a symlink: {path}")
    if path.exists():
        os.chmod(path, 0o600)
    return path


def _write_private_text(path: Path, content: str) -> None:
    """Write a Helm-private file so a reader never sees it half-written.

    Truncate-then-write left a window in which the file existed and was empty,
    and something does read these while they are being written: `poll_worker`
    treats the existence of `exit.json` as "the worker finished" and its
    contents as the exit code. Landing in that window parsed nothing, took the
    unreadable-record branch, and recorded a healthy worker as *failed* --
    which fails its task, emits a failure message, and keeps the project's
    Herdr space open on work that actually succeeded.

    So the content is written to a temporary file in the same directory and
    moved into place with `os.replace`, which is atomic on one filesystem: a
    reader sees either the previous file or the complete new one, never a
    partial one.
    """
    _private_file(path)
    directory = path.parent
    fd, temporary = tempfile.mkstemp(dir=str(directory), prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", errors="replace") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(temporary)
        raise


def inside(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
        return True
    except ValueError:
        return False


def overlaps(a: Path, b: Path) -> bool:
    return inside(a, b) or inside(b, a)


def _file_digest(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _safe_configuration_path(path: Path, allowed_root: Path, label: str) -> Path:
    """Resolve configuration only when it remains inside its owner root."""
    resolved = path.resolve(strict=False)
    if not inside(resolved, canonical(allowed_root)):
        raise SafetyError(f"{label} resolves outside its allowed root: {path}")
    return resolved


#: The directory that must be on `PYTHONPATH` for `import helm` to work --
#: the parent of the installed package, derived from the package itself.
#: Computed here rather than from any one module's `__file__`, because
#: `Path(__file__).parent.parent` silently means a different directory once
#: the file holding it moves one level down, and the failure it produces is
#: "No module named helm" in a subprocess that has already been launched.
def package_parent() -> Path:
    import helm

    return canonical(Path(helm.__file__).resolve().parent.parent)



#: The two files that make a turns worker's queue and runner safe to share
#: between processes. The queue (`next.json`) is written by whichever Helm
#: command has something to say and consumed by the runner; the runner lock is
#: held by the one runner alive for that worker, for as long as it lives.
TURN_QUEUE_LOCK = "queue.lock"
TURN_RUNNER_LOCK = "runner.lock"


@contextlib.contextmanager
def turn_queue_lock(turns_dir: Path):
    """Hold the exclusive lock on one turns worker's prompt queue.

    `next.json` is appended to by a read-modify-write and consumed by a
    read-then-unlink. Unlocked, a prompt written between the runner's read and
    its unlink is deleted unread -- and the sender has already been told it is
    queued. Both sides take this lock, so a prompt is either in the read the
    runner acts on or still in the file afterwards.
    """
    import fcntl

    turns_dir.mkdir(parents=True, exist_ok=True)
    fd = os.open(turns_dir / TURN_QUEUE_LOCK, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        with contextlib.suppress(OSError):
            fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def hold_turns_runner_lock(turns_dir: Path) -> int | None:
    """Take the runner lock for life; the open fd, or None when a runner holds it.

    The lock, not a pid, is what says a runner is alive: the kernel releases it
    the instant the process ends, however it ends, and no other process can be
    mistaken for it -- where a recorded pid outlives its runner and can name
    whatever the system hands that number to next. It also makes a second
    runner for the same worker refuse to start rather than run turns beside
    the first.
    """
    import fcntl
    import time

    turns_dir.mkdir(parents=True, exist_ok=True)
    fd = os.open(turns_dir / TURN_RUNNER_LOCK, os.O_RDWR | os.O_CREAT, 0o600)
    # A few tries, because `turns_runner_lock_held` probes by taking the lock
    # for an instant: a runner starting in that instant must not read the
    # probe as a rival and leave.
    for _attempt in range(20):
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return fd
        except OSError:
            time.sleep(0.05)
    os.close(fd)
    return None


def turns_runner_lock_held(turns_dir: Path) -> bool | None:
    """Whether a live runner holds this worker's runner lock.

    None when there is no lock file at all -- a runner from before the lock
    existed, or none ever started -- so the caller can fall back to what it
    knew before rather than read "no evidence" as "dead".
    """
    import fcntl

    path = turns_dir / TURN_RUNNER_LOCK
    try:
        fd = os.open(path, os.O_RDWR)
    except FileNotFoundError:
        return None
    except OSError:
        return None
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        return True
    else:
        fcntl.flock(fd, fcntl.LOCK_UN)
        return False
    finally:
        os.close(fd)
