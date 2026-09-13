"""Who is calling, and whether they may authorize a protected action.

A mixin over `CoordinatorBase`, split out of `status` -- which had grown to
cover seven of these at once. Moved verbatim; it imports nothing from
`helm.core`.
"""

from __future__ import annotations

import contextlib
import hashlib
import os
import signal
from pathlib import Path
from typing import Any

from ..authority import AUTHORITY_ENV, Authority
from ..errors import HelmError, SafetyError
from ..paths import _private_dir, _write_private_text
from ..processes import _process_parents
from ..values import _safe_text


class CallerMixin:
    def caller_identity(self) -> dict[str, str]:
        """Who is running this command, from evidence the caller does not own.

        Two signals, and the least privileged answer wins. The marker every
        agent Helm starts inherits is the first, and it is the one an agent can
        edit: `env -u HELM_WORKER_ID` used to be enough to be read as the root.
        So the second is process ancestry -- a worker cannot make itself not be
        a descendant of the runner Helm started for it, and a command it spawns
        inherits that lineage whatever it does to its environment.

        Returns the role, the worker id it was attributed to, and which signal
        decided it, so a refusal can say what identified the caller.
        """
        data = self.store.load()

        def role_for(worker_id: str) -> str:
            worker = data.get("workers", {}).get(worker_id)
            if worker is None:
                # A marker naming no worker Helm knows is not authority; the
                # least privileged reading is the safe one.
                return "worker"
            task = data.get("tasks", {}).get(worker.get("task_id"))
            return "foreman" if (task or {}).get("role") == "foreman" else "worker"

        marked = os.environ.get("HELM_WORKER_ID", "").strip()
        if marked:
            return {"role": role_for(marked), "worker_id": marked, "evidence": "marker"}
        recorded = {
            worker["pid"]: worker_id
            for worker_id, worker in data.get("workers", {}).items()
            if isinstance(worker.get("pid"), int)
        }
        if recorded:
            # Only pay for the process table when there is something to match:
            # a root with no launched worker cannot be one.
            lineage = {os.getpid(), *_process_parents(os.getpid())}
            with contextlib.suppress(OSError):
                lineage.add(os.getpgid(0))
            for pid in lineage:
                if pid in recorded:
                    worker_id = recorded[pid]
                    return {
                        "role": role_for(worker_id),
                        "worker_id": worker_id,
                        "evidence": "ancestry",
                    }
        return {"role": "root", "worker_id": "", "evidence": "unmarked"}

    def require_same_project(self, worker_id: str, action: str) -> None:
        """Refuse an agent addressing a worker outside its own project.

        Isolation was enforced where work HAPPENS -- worktrees, branches,
        composed context -- and nowhere on the path where agents TALK. So a
        foreman could hand a message to any worker id in the root, and on
        2026-08-23 one did: a review brief naming another project's runbook,
        its tracker rows and a commander decision was delivered into a second
        project's foreman session by a mistyped id. Nothing refused it.

        Isolation is about what an agent is allowed to KNOW, not only what it
        may write, so a message is a leak in its own right -- the receiving
        foreman could not unread it, and correctly stood down rather than
        continue with another project's material in its context.

        The root addresses anything: it coordinates every project by design.
        An agent is confined to its own.
        """
        identity = self.caller_identity()
        if identity["role"] == "root":
            return
        data = self.store.load()
        caller = data.get("workers", {}).get(identity["worker_id"]) or {}
        target = data.get("workers", {}).get(worker_id) or {}
        mine, theirs = caller.get("project_id"), target.get("project_id")
        if not mine or not theirs or mine == theirs:
            return
        raise HelmError(
            f"{action} refused: worker {worker_id} belongs to project {theirs}, "
            f"and you are {identity['worker_id']} on {mine}. One worker serves one "
            "project, and a message is context -- an agent cannot unread another "
            "project's material. Check the worker id; ask the root to route it."
        )

    def caller_role(self) -> str:
        """The calling agent's role: the root, a foreman, or a worker."""
        return self.caller_identity()["role"]

    def _authority_hash(self) -> str:
        configured = self.store.load().get("config", {}).get("authority_hash")
        return str(configured or "")

    def configure_authority(self, secret: str) -> Path:
        """Record the hash of the root's capability and store the secret 0600.

        The secret itself is never printed, logged, or put in state: only its
        hash is, which is what makes the check possible without Helm holding a
        credential it could leak. The file exists so a human can export it into
        their own shell; the value never crosses this process's output.
        """
        # Gated like everything it protects: an agent that could write the
        # root's capability hash would be issuing its own authority. The first
        # configuration on a root passes on the session-role check; every later
        # one requires the capability already in force.
        self.authority("configuring the root's authority capability")
        secret = str(secret or "")
        if len(secret) < 32:
            raise SafetyError("an authority capability must be at least 32 characters")
        digest = hashlib.sha256(secret.encode("utf-8")).hexdigest()
        with self.store.locked() as data:
            data.setdefault("config", {})["authority_hash"] = digest
        path = _private_dir(self.store.directory / "private") / "authority.secret"
        _write_private_text(path, secret + "\n")
        return path

    def write_preferences(self, updated: Any) -> Path:
        """Write the root's preferences, on the root's authority.

        A preference is the commander's own cost and safety policy for this
        machine; an agent that could write one could lift the exclusion that
        stops it starting an expensive runtime. So the write obtains the same
        authority a merge does, here in core, not only in CLI dispatch.
        """
        self.authority("writing a preference")
        from .. import preferences as _prefs
        return _prefs.save(updated)

    def authority(self, action: str, project_id: str | None = None) -> Authority:
        """Build the capability a protected core operation requires, or refuse.

        This is the boundary against a cooperating agent: one that follows
        the protocol but reasons badly or is prompt-injected. It is here, in
        core, rather than in CLI dispatch, because an agent that can import
        `Coordinator` bypasses dispatch entirely -- and the actions on the
        other side of this line cannot be undone by deleting a branch. It is
        not isolation: every agent runs as Helm's own user, so an agent that
        sets out to defeat it can edit the store or shed its ancestry. What
        that means in practice is written down in docs/security.md.
        """
        action = _safe_text(action).strip() or "this action"
        identity = self.caller_identity()
        if identity["role"] != "root":
            raise SafetyError(
                f"{action} is the human's, held at the Helm root. This caller was "
                f"identified as {identity['role']} {identity['worker_id']} by "
                f"{identity['evidence']}; an agent cannot authorize it for itself."
            )
        expected = self._authority_hash()
        if not expected:
            # No capability configured for this root. The session-role boundary
            # is all there is, and the record says so rather than implying a
            # stronger check than the one that ran.
            return Authority("session", "root")
        presented = os.environ.get(AUTHORITY_ENV, "")
        if not presented or hashlib.sha256(presented.encode("utf-8")).hexdigest() != expected:
            raise SafetyError(
                f"{action} requires this root's authorization capability in "
                f"{AUTHORITY_ENV}; it is missing or does not match. No agent Helm "
                "starts can inherit it."
            )
        return Authority("capability", "root")
