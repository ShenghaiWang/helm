"""Live model catalogues and conservative cost classification.

Helm must not carry a model name or a price in the repository: both go stale
in months, and a stale name asserted confidently is a failed launch. So the
catalogues come from the agent CLIs themselves, queried near dispatch time,
and this module is the small auditable table of which runtimes publish a safe
catalogue command, plus the parsers that turn that output into exact model
ids.

Three rules hold here:

**Only launchable runtimes are queried.** ``CATALOGUE_COMMANDS`` exists for
built-ins that publish a list command (``pi --list-models``, ``opencode
models``). A runtime with no safe catalogue command is reported
``unsupported``, never guessed at. An excluded runtime is not queried at all:
Helm's exclusion is a standing decision about cost, and a query behind it
would be a way around the decision.

**Free is classified from explicit catalogue evidence only.** A ``:free``
suffix is the marker gateways print in the model id itself, and the opencode
gateway's own provider uses a ``-free`` suffix the same way. Nothing else
counts: a session UI showing ``$0.00``, a login or subscription state, a
vague memory of a price, or a model name that sounds cheap all stay
``unknown``. Prices are never committed anywhere and never inferred.

**Nothing sensitive is touched.** The catalogue commands are the tool's own
status surfaces -- they read whatever credentials the tool itself owns, which
Helm never inspects. Helm captures stdout only through a narrow parser that
keeps model ids and drops everything else, sends stderr to the void, injects
no environment, and bounds every command with a timeout so a hung catalogue
cannot stall a dispatch.
"""

from __future__ import annotations

import re
import shutil
import subprocess
from dataclasses import dataclass
from typing import Iterable, Sequence

#: How long one catalogue command may run before it is abandoned. A catalogue
#: is a nice-to-have at dispatch time, never a gate, so a hung one must cost
#: seconds, not a session.
CATALOGUE_TIMEOUT = 8

#: Runtime id -> safe catalogue command. Only built-ins that publish a list
#: command belong here; a runtime not listed is reported as having no
#: catalogue Helm may query, which is a statement about the surface, never an
#: excuse to parse somebody's config file.
CATALOGUE_COMMANDS: dict[str, tuple[str, ...]] = {
    "pi": ("pi", "--list-models"),
    "opencode": ("opencode", "models"),
}


@dataclass(frozen=True)
class ModelEntry:
    """One exact model id a catalogue reported, with its cost evidence."""

    runtime: str
    id: str
    free: bool


@dataclass(frozen=True)
class CatalogueResult:
    """One runtime's catalogue query, with a deterministic outcome.

    ``available`` is True only when the command ran to completion and its
    output produced at least one row; ``reason`` is the exact statement of
    what happened, so the CLI and the composed context can say why a
    catalogue is missing without guessing.
    """

    runtime: str
    command: tuple[str, ...]
    supported: bool
    available: bool
    reason: str
    models: tuple[ModelEntry, ...] = ()

    @property
    def free_models(self) -> tuple[str, ...]:
        return tuple(entry.id for entry in self.models if entry.free)


UNSUPPORTED_REASON = "this runtime publishes no model catalogue Helm may query"


def catalogue_command(runtime_id: str) -> tuple[str, ...] | None:
    return CATALOGUE_COMMANDS.get(runtime_id)


# ---------- free classification: explicit evidence only ----------
#
# The one real marker is the `:free` suffix gateways print in the model id
# itself -- `openrouter/example/model:free` -- and it is present verbatim in
# both pi's and opencode's catalogues. The opencode gateway's own provider
# spells the same fact as a trailing `-free` segment. Both are part of the id
# a catalogue prints, which is the only class of evidence this module trusts.
_FREE_SUFFIX = ":free"


def is_free_model(model_id: str) -> bool:
    """Whether a catalogue id itself carries an explicit free marker.

    Narrow on purpose: any other signal -- display rendering, login state, a
    remembered or suspected price -- is a guess, and a guess about money is
    the one thing Helm must never make on behalf of the person paying.
    """
    lowered = model_id.strip().lower()
    if lowered.endswith(_FREE_SUFFIX):
        return True
    # The opencode gateway's own provider (`opencode/...`). Scoped to that
    # provider segment because a bare `-free` suffix is a claim only that
    # catalogue makes; an OpenRouter id, for example, spells it `:free`.
    if lowered.startswith("opencode/"):
        head, _, tail = lowered.rpartition("/")
        return bool(head) and tail.endswith("-free")
    return False


# ---------- parsing ----------

# The same narrow validator a model id passes everywhere else, applied to
# catalogue rows before anything may be echoed back out.
_MODEL_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:~/-]{0,127}$")

# pi's table: `provider  model  context  max-out  thinking  images`. Rows are
# whitespace-separated; the provider and the model are the first two fields.
_WHITESPACE = re.compile(r"\s+")


def parse_pi_catalogue(output: str) -> list[str]:
    """Parse ``pi --list-models`` rows into exact model ids, once each."""
    models: list[str] = []
    for raw in output.splitlines():
        fields = _WHITESPACE.split(raw.strip())
        if len(fields) < 2 or fields[0] == "provider":
            continue
        model = fields[1]
        if _MODEL_ID.fullmatch(model):
            models.append(model)
    return list(dict.fromkeys(models))


def parse_opencode_catalogue(output: str) -> list[str]:
    """Parse ``opencode models`` lines (one ``provider/model`` per line), once each."""
    models: list[str] = []
    for raw in output.splitlines():
        model = raw.strip()
        if not model or "/" not in model:
            continue
        if _MODEL_ID.fullmatch(model):
            models.append(model)
    return list(dict.fromkeys(models))


PARSERS: dict[str, "callable[[str], list[str]]"] = {
    "pi": parse_pi_catalogue,
    "opencode": parse_opencode_catalogue,
}


def query_catalogue(
    runtime_id: str,
    *,
    timeout: float = CATALOGUE_TIMEOUT,
    which: "callable[[str], str | None] | None" = None,
) -> CatalogueResult:
    """Query one runtime's catalogue with a bounded, deterministic outcome.

    ``which`` is injectable for tests; production uses the executable search.
    """
    command = catalogue_command(runtime_id)
    if command is None:
        return CatalogueResult(
            runtime=runtime_id,
            command=(),
            supported=False,
            available=False,
            reason=UNSUPPORTED_REASON,
        )
    find = shutil.which if which is None else which
    if find(command[0]) is None:
        return CatalogueResult(
            runtime=runtime_id,
            command=command,
            supported=True,
            available=False,
            reason=f"catalogue executable not found: {command[0]}",
        )
    try:
        result = subprocess.run(
            command,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            check=False,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return CatalogueResult(
            runtime=runtime_id,
            command=command,
            supported=True,
            available=False,
            reason=f"catalogue command timed out after {timeout:g}s",
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return CatalogueResult(
            runtime=runtime_id,
            command=command,
            supported=True,
            available=False,
            reason=f"catalogue command failed: {exc}",
        )
    if result.returncode != 0:
        return CatalogueResult(
            runtime=runtime_id,
            command=command,
            supported=True,
            available=False,
            reason=f"catalogue command exited {result.returncode}",
        )
    parser = PARSERS.get(runtime_id)
    ids = parser(result.stdout or "") if parser is not None else []
    entries = tuple(
        ModelEntry(runtime=runtime_id, id=model_id, free=is_free_model(model_id))
        for model_id in ids
    )
    if not entries:
        return CatalogueResult(
            runtime=runtime_id,
            command=command,
            supported=True,
            available=False,
            reason="catalogue command produced no parseable model rows",
        )
    return CatalogueResult(
        runtime=runtime_id,
        command=command,
        supported=True,
        available=True,
        reason="ok",
        models=entries,
    )


def query_launchable_catalogues(
    launchable: Iterable[str], *, timeout: float = CATALOGUE_TIMEOUT
) -> list[CatalogueResult]:
    """Query only the runtimes a caller has already proved launchable.

    The caller names the set -- usually launchable minus excluded -- so this
    module never decides availability and never queries behind an exclusion.
    """
    return [
        query_catalogue(runtime_id, timeout=timeout)
        for runtime_id in sorted(set(launchable))
        if catalogue_command(runtime_id) is not None
    ]


def catalogue_summary(results: Sequence[CatalogueResult]) -> str:
    """One stable line per queried catalogue, for text output."""
    lines: list[str] = []
    for result in results:
        state = (
            "ok"
            if result.available
            else "unavailable"
            if result.supported
            else "unsupported"
        )
        counts = f", {len(result.models)} models, {len(result.free_models)} free"
        lines.append(
            f"{result.runtime}: {state} ({result.reason})"
            + (counts if result.available else "")
        )
    return "\n".join(lines)
