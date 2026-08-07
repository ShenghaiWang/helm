"""Built-in agent runtimes for delegated workers.

Helm's native path must not require a provider command, but a worker still has
to be *started* somehow.  This module is the small, auditable table of agent
CLIs Helm knows how to launch, so a task can be delegated to Claude Code,
Codex, or pi without anyone writing an ``agents.json`` first.

Nothing here is authority.  A runtime contributes exactly three things: the
executable and argv shape that hands an agent one prompt, the narrow set of
credential variables that runtime reads (worker environments are scrubbed, so
an unlisted variable stays stripped), and an environment marker used to guess
which runtime *this* Helm session is itself running under.  Availability is
still proved by finding the executable, a configured profile always outranks a
built-in default, and a wrong guess is corrected by naming a runtime
explicitly rather than by editing this file.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from dataclasses import dataclass
from typing import Mapping, Sequence

# A launch command may carry this token once; Helm replaces it with the
# assignment's bootstrap prompt after the worker's context document exists.
PROMPT_PLACEHOLDER = "{prompt}"

# An assignment's context document lives in Helm state, deliberately outside
# the project worktree so it can never be committed into the project. A runtime
# that gates file access per directory therefore has to be told about it, or it
# stalls before it can read its own brief.
WORKER_DIR_PLACEHOLDER = "{worker_dir}"

# Helm's state directory. A worker reports by running a Helm command, and that
# command writes to the store -- so a runtime that sandboxes writes to the
# worktree cannot report at all, and goes silent while looking like it is
# working. Runtimes that gate writes per directory are told about this one.
STATE_DIR_PLACEHOLDER = "{state_dir}"


@dataclass(frozen=True)
class AgentRuntime:
    """One agent CLI Helm knows how to start as a worker."""

    id: str
    name: str
    # Interactive argv, used when the worker gets a real terminal (a Herdr
    # pane), so the agent renders its session and the user can type into it.
    interactive: tuple[str, ...]
    # Non-interactive argv, used by the process fallback where stdout is a
    # pipe and a full-screen TUI would only produce escape noise.
    noninteractive: tuple[str, ...]
    # Environment variables this runtime reads for credentials or config.
    # Forwarded only for a worker actually launched with this runtime.
    env_passthrough: tuple[str, ...]
    # Variables the CLI sets in its own children; presence means this Helm
    # session is probably running inside that agent.  Best effort only.
    detect_env: tuple[str, ...]
    # Flag that selects a model. Needed when only one runtime is installed and
    # a review still has to be run by something other than the author.
    model_flag: str = "--model"

    def with_model(self, model: str | None, *, interactive: bool) -> list[str]:
        command = self.command(interactive=interactive)
        if not model:
            return command
        # Insert before the prompt so a variadic option cannot swallow it.
        return [command[0], self.model_flag, model, *command[1:]]

    def command(self, *, interactive: bool) -> list[str]:
        return list(self.interactive if interactive else self.noninteractive)

    def environment(self, source: Mapping[str, str] | None = None) -> dict[str, str]:
        source = os.environ if source is None else source
        return {name: source[name] for name in self.env_passthrough if source.get(name)}


BUILTIN_RUNTIMES: tuple[AgentRuntime, ...] = (
    AgentRuntime(
        id="claude",
        name="Claude Code",
        # A Helm worker runs in a pane nobody is watching, so an interactive
        # permission prompt is not a safety gate -- it is a deadlock. The
        # boundary that actually holds is the one Helm enforces: an isolated
        # worktree, a scrubbed environment, and the core safety rules the
        # assignment opens with. --add-dir grants the worker its own context
        # document, which lives outside the worktree by design.
        # Ordering is load-bearing: --add-dir takes a VARIADIC list, so it
        # swallows whatever follows it. The prompt must never sit directly
        # after it -- another flag has to terminate the list first, or the
        # agent starts with an empty prompt and waits forever.
        interactive=(
            "claude",
            "--add-dir",
            WORKER_DIR_PLACEHOLDER,
            "--permission-mode",
            "bypassPermissions",
            PROMPT_PLACEHOLDER,
        ),
        noninteractive=(
            "claude",
            "--add-dir",
            WORKER_DIR_PLACEHOLDER,
            "--permission-mode",
            "bypassPermissions",
            "--print",
            PROMPT_PLACEHOLDER,
        ),
        env_passthrough=(
            "ANTHROPIC_API_KEY",
            "ANTHROPIC_AUTH_TOKEN",
            "ANTHROPIC_BASE_URL",
            "ANTHROPIC_MODEL",
            "CLAUDE_CONFIG_DIR",
        ),
        detect_env=("CLAUDECODE", "CLAUDE_CODE_ENTRYPOINT"),
    ),
    AgentRuntime(
        id="codex",
        name="Codex CLI",
        # Same reason as Claude Code's permission mode: a Helm worker runs in
        # a pane nobody is watching, so an approval prompt is a deadlock, not a
        # gate.
        # --add-dir names the one directory reporting needs. Codex otherwise
        # confines writes to the workspace, which excludes Helm's state -- so
        # `helm worker message` fails with "Operation not permitted" and the
        # worker goes silent while looking like it is working. That cost a
        # completed, correct review: the verdict existed only in the
        # reviewer's pane and reached nobody.
        # --sandbox is required for that to take effect, and omitting it does
        # not keep the sandbox narrow -- it makes Codex refuse the flag
        # outright: "Ignoring --add-dir because the effective permissions do
        # not allow additional writable roots", then exit 1. So every codex
        # worker died at launch, which took the independent reviewer with it
        # and silently reduced code review to same-runtime self-review.
        # workspace-write is the narrower of the two modes Codex offers here;
        # danger-full-access would drop the sandbox entirely. This grants the
        # workspace plus exactly the named directory, which is the boundary
        # that was always intended.
        interactive=(
            "codex",
            "--sandbox",
            "workspace-write",
            "--add-dir",
            STATE_DIR_PLACEHOLDER,
            "--ask-for-approval",
            "never",
            PROMPT_PLACEHOLDER,
        ),
        noninteractive=(
            "codex",
            "exec",
            "--sandbox",
            "workspace-write",
            "--add-dir",
            STATE_DIR_PLACEHOLDER,
            PROMPT_PLACEHOLDER,
        ),
        env_passthrough=(
            "OPENAI_API_KEY",
            "CODEX_API_KEY",
            "CODEX_HOME",
        ),
        detect_env=("CODEX_SANDBOX",),
    ),
    AgentRuntime(
        id="pi",
        name="pi",
        # Helm-created worker worktrees contain project-local files that Pi
        # otherwise asks the user to trust. No user is present to answer that
        # bootstrap prompt in either worker launch mode.
        interactive=("pi", "--approve", PROMPT_PLACEHOLDER),
        noninteractive=("pi", "--approve", "--print", PROMPT_PLACEHOLDER),
        env_passthrough=(
            "ANTHROPIC_API_KEY",
            "OPENAI_API_KEY",
            "GEMINI_API_KEY",
            "GOOGLE_API_KEY",
            "GOOGLE_GENERATIVE_AI_API_KEY",
        ),
        detect_env=("PI_CODING_AGENT",),
    ),
    AgentRuntime(
        id="opencode",
        name="opencode",
        # Same reason as every runtime above: a Helm worker runs in a pane
        # nobody is watching, so a permission prompt is a deadlock, not a gate.
        # `--auto` is opencode's form of that.
        #
        # There is deliberately no --add-dir analogue here, and that absence
        # was *checked* rather than assumed -- assuming it is what silently
        # killed every codex worker until the flags above were found. Under
        # `--auto` opencode writes to absolute paths outside its working
        # directory, so a worker can reach Helm's state and `helm worker
        # message` lands. A runtime that could not do that would go silent
        # while looking like it was working.
        #
        # Interactive is the TUI seeded with --prompt; non-interactive is the
        # `run` subcommand. `--model` is accepted both before the subcommand
        # and after it, which is what `with_model` needs, since it inserts the
        # flag directly after argv[0].
        interactive=("opencode", "--auto", "--prompt", PROMPT_PLACEHOLDER),
        noninteractive=("opencode", "run", "--auto", PROMPT_PLACEHOLDER),
        # opencode keeps credentials in ~/.local/share/opencode/auth.json and
        # config in ~/.config/opencode, both reached through HOME, which
        # survives the worker scrub. These are forwarded for roots that
        # configure a provider by environment instead.
        env_passthrough=(
            "OPENROUTER_API_KEY",
            "ANTHROPIC_API_KEY",
            "OPENAI_API_KEY",
            "GOOGLE_GENERATIVE_AI_API_KEY",
        ),
        detect_env=("OPENCODE", "OPENCODE_PID"),
    ),
    AgentRuntime(
        id="omp",
        name="Oh My Pi",
        # Same reason as every runtime above: nobody is watching this pane, so
        # an approval prompt is a deadlock rather than a gate.
        #
        # omp publishes `--add-dir`, which usually means writes are confined
        # and Helm's state would be out of reach -- the failure that silenced
        # every codex worker. It is not: under `--auto-approve` omp writes to
        # absolute paths outside its working directory, verified before this
        # entry was written rather than inferred from the flag's existence. So
        # `helm worker message` lands and no extra grant is needed.
        interactive=("omp", "--auto-approve", PROMPT_PLACEHOLDER),
        noninteractive=("omp", "--auto-approve", "--print", PROMPT_PLACEHOLDER),
        # Auth lives in an omp profile under HOME, which survives the worker
        # scrub. These are forwarded for roots that configure a provider by
        # environment instead. PI_SMOL_MODEL and friends are deliberately not
        # here: they choose models, and model choice belongs to the task.
        env_passthrough=(
            "ANTHROPIC_API_KEY",
            "OPENAI_API_KEY",
            "GEMINI_API_KEY",
            "GOOGLE_API_KEY",
            "GOOGLE_GENERATIVE_AI_API_KEY",
        ),
        detect_env=("OMPCODE",),
    ),
)

_BY_ID = {runtime.id: runtime for runtime in BUILTIN_RUNTIMES}


def builtin_runtime(runtime_id: str | None) -> AgentRuntime | None:
    if not runtime_id:
        return None
    return _BY_ID.get(runtime_id)


def builtin_runtime_ids() -> list[str]:
    return [runtime.id for runtime in BUILTIN_RUNTIMES]


def parse_herdr_integration_status(output: str) -> dict[str, str]:
    """Parse ``herdr integration status`` without retaining local paths."""
    statuses: dict[str, str] = {}
    for raw in output.splitlines():
        line = raw.strip()
        match = re.match(r"^([A-Za-z0-9_-]+):\s+([^()]+?)(?:\s+\(|$)", line)
        if match:
            statuses[match.group(1)] = match.group(2).strip()
    return statuses


def herdr_integration_status(source: Mapping[str, str] | None = None) -> dict[str, str] | None:
    """Return Herdr's agent integration inventory when safely available.

    Herdr's CLI is session-scoped. Outside a Herdr-managed pane there is no
    caller context Helm owns, so absence here is "not safely queryable" rather
    than evidence that no integrations exist.
    """
    source = os.environ if source is None else source
    if source.get("HERDR_ENV") != "1" or shutil.which("herdr") is None:
        return None
    try:
        result = subprocess.run(
            ["herdr", "integration", "status"],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            check=False,
            timeout=3,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    return parse_herdr_integration_status(result.stdout)


def detect_runtime(source: Mapping[str, str] | None = None) -> AgentRuntime | None:
    """Guess the runtime this Helm session is itself running under.

    Used only as the last default, so a wrong guess still has to survive the
    executable check and can always be overridden by naming a runtime.
    """
    source = os.environ if source is None else source
    for runtime in BUILTIN_RUNTIMES:
        if any(source.get(marker) for marker in runtime.detect_env):
            return runtime
    return None


def apply_prompt(
    command: Sequence[str], prompt: str, worker_dir: str = "", state_dir: str = ""
) -> list[str]:
    """Fill a launch command's placeholder slots.

    A command without them is returned unchanged: an external worker that
    reads only ``HELM_CONTEXT_FILE`` keeps working exactly as before.
    """
    filled = {
        PROMPT_PLACEHOLDER: prompt,
        WORKER_DIR_PLACEHOLDER: worker_dir,
        STATE_DIR_PLACEHOLDER: state_dir,
    }
    return [filled.get(part, part) for part in command]


def wants_prompt(command: Sequence[str]) -> bool:
    return PROMPT_PLACEHOLDER in command
