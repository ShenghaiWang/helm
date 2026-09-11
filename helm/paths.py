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

