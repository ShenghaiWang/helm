"""Proof that a protected action was authorized by the root, not an agent."""

from __future__ import annotations

from .errors import SafetyError


#: The environment variable that carries the root's authorization capability.
#: Deliberately absent from the allowlist in `helm.launching.worker_environment`,
#: so no agent Helm starts can inherit it, and never written into a worker's
#: context document, prompt, or reporting command.
AUTHORITY_ENV = "HELM_AUTHORITY"


class Authority:
    """Proof that a protected action was authorized by the root, not an agent.

    Helm used to decide this in CLI dispatch, from the *absence* of a worker
    marker in the environment. A worker owns the environment of the commands it
    starts, so `env -u HELM_WORKER_ID helm approval release ...` was accepted,
    and importing `Coordinator` skipped the check altogether. Absence of
    evidence is not authority: an object of this type is required by every
    protected core operation, and only `Coordinator.authority()` can build one.

    `mode` records which boundary actually held, because an audit that cannot
    distinguish a capability-backed decision from a session-role one is telling
    the reader less than it appears to.
    """

    __slots__ = ("mode", "actor")

    def __init__(self, mode: str, actor: str) -> None:
        if mode not in {"capability", "session"}:
            raise SafetyError(f"unknown authority mode: {mode}")
        self.mode = mode
        self.actor = actor

    def record(self) -> dict[str, str]:
        return {"mode": self.mode, "actor": self.actor}
