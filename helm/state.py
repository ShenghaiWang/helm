"""The on-disk Helm state document: its schema version, migration and store."""
from __future__ import annotations

import contextlib
import copy
import fcntl
import json
import os
import tempfile
import time
from pathlib import Path
from typing import Any, Iterator

from .errors import HelmError, SafetyError
from .paths import _private_dir, _private_file, canonical


SCHEMA_VERSION = 2
#: Versions this build can open. An older document is migrated on load rather
#: than refused: the state that most needs repairing is the state written by
#: the build with the bug in it.
SUPPORTED_SCHEMA_VERSIONS = (1, 2)


def _migrate_state(data: dict[str, Any], version: int) -> dict[str, Any]:
    """Bring an older state document up to this build's schema, conservatively.

    v1 -> v2 moves each task's single `hold` into the `holds` history and
    downgrades any authorization it carried. A v1 `authorized` hold cannot say
    whether it was ever delivered, consumed, or acted on, and the safe reading
    of an unknown authorization is that it is not one: it becomes `invalidated`,
    so the commander is asked again rather than a stale agreement being spent.
    """
    if version >= 2:
        return data
    downgrade = {
        "waiting": "waiting",
        "authorized": "invalidated",
        "closed": "closed",
        "invalidated": "invalidated",
        "abandoned": "abandoned",
    }
    for task in data.get("tasks", {}).values():
        if not isinstance(task, dict):
            continue
        holds = task.get("holds")
        if not isinstance(holds, list):
            holds = []
        legacy = task.pop("hold", None)
        if isinstance(legacy, dict) and legacy.get("id"):
            legacy["status"] = downgrade.get(str(legacy.get("status")), "invalidated")
            legacy["migrated_from_schema"] = version
            if not any(
                isinstance(entry, dict) and entry.get("id") == legacy["id"] for entry in holds
            ):
                holds.append(legacy)
        task["holds"] = holds
    data["version"] = SCHEMA_VERSION
    return data


class StateStore:
    """A tiny JSON store with a process lock and atomic replacement."""

    def __init__(
        self,
        state_dir: str | os.PathLike[str] | None = None,
        *,
        helm_root: str | os.PathLike[str] | None = None,
        read_only: bool = False,
    ):
        configured = state_dir or os.environ.get("HELM_STATE_DIR") or "~/.helm"
        requested_directory = Path(configured).expanduser()
        if requested_directory.is_symlink():
            raise SafetyError(f"Helm state directory must not be a symlink: {requested_directory}")
        self.directory = canonical(configured)
        self._helm_root = canonical(helm_root) if helm_root else None
        self.state_file = self.directory / "state.json"
        self.lock_file = self.directory / ".lock"
        #: A store that must not change the root it is opened against. Opening
        #: a store normally *repairs* it -- the permissions below are tightened
        #: on the way in -- which is right for a command about to write, and
        #: wrong for one whose whole contract is that it changes nothing. A
        #: read-only store performs no repair and refuses every write, so
        #: "inspect without touching" is a property of the object rather than a
        #: promise each caller has to keep.
        self.read_only = read_only
        #: One parsed document, keyed by the identity of the file it came from.
        #: A mature root's state is tens of megabytes and one command reaches
        #: `load` dozens of times, so the parse -- not the work -- was most of
        #: what every read-only command cost. The key is the file's identity
        #: and not a timestamp alone: inode, size and nanosecond mtime together,
        #: so a rewrite of the same length still misses. Callers mutate what
        #: they are handed, so a hit is still copied; that copy is what makes
        #: reusing the parse safe, and it is a third of the price of redoing it.
        self._cached_key: tuple[Any, ...] | None = None
        self._cached_document: dict[str, Any] | None = None
        self._validate_open()
        #: The permission bits found on the way in. A read-only store leaves
        #: them exactly as found; an ordinary one repairs them immediately
        #: below, so anything wanting to report on the *found* state has to
        #: read it here rather than off the disk afterwards.
        self.opened_modes: dict[str, int] = {}
        if self.directory.exists():
            for path in (self.directory, self.state_file, self.lock_file):
                with contextlib.suppress(OSError):
                    if path.exists() and not path.is_symlink():
                        self.opened_modes[str(path)] = path.stat().st_mode & 0o777
            if not read_only:
                _private_dir(self.directory)
                _private_file(self.state_file)
                _private_file(self.lock_file)

    @staticmethod
    def empty() -> dict[str, Any]:
        return {
            "version": SCHEMA_VERSION,
            "projects": {},
            "tasks": {},
            "workers": {},
            "messages": [],
            "artifacts": [],
            "learning_proposals": [],
            # Standing approvals a human granted in advance.  Helm-owned
            # state, never a project file: a project's own files are untrusted
            # guidance and must never be able to authorize a protected action.
            "approval_grants": {},
            "config": {},
            # Presentation adapters own this generic namespace. Helm core
            # never interprets provider IDs or talks to a presentation service.
            "integrations": {},
        }

    def _validate_paths(self) -> None:
        """The half of the open check that reads no state at all.

        Split out from `_validate_open` so `load` can run it, parse once, and
        check the parsed document -- rather than parsing the whole file here
        and again immediately afterwards. It stays first, and separate,
        because refusing a symlinked state file is only meaningful before
        anything has read through that symlink.
        """
        if self.directory.is_symlink():
            raise SafetyError(f"Helm state directory must not be a symlink: {self.directory}")
        if self.state_file.is_symlink() or self.lock_file.is_symlink():
            raise SafetyError("Helm state and lock files must not be symlinks")
        if self._helm_root is not None and self.directory != self._helm_root / "state":
            raise SafetyError(
                f"Helm root state must be {self._helm_root / 'state'} (got {self.directory})"
            )

    def _validate_document(self, raw: Any) -> None:
        """The half that needs the parsed document, given one already parsed."""
        if not isinstance(raw, dict):
            raise HelmError(f"unsupported or corrupt Helm state: {self.state_file}")
        configured = raw.get("config", {}).get("helm_root") if isinstance(raw.get("config"), dict) else None
        if not configured:
            return
        if not isinstance(configured, str):
            raise HelmError(f"invalid persisted Helm root in {self.state_file}")
        persisted_root = canonical(configured)
        if self.directory != persisted_root / "state":
            raise SafetyError(
                f"state directory does not match its persisted Helm root: {self.directory} vs {persisted_root / 'state'}"
            )
        if self._helm_root is not None and self._helm_root != persisted_root:
            raise SafetyError(
                f"state is already configured for a different Helm root: {configured}"
            )

    def _validate_open(self) -> None:
        """Enforce one immutable root/state namespace on every store open."""
        self._validate_paths()
        if not self.state_file.exists():
            return
        self._validate_document(self._read_document())

    def _read_document(self) -> Any:
        """Parse the state file, or refuse in the one shape every reader here uses."""
        try:
            return json.loads(self.state_file.read_text(encoding="utf-8"))
        # `UnicodeDecodeError` is raised by the decode, before json ever sees
        # the bytes, so a file that is merely not UTF-8 used to escape every
        # handler here as a traceback rather than the "cannot read" this
        # promises. Every JSON read in this module catches it for that reason.
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise HelmError(f"cannot read state {self.state_file}: {exc}") from exc

    def _document_identity(self) -> tuple[Any, ...] | None:
        """What makes one parse of the state file reusable for the next read.

        Deliberately not mtime alone. Two writes inside one filesystem
        timestamp tick are rare but not impossible, and the failure mode of
        guessing wrong here is serving a stale document to a command that then
        decides something on it. Inode catches a replaced file -- `save` writes
        a temporary and renames, so every write lands a new inode -- and size
        catches the same-tick rewrite that happens to change length.
        """
        try:
            info = self.state_file.stat()
        except OSError:
            return None
        return (info.st_ino, info.st_size, info.st_mtime_ns)

    def load(self, *, use_cache: bool = True) -> dict[str, Any]:
        # One parse, not two. This used to call `_validate_open`, which read
        # and parsed the whole file to check the persisted root, and then
        # parsed it a second time here for the caller. On a mature root that
        # document is tens of megabytes and a single command loads it dozens
        # of times, so the duplicate was the largest cost in every read-only
        # command Helm has. The checks are unchanged and still run in the same
        # order: paths first, before anything reads through a symlink.
        self._validate_paths()
        if not self.state_file.exists():
            return self.empty()
        identity = self._document_identity() if use_cache else None
        if (
            identity is not None
            and identity == self._cached_key
            and self._cached_document is not None
        ):
            return copy.deepcopy(self._cached_document)
        data = self._read_document()
        self._validate_document(data)
        if not isinstance(data, dict) or data.get("version") not in SUPPORTED_SCHEMA_VERSIONS:
            raise HelmError(f"unsupported or corrupt Helm state: {self.state_file}")
        for key in ("projects", "tasks", "workers", "messages", "artifacts", "learning_proposals"):
            data.setdefault(key, {} if key in {"projects", "tasks", "workers"} else [])
        data.setdefault("approval_grants", {})
        data.setdefault("config", {})
        data.setdefault("integrations", {})
        # Migration on read, so a root written by the build that had the bug
        # opens here instead of being refused as corrupt. The upgraded document
        # is persisted by the next save; reading alone changes nothing on disk.
        # A document can carry a supported version and still be unwalkable --
        # `tasks` as a list parses, passes the version check, and then raises
        # an AttributeError from whichever caller iterates it first. That is a
        # traceback where every other corrupt-state path gives a clear refusal,
        # so the containers are checked here, once, for everybody.
        for key in ("projects", "tasks", "workers", "config", "approval_grants",
                    "integrations"):
            if not isinstance(data.get(key), dict):
                raise HelmError(
                    f"unusable Helm state shape in {self.state_file}: "
                    f"{key} is not an object"
                )
        for key in ("messages", "artifacts", "learning_proposals"):
            if not isinstance(data.get(key), list):
                raise HelmError(
                    f"unusable Helm state shape in {self.state_file}: "
                    f"{key} is not a list"
                )
        version = int(data.get("version") or 1)
        if version != SCHEMA_VERSION:
            data = _migrate_state(data, version)
        for task in data.get("tasks", {}).values():
            if isinstance(task, dict) and not isinstance(task.get("holds"), list):
                task["holds"] = []
        # Cache the finished document -- migrated, defaulted and shape-checked
        # -- so a hit is indistinguishable from a fresh parse. The stored copy
        # is separate from the one handed back, because the caller owns what it
        # is given and several of them mutate it in place.
        if identity is not None:
            self._cached_key = identity
            self._cached_document = copy.deepcopy(data)
        return data

    def configured_root(self) -> Path | None:
        """Return the persisted Helm root, if this store belongs to one."""
        # Paths only: every path below that reads anything reaches `load`,
        # which runs the document half itself. Asking for the full open check
        # here parsed the file a third time to reach the same verdict. The
        # write paths -- `locked`, `save`, `initialize_root` -- deliberately
        # keep the full check, because they must refuse before they touch the
        # root at all, and taking the lock is already a write.
        self._validate_paths()
        if self._helm_root is not None:
            if self.state_file.exists():
                data = self.load()
                configured = data.get("config", {}).get("helm_root")
                if configured and canonical(configured) != self._helm_root:
                    raise SafetyError(
                        f"state is already configured for a different Helm root: {configured}"
                    )
            return self._helm_root
        if not self.state_file.exists():
            return self.directory.parent if self.directory.name == "state" else None
        data = self.load()
        raw_root = data.get("config", {}).get("helm_root")
        if isinstance(raw_root, str) and raw_root:
            return canonical(raw_root)
        return self.directory.parent if self.directory.name == "state" else None

    def initialize_root(self, root: str | os.PathLike[str]) -> Path:
        """Create the root layout while preserving existing projects and state."""
        self._refuse_if_read_only("initializing a root")
        helm_root = canonical(root)
        if self.directory != helm_root / "state":
            raise SafetyError(
                f"Helm root state must be {helm_root / 'state'} (got {self.directory})"
            )
        self._validate_open()
        if helm_root.exists() and not helm_root.is_dir():
            raise HelmError(f"Helm root is not a directory: {helm_root}")
        helm_root.mkdir(parents=True, exist_ok=True)
        _private_dir(helm_root / "state")
        for child in ("projects", "domains", "agents"):
            child_path = helm_root / child
            if child_path.exists() and child_path.is_symlink():
                raise SafetyError(f"Helm-owned directory must not be a symlink: {child_path}")
            child_path.mkdir(exist_ok=True)
        with self.locked() as data:
            configured = data.get("config", {}).get("helm_root")
            if configured and canonical(configured) != helm_root:
                raise SafetyError(
                    f"state is already configured for a different Helm root: {configured}"
                )
            data.setdefault("config", {})["helm_root"] = str(helm_root)
        self._helm_root = helm_root
        return helm_root

    #: A save that is interrupted between writing its temporary file and
    #: renaming it leaves the temporary behind, and nothing collected those --
    #: each is a near-complete copy of the state file, so they accumulate into
    #: real disk. Swept only once old enough that no live save could own one.
    _ORPHAN_TEMP_AGE_SECONDS = 3600.0

    def _sweep_orphan_temporaries(self) -> None:
        cutoff = time.time() - self._ORPHAN_TEMP_AGE_SECONDS
        with contextlib.suppress(OSError):
            for candidate in self.directory.glob("state.*.tmp"):
                with contextlib.suppress(OSError):
                    if candidate.stat().st_mtime < cutoff:
                        candidate.unlink()

    def _refuse_if_read_only(self, operation: str) -> None:
        """A read-only store fails loudly rather than quietly writing.

        Enforced here, not left to each caller, because "this command does not
        write" is exactly the kind of claim that stays true only until someone
        adds a line to it.
        """
        if self.read_only:
            raise SafetyError(
                f"this Helm state store was opened read-only; {operation} is refused"
            )

    def save(self, data: dict[str, Any]) -> None:
        self._refuse_if_read_only("saving state")
        self._validate_open()
        _private_dir(self.directory)
        _private_file(self.state_file)
        self._sweep_orphan_temporaries()
        fd, temporary = tempfile.mkstemp(prefix="state.", suffix=".tmp", dir=self.directory)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                json.dump(data, stream, indent=2, sort_keys=True)
                stream.write("\n")
                stream.flush()
                os.fsync(stream.fileno())
            os.chmod(temporary, 0o600)
            os.replace(temporary, self.state_file)
            os.chmod(self.state_file, 0o600)
        finally:
            with contextlib.suppress(FileNotFoundError):
                os.unlink(temporary)

    @contextlib.contextmanager
    def locked(self) -> Iterator[dict[str, Any]]:
        # Refused rather than merely unused: taking the lock creates and
        # chmods the lock file, which is a write to the root before the block
        # body has done anything at all.
        self._refuse_if_read_only("taking the state lock")
        self._validate_open()
        _private_dir(self.directory)
        _private_file(self.lock_file)
        with self.lock_file.open("a+", encoding="utf-8") as lock:
            os.chmod(self.lock_file, 0o600)
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
            # Uncached on purpose. Everything above is about being the only
            # writer; reusing a parse taken before the lock was held would
            # hand that writer a document from before whoever it just waited
            # for. The read path may trade staleness for speed, the write
            # path may not.
            data = self.load(use_cache=False)
            # Write only what changed. This block used to save unconditionally,
            # and most callers under it observe rather than mutate -- a poll
            # that finds a worker still running changes nothing at all. Saving
            # anyway meant a directory sweep, a full sorted re-serialisation of
            # the whole document, an fsync and a rename, under the global
            # exclusive lock, for every look. On a mature root that document is
            # tens of megabytes, and one `worker launch` polling in its wait
            # loop held the lock so continuously that status pushes from every
            # other project queued behind it for forty-five minutes. Comparing
            # costs a second serialisation and no disk at all.
            before = json.dumps(data, indent=2, sort_keys=True)
            try:
                yield data
                if json.dumps(data, indent=2, sort_keys=True) != before:
                    self.save(data)
            finally:
                fcntl.flock(lock.fileno(), fcntl.LOCK_UN)
