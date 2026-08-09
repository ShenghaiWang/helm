"""Root-local operator preferences.

Helm the product is generic. What one installation should default to -- which
agent runtime, which model, which runtimes are too expensive to start, which
model families may only run on certain runtimes -- is not generic: it is one
operator's decision about their own machine, their own credentials, and their
own bill. Committing those choices into the repository would ship them to
everybody who clones it, and hard-coding them into Helm would make a preference
look like a product invariant.

So they live in one file at the Helm root, ``preferences.json``, which is
ignored by Git. The tracked repository carries the schema, the mechanism, the
CLI, the docs and the tests; it never carries an operator's answers.

Three properties are load-bearing:

**A project cannot supply or weaken them.** The file is read from the Helm root
and nowhere else. A managed project's ``.helm/project.json`` is untrusted
guidance -- it may make a choice *more* specific (pin its own agent or model),
never wider, and it can neither add an exclusion nor remove one.

**It is not a credential store.** Every supported key is enumerated below and
every value passes the same narrow validator a runtime or model id passes
anywhere else in Helm. There is no free-text field, no ``env`` block, and no
passthrough map. An unknown key is an error naming the known keys rather than
data Helm carries around, which is what keeps ``helm prefs show`` from ever
becoming a way to print a secret somebody parked here.

**It is versioned.** ``version`` is required and is checked, so a file written
by a later build is refused with a clear message instead of being half
understood.
"""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Mapping

from . import runtimes

#: The file, always directly at the Helm root. `HELM_PREFERENCES_FILE` points
#: somewhere else, which is what the test suite uses; it is not a way for a
#: project to supply one, because a project never sets the coordinator's
#: environment.
PREFERENCES_FILENAME = "preferences.json"
PREFERENCES_ENV = "HELM_PREFERENCES_FILE"

#: Bumped only when an older document would be misread by this build. Loading
#: refuses a version it does not know rather than guessing at the difference.
PREFERENCES_VERSION = 1
SUPPORTED_VERSIONS = (1,)

#: These are small documents by construction -- a handful of ids. A file far
#: larger than that is not a preferences file, and reading it before finding
#: out is how a parser becomes a denial-of-service surface.
MAX_BYTES = 64 * 1024


class PreferencesError(Exception):
    """A preferences file that cannot be trusted to mean what it says."""


# ---------- the supported keys ----------
#
# One flat, dotted key per setting, because that is what the CLI takes and what
# the errors name. `model.runtimes.<family>` is the one templated key: its tail
# is a model-family id from the generic classifier in `runtimes`, so the set of
# valid keys grows with the classifier rather than with a second list here.

KEY_AGENT_DEFAULT = "agent.default"
KEY_AGENT_EXCLUDE = "agent.exclude"
KEY_MODEL_DEFAULT = "model.default"
KEY_MODEL_RUNTIMES = "model.runtimes"

#: key -> (takes a list?, one-line description). Printed by `helm prefs keys`
#: and quoted in every "unknown key" error, so this is the documentation.
SUPPORTED_KEYS: dict[str, tuple[bool, str]] = {
    KEY_AGENT_DEFAULT: (
        False,
        "root default agent runtime, below a task choice and a project pin",
    ),
    KEY_AGENT_EXCLUDE: (
        True,
        "agent runtimes this root will not start at all (a cost/safety limit)",
    ),
    KEY_MODEL_DEFAULT: (
        False,
        "root default model, below a task choice and a project pin",
    ),
    f"{KEY_MODEL_RUNTIMES}.<family>": (
        True,
        "restrict a model family to named runtimes; families: "
        + ", ".join(runtimes.model_family_ids()),
    ),
}


def key_help() -> list[str]:
    return [f"{key} -- {text}" for key, (_, text) in SUPPORTED_KEYS.items()]


def _unknown_key(key: str) -> PreferencesError:
    return PreferencesError(
        f"unknown preference key: {key}. Supported keys: "
        + ", ".join(SUPPORTED_KEYS)
    )


def split_model_runtimes_key(key: str) -> str | None:
    """Return the family a `model.runtimes.<family>` key names, or None."""
    prefix = f"{KEY_MODEL_RUNTIMES}."
    if not key.startswith(prefix):
        return None
    family = key[len(prefix) :]
    if family not in runtimes.model_family_ids():
        raise PreferencesError(
            f"unknown model family: {family}. Known families: "
            + ", ".join(runtimes.model_family_ids())
        )
    return family


def takes_list(key: str) -> bool:
    if key in SUPPORTED_KEYS:
        return SUPPORTED_KEYS[key][0]
    if split_model_runtimes_key(key) is not None:
        return True
    raise _unknown_key(key)


# ---------- the loaded value ----------


@dataclass(frozen=True)
class Preferences:
    """One root's operator preferences, already validated.

    ``present`` is False for a root with no file, and that is the shipped
    default: generic Helm imposes no operator choice at all. Every consumer
    therefore has to behave sensibly against an empty instance, which is what
    keeps "no preferences" a supported configuration rather than an untested
    one.
    """

    path: Path | None = None
    present: bool = False
    default_agent: str | None = None
    default_model: str | None = None
    excluded_agents: frozenset[str] = frozenset()
    model_runtimes: Mapping[str, frozenset[str]] = field(default_factory=dict)

    def constraint_for(self, model: str | None) -> tuple[str, frozenset[str]] | None:
        """The family restriction that applies to a model, if any is enabled.

        Absent a restriction this returns None, and nothing anywhere refuses a
        launch: the classifier in ``runtimes`` is generic technical metadata,
        and metadata alone never rejects anything. Rejection needs a root to
        have asked for it.
        """
        for family in runtimes.model_families(model):
            allowed = self.model_runtimes.get(family)
            if allowed:
                return family, allowed
        return None

    def document(self) -> dict[str, Any]:
        """The on-disk shape, omitting anything unset."""
        document: dict[str, Any] = {"version": PREFERENCES_VERSION}
        agent: dict[str, Any] = {}
        if self.default_agent:
            agent["default"] = self.default_agent
        if self.excluded_agents:
            agent["exclude"] = sorted(self.excluded_agents)
        if agent:
            document["agent"] = agent
        model: dict[str, Any] = {}
        if self.default_model:
            model["default"] = self.default_model
        if self.model_runtimes:
            model["runtimes"] = {
                family: sorted(allowed)
                for family, allowed in sorted(self.model_runtimes.items())
            }
        if model:
            document["model"] = model
        return document

    def entries(self) -> list[tuple[str, str]]:
        """Every set preference as (key, printable value), for `prefs show`.

        Built from the validated fields rather than from the raw document, so
        nothing that was not understood can be printed back out.
        """
        rows: list[tuple[str, str]] = []
        if self.default_agent:
            rows.append((KEY_AGENT_DEFAULT, self.default_agent))
        if self.excluded_agents:
            rows.append((KEY_AGENT_EXCLUDE, ", ".join(sorted(self.excluded_agents))))
        if self.default_model:
            rows.append((KEY_MODEL_DEFAULT, self.default_model))
        for family, allowed in sorted(self.model_runtimes.items()):
            rows.append((f"{KEY_MODEL_RUNTIMES}.{family}", ", ".join(sorted(allowed))))
        return rows


EMPTY = Preferences()


# ---------- locating, reading, validating ----------


def preferences_path(
    helm_root: Path | None, source: Mapping[str, str] | None = None
) -> Path | None:
    source = os.environ if source is None else source
    override = source.get(PREFERENCES_ENV, "").strip()
    if override:
        return Path(override).expanduser()
    if helm_root is None:
        return None
    return Path(helm_root) / PREFERENCES_FILENAME


def load(path: Path | None) -> Preferences:
    """Read and validate one preferences file; absence is not an error."""
    if path is None or not path.exists():
        return Preferences(path=path)
    if path.is_symlink():
        # Same rule as every other Helm-owned path: a symlink is a way to make
        # the root read a file somebody else controls.
        raise PreferencesError(f"preferences file must not be a symlink: {path}")
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise PreferencesError(f"cannot read preferences {path}: {exc}") from exc
    if len(raw) > MAX_BYTES:
        raise PreferencesError(
            f"preferences file is too large ({len(raw)} bytes, limit {MAX_BYTES}): {path}"
        )
    try:
        document = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise PreferencesError(f"cannot parse preferences {path}: {exc}") from exc
    return _from_document(document, path)


def _from_document(document: Any, path: Path | None) -> Preferences:
    where = f" in {path}" if path else ""
    if not isinstance(document, dict):
        raise PreferencesError(f"preferences must be a JSON object{where}")
    version = document.get("version")
    if version is None:
        raise PreferencesError(
            f"preferences must state a version{where}; this build writes "
            f"version {PREFERENCES_VERSION}"
        )
    if version not in SUPPORTED_VERSIONS:
        raise PreferencesError(
            f"unsupported preferences version {version!r}{where}; this build "
            f"understands {', '.join(str(item) for item in SUPPORTED_VERSIONS)}"
        )
    _reject_unknown(document, {"version", "agent", "model"}, "", where)

    agent = _object(document.get("agent"), "agent", where)
    _reject_unknown(agent, {"default", "exclude"}, "agent", where)
    default_agent = (
        _agent_id(agent["default"], "agent.default", where)
        if agent.get("default") is not None
        else None
    )
    excluded = frozenset(
        _agent_id(item, "agent.exclude", where)
        for item in _list(agent.get("exclude"), "agent.exclude", where)
    )

    model = _object(document.get("model"), "model", where)
    _reject_unknown(model, {"default", "runtimes"}, "model", where)
    default_model = (
        _model_id(model["default"], "model.default", where)
        if model.get("default") is not None
        else None
    )
    runtimes_by_family: dict[str, frozenset[str]] = {}
    families = _object(model.get("runtimes"), "model.runtimes", where)
    for family, allowed in families.items():
        if family not in runtimes.model_family_ids():
            raise PreferencesError(
                f"unknown model family model.runtimes.{family}{where}. Known "
                "families: " + ", ".join(runtimes.model_family_ids())
            )
        names = frozenset(
            _agent_id(item, f"model.runtimes.{family}", where)
            for item in _list(allowed, f"model.runtimes.{family}", where)
        )
        if not names:
            raise PreferencesError(
                f"model.runtimes.{family}{where} must name at least one runtime; "
                "an empty list would forbid every launch of that family rather "
                "than restricting it. Remove the key instead."
            )
        runtimes_by_family[family] = names

    return Preferences(
        path=path,
        present=True,
        default_agent=default_agent,
        default_model=default_model,
        excluded_agents=excluded,
        model_runtimes=runtimes_by_family,
    )


def _reject_unknown(
    section: Mapping[str, Any], known: set[str], prefix: str, where: str
) -> None:
    """Refuse a key nobody wrote support for, naming what is supported.

    An allowlist rather than an ignore, deliberately. Silently dropping an
    unrecognised field means a typo'd exclusion reads as "no exclusion", and a
    cost limit that quietly does nothing is worse than one that fails to load.
    It is also what stops this file accumulating a field somebody parks a
    credential in.
    """
    unknown = sorted(set(section) - known)
    if unknown:
        named = ", ".join(f"{prefix}.{key}" if prefix else key for key in unknown)
        supported = ", ".join(
            f"{prefix}.{key}" if prefix else key for key in sorted(known)
        )
        raise PreferencesError(
            f"unknown preference field(s){where}: {named}. Supported here: {supported}"
        )


def _object(value: Any, name: str, where: str) -> dict[str, Any]:
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise PreferencesError(f"{name}{where} must be an object")
    return value


def _list(value: Any, name: str, where: str) -> list[Any]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise PreferencesError(f"{name}{where} must be a list of strings")
    return value


def _agent_id(value: Any, name: str, where: str) -> str:
    try:
        return runtimes.validate_agent_id(value)
    except ValueError as exc:
        raise PreferencesError(f"{name}{where}: {exc}") from exc


def _model_id(value: Any, name: str, where: str) -> str:
    try:
        return runtimes.validate_model_id(value)
    except ValueError as exc:
        raise PreferencesError(f"{name}{where}: {exc}") from exc


# ---------- writing ----------


def _write_atomic(path: Path, document: Mapping[str, Any]) -> None:
    """Replace the file in one step, or leave the old one exactly as it was.

    Same reason Helm's state is written this way: a reader that catches a
    truncated file reads a root with no exclusions, which is a cost limit
    silently switched off for as long as the window lasts.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(
        dir=str(path.parent), prefix=f".{path.name}.", suffix=".tmp"
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(document, stream, indent=2, sort_keys=True)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
    except BaseException:
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise


def apply(current: Preferences, key: str, values: Iterable[str] | None) -> Preferences:
    """Return `current` with one key set to `values`, or unset when None.

    Pure, so the CLI validates the whole result before anything is written and
    a rejected value never lands half-applied on disk.
    """
    listed = None if values is None else [str(item) for item in values]
    if listed is not None and not listed:
        raise PreferencesError(f"{key} needs at least one value; use unset to remove it")
    if listed is not None and not takes_list(key) and len(listed) != 1:
        raise PreferencesError(f"{key} takes exactly one value")
    if listed is None:
        takes_list(key)  # reject an unknown key before pretending to remove it

    document = current.document()
    agent = dict(document.get("agent", {}))
    model = dict(document.get("model", {}))
    families = dict(model.get("runtimes", {}))

    family = split_model_runtimes_key(key)
    if family is not None:
        if listed is None:
            families.pop(family, None)
        else:
            families[family] = sorted(set(listed))
        if families:
            model["runtimes"] = families
        else:
            model.pop("runtimes", None)
    elif key == KEY_AGENT_DEFAULT:
        if listed is None:
            agent.pop("default", None)
        else:
            agent["default"] = listed[0]
    elif key == KEY_AGENT_EXCLUDE:
        if listed is None:
            agent.pop("exclude", None)
        else:
            agent["exclude"] = sorted(set(listed))
    elif key == KEY_MODEL_DEFAULT:
        if listed is None:
            model.pop("default", None)
        else:
            model["default"] = listed[0]
    else:
        raise _unknown_key(key)

    document["agent"] = agent
    document["model"] = model
    if not agent:
        document.pop("agent")
    if not model:
        document.pop("model")
    # Re-validate the whole document: the same path a hand-edited file takes,
    # so the CLI can never write something loading would then refuse.
    return _from_document(document, current.path)


def save(preferences: Preferences) -> Path:
    if preferences.path is None:
        raise PreferencesError(
            "no Helm root is configured, so there is nowhere to store preferences. "
            "Run helm init, or point HELM_PREFERENCES_FILE at a file."
        )
    _write_atomic(preferences.path, preferences.document())
    return preferences.path
