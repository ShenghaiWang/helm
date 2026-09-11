"""Helm's user-facing error types, shared by every layer above this one."""
from __future__ import annotations


class HelmError(RuntimeError):
    """An expected, user-facing coordinator error."""


class SafetyError(HelmError):
    """An operation was refused by an isolation or approval guard."""
