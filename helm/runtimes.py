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
from typing import Callable, Iterable, Mapping, Sequence

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
        id="cursor",
        name="Cursor CLI",
        # Same reason as every runtime above: nobody is watching this pane, so
        # an approval prompt is a deadlock rather than a gate. `--force` is
        # cursor's form of that (`--yolo` is documented as its alias).
        #
        # The executable is `cursor-agent`, not `cursor`. Herdr recognises this
        # agent, which is why it appeared under "integrations not in Helm's
        # built-ins" -- but Herdr recognising a running agent is not a launch
        # recipe, so it was unusable for Helm work until this entry existed.
        #
        # It publishes BOTH `--add-dir` and `--sandbox`, the pair that usually
        # means writes are confined and Helm's state would be out of reach --
        # the failure that silenced every codex worker. It is not confined:
        # under `--force` it writes to an absolute path outside its working
        # directory, checked by running it rather than inferred from the flags
        # existing. So `helm worker message` lands and no extra grant is needed.
        #
        # Interactive takes the prompt positionally; `--print` is the
        # non-interactive form and is documented as retaining every tool,
        # including write and shell. `--model` sits before the prompt, which is
        # what `with_model` needs.
        interactive=("cursor-agent", "--force", PROMPT_PLACEHOLDER),
        noninteractive=("cursor-agent", "--force", "--print", PROMPT_PLACEHOLDER),
        # Auth is a login under HOME, which survives the worker scrub.
        # CURSOR_API_KEY is forwarded for roots that authenticate by
        # environment instead; CURSOR_API_ENDPOINT for a proxied install.
        env_passthrough=(
            "CURSOR_API_KEY",
            "CURSOR_API_ENDPOINT",
        ),
        detect_env=("CURSOR_AGENT", "CURSOR_TRACE_ID"),
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

# ---------- naming an agent and a model ----------
#
# Both of these end up in an argv, so the only question either one answers is
# "can this turn into shell, or into another argument". Neither validates that
# the thing named exists: that is the runtime's answer to give, and a list of
# valid models baked in here would go stale the first week. They live in this
# module because it is the one place that owns the vocabulary of agents and
# models, and because the preferences layer needs them without importing core.

_AGENT_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}")
_MODEL_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:~/-]{0,127}")


def validate_agent_id(agent_id: object) -> str:
    """Accept a runtime/profile id without letting it become a command."""
    if not isinstance(agent_id, str) or not _AGENT_ID.fullmatch(agent_id):
        raise ValueError(
            "agent id must be 1-64 characters: letters, numbers, '.', '_' or '-' only"
        )
    return agent_id


def validate_model_id(model_id: object) -> str:
    """Accept a model name without letting it become a command.

    Deliberately wider than an agent id: real model names carry slashes and
    colons -- ``openrouter/~vendor/model-latest``, ``model:high`` -- so the
    check is that it cannot turn into shell or another argument, not that it
    matches any one vendor's spelling.
    """
    if not isinstance(model_id, str) or not _MODEL_ID.fullmatch(model_id):
        raise ValueError(
            "model must be 1-128 characters: letters, numbers, '.', '_', ':', "
            "'~', '/' or '-' only"
        )
    return model_id


# ---------- model families: generic technical metadata ----------
#
# Which vendor's family a model identifier belongs to is a *fact* about the
# identifier, not a policy about what may run it. Helm ships the classifier
# because gateway spellings make the question genuinely hard, and because a
# root that wants to restrict a family needs something reliable to name.
#
# Nothing here refuses anything. A family becomes a restriction only when a
# root's own ``preferences.json`` says so -- see ``helm/preferences.py``. That
# separation is the point: shipped Helm imposes no operator's runtime choice on
# anybody who clones it.

# Segments are split on the separators gateways and provider-qualified ids use:
# ``anthropic/claude-opus-4``, ``openrouter/anthropic/claude-3.5-sonnet``,
# ``vertex:claude-sonnet-4``, ``bedrock/us.anthropic.claude-sonnet-4-v1:0``.
_MODEL_SEGMENTS = re.compile(r"[/:@|,]+")

# The provider whose whole catalogue is Claude. A path segment naming it is
# enough on its own, which is what catches gateway spellings whose model half
# is abbreviated past recognition.
_ANTHROPIC_SEGMENT = "anthropic"

# ``claude`` anywhere at the head of a segment, plus the bare family aliases
# agent CLIs accept in place of a full id. The aliases are deliberately narrow:
# they match alone or with a version-shaped suffix, so ``opus-4.1`` and
# ``sonnet-4-5`` are Claude while ``opus-magnum`` and ``sonnetize`` are not.
_CLAUDE_NAME = re.compile(r"^claude(?:[-._].*)?$")
_CLAUDE_ALIAS = re.compile(r"^(?:opus|sonnet|haiku|fable)(?:[-._]?\d[a-z0-9._-]*)?$")

# Some gateways flatten the provider into the model half of one segment rather
# than keeping it as its own path element: ``azure/anthropic-claude-sonnet-4``
# has no ``anthropic`` segment at all. So a segment's own ``-``/``.``/``_``
# tokens are checked too. Token equality, never a substring: ``myanthropic``
# and ``claudette`` are single tokens that match nothing.
_PROVIDER_TOKENS = re.compile(r"[-._]+")
_CLAUDE_TOKENS = frozenset({_ANTHROPIC_SEGMENT, "claude"})


def is_claude_model(model: str | None) -> bool:
    """Report whether a model identifier names a Claude-family model.

    Narrow on purpose. Matching a bare substring would classify any string
    that happens to contain ``haiku`` as Claude and refuse a launch nobody
    asked to restrict, so every test here is anchored to a whole segment of
    the identifier.
    """
    if not model:
        return False
    for segment in _MODEL_SEGMENTS.split(model.strip().lower()):
        if not segment:
            continue
        if _CLAUDE_NAME.match(segment) or _CLAUDE_ALIAS.match(segment):
            return True
        # Bedrock-style ids carry the family inside one dotted segment, and
        # provider-prefixed gateway ids carry it inside a dashed one.
        if _CLAUDE_TOKENS & set(_PROVIDER_TOKENS.split(segment)):
            return True
    return False


# family id -> classifier. One entry today; the shape is the contract, so a
# second vendor family is a function and a line here rather than a new boundary
# threaded through core. A root names these ids in `model.runtimes.<family>`.
MODEL_FAMILIES: dict[str, "Callable[[str | None], bool]"] = {
    "claude": is_claude_model,
}


def model_family_ids() -> list[str]:
    return sorted(MODEL_FAMILIES)


def model_families(model: str | None) -> tuple[str, ...]:
    """Every family a model identifier belongs to, in stable order."""
    if not model:
        return ()
    return tuple(family for family in model_family_ids() if MODEL_FAMILIES[family](model))


# Flags that select a model in the CLIs Helm knows how to launch. Every
# built-in runtime publishes ``--model``; none of them establishes a short
# form here, so none is guessed -- an invented ``-m`` would refuse launches for
# a flag that may mean something else entirely to the command being run.
_MODEL_FLAGS = frozenset({runtime.model_flag for runtime in BUILTIN_RUNTIMES})


def model_in_command(command: Sequence[str]) -> str | None:
    """Find the model a command selects for itself, if it is visible.

    A model does not have to arrive through Helm's model field: a configured
    profile or a caller-supplied command can bake ``--model`` straight into
    argv, which would otherwise walk past a boundary checked only where Helm
    places a model itself.

    **This reads argv and nothing else, which is the exact limit of it.** A
    command that is an opaque wrapper -- a shell script, an alias, a launcher
    that reads its own config file or an environment variable, or one that
    spells the selection in a form not listed here -- can still choose a model
    Helm never sees, and Helm does not pretend otherwise: it cannot execute or
    introspect the wrapper to find out. Naming a runtime with its own command
    is a deliberate act by whoever configured this root, and the pairing there
    is theirs to keep. What is *visible* is refused.
    """
    parts = list(command)
    for index, part in enumerate(parts):
        flag, joined, inline = part.partition("=")
        if flag not in _MODEL_FLAGS:
            continue
        if joined:
            # `--model=` states an empty value. The next argument is the
            # prompt, not the model, and reading it as one would refuse a
            # launch over whatever the brief happened to mention.
            value = inline
        else:
            value = parts[index + 1] if index + 1 < len(parts) else ""
        if value:
            return value
    return None


def family_pairing_error(
    model: str,
    family: str,
    allowed: Iterable[str],
    runtime_id: str | None,
    reason: str = "",
) -> str:
    """The one message every rejected model-family/runtime pairing uses.

    It names the preference that produced the refusal, because the refusal is
    a *local* choice: somebody reading it on another machine, or six months
    later, has to be able to tell that Helm did not decide this and to find the
    one command that changes it.
    """
    named = runtime_id or "an unnamed runtime"
    permitted = ", ".join(sorted(allowed))
    first = sorted(allowed)[0]
    return (
        f"model {model} is in the {family} model family, which this Helm root's "
        f"preferences restrict to the {permitted} runtime(s), but {named} was "
        "selected"
        + (f" because {reason}" if reason else "")
        + f". Name --agent {first}, or choose a model outside the {family} family "
        f"for {named}. Helm will not substitute a runtime or a model for you. "
        f"This restriction is local to this root: remove it with "
        f"`helm prefs unset model.runtimes.{family}`."
    )


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
