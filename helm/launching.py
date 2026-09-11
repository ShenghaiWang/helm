"""Preparing the environment a worker is started in.

Three module functions lifted out of `core` unchanged: the environment a
worker inherits, the trust record that stops a runtime asking whether it may
read the directory Helm just built for it, and whether a launch command names
something actually on PATH. Agent resolution and worker launch both need
them, which is why they sit below both.
"""

from __future__ import annotations

import json
import os
import shutil
from pathlib import Path
from typing import Any, Sequence

from .errors import HelmError
from .paths import canonical

#: Runtimes that ask, on a TTY, whether the directory they were started in is
#: trusted -- and where that answer is stored, keyed by absolute path.
_TRUST_CONFIGS = {"claude": (Path.home() / ".claude.json", "hasTrustDialogAccepted")}

def _pretrust_workspace(agent_id: str | None, workspace: Path) -> None:
    """Record that a workspace Helm just built is one Helm trusts.

    Claude Code asks "Is this a project you created or one you trust?" the
    first time it starts in a directory, and skips the question only in
    non-interactive mode. Helm gives every task a *fresh* worktree, so the
    directory is always one it has never seen -- which made the question
    certain, and a question in a pane nobody is watching is a deadlock, not a
    safety gate. `--permission-mode bypassPermissions` does not cover it and
    neither does `--dangerously-skip-permissions`; both were tried.

    The trust answer is a real one rather than a bypass: Helm created this
    directory, from a repository the commander registered, moments ago. What is
    dishonest is asking a human about it in a pane that has no human.

    Deliberately narrow, because this is the commander's own file:
    - only the workspace path Helm just created, never a parent, never a wildcard
    - only the one key, merged into whatever else that entry holds
    - never removes or rewrites anything else, and never reads a value out
    - any failure is swallowed. A trust entry is an optimisation; if it does not
      land the worker stalls at the prompt exactly as it does today, which the
      health check now reports. Breaking a launch over it would be worse than
      the problem.
    """
    entry = _TRUST_CONFIGS.get(agent_id or "")
    if entry is None:
        return
    config_path, key = entry
    try:
        raw = json.loads(config_path.read_text()) if config_path.exists() else {}
        if not isinstance(raw, dict):
            return
        projects = raw.setdefault("projects", {})
        if not isinstance(projects, dict):
            return
        path = str(canonical(workspace))
        existing = projects.get(path)
        if isinstance(existing, dict) and existing.get(key) is True:
            return
        projects[path] = {**(existing if isinstance(existing, dict) else {}), key: True}
        # Written whole through a neighbouring temp file and renamed, so a
        # crash or a concurrent read never sees a half-written config. The
        # runtime rewrites this file itself, so a lost race costs one prompt,
        # not a corrupted file.
        tmp = config_path.with_name(f"{config_path.name}.helm-{os.getpid()}.tmp")
        tmp.write_text(json.dumps(raw, indent=2))
        os.replace(tmp, config_path)
    except (OSError, ValueError, TypeError):
        return


def worker_environment(source: dict[str, str] | None = None) -> dict[str, str]:
    """Keep worker processes from inheriting ambient credentials or paths."""
    source = source or os.environ
    allowed_exact = {
        "PATH",
        "HOME",
        "USER",
        "LOGNAME",
        "TERM",
        "SHELL",
        "TMPDIR",
        "TMP",
        "TEMP",
        "SYSTEMROOT",
        "PATHEXT",
    }
    result = {
        key: value
        for key, value in source.items()
        if key in allowed_exact or key.startswith("LC_")
    }
    result["GIT_TERMINAL_PROMPT"] = "0"
    return result


def _command_executable_available(
    command: Sequence[str], *, cwd: Path | None = None
) -> tuple[bool, str]:
    if not command:
        return False, "no launch command configured"
    executable = command[0]
    if os.path.sep in executable and not Path(executable).is_absolute() and cwd is not None:
        resolved = cwd / executable
    else:
        resolved = Path(executable).expanduser() if os.path.sep in executable else None
    if resolved is not None:
        if not resolved.is_file() or not os.access(resolved, os.X_OK):
            return False, f"launch executable is not executable: {executable}"
    elif shutil.which(executable) is None:
        return False, f"launch executable is not on PATH: {executable}"
    return True, "launch executable validated"
