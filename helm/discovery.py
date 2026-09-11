"""What a project's own files, and a configured profile, actually declare.

Both functions here were static methods on `Coordinator` that never touched
an instance -- they take a path or a dict and return a value. They are read
by the skills and launch-resolution code, which is why they have to sit
below it rather than on the class it lives on.
"""

from __future__ import annotations

import json
import re
import time
from pathlib import Path
from typing import Any, Sequence

from . import runtimes
from .errors import HelmError
from .paths import _safe_configuration_path, inside
from .values import (
    DELIVERY_POLICIES,
    _safe_text,
    _string_list,
    _validate_agent_id,
    _validate_branch_name,
    _validate_domain_id,
    _validate_model_id,
)


def _discovery_settings(project_root: Path) -> dict[str, Any]:
    """Read optional per-project defaults without changing the project."""
    settings_file = project_root / ".helm" / "project.json"
    settings_file = _safe_configuration_path(
        settings_file, project_root, "project settings"
    )
    if not settings_file.exists():
        return {}
    try:
        settings = json.loads(settings_file.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise HelmError(f"cannot read project settings {settings_file}: {exc}") from exc
    if not isinstance(settings, dict):
        raise HelmError(f"project settings must be a JSON object: {settings_file}")
    result: dict[str, Any] = {}
    if "delivery_policy" in settings:
        policy = settings["delivery_policy"]
        if policy not in DELIVERY_POLICIES:
            raise HelmError(
                f"project settings delivery_policy must be 'local' or 'pr': {settings_file}"
            )
        result["delivery_policy"] = policy
    if "label" in settings or "name" in settings:
        label = _safe_text(settings.get("label", settings.get("name"))).strip()
        if not label:
            raise HelmError(f"project settings label must not be empty: {settings_file}")
        result["label"] = label
    if "color" in settings:
        color = settings["color"]
        if not isinstance(color, str) or not re.fullmatch(r"#[0-9A-Fa-f]{6}", color):
            raise HelmError(
                f"project settings color must be a six-digit hex value: {settings_file}"
            )
        result["color"] = color
    domain_value = settings.get("domains", settings.get("default_domains", settings.get("domain")))
    if domain_value is not None:
        domains = _string_list(domain_value, f"project settings domains: {settings_file}")
        result["domains"] = [_validate_domain_id(domain) for domain in domains]
    # A project may pin the runtime its workers run under -- one project
    # on Codex while the rest follow this session -- without naming it on
    # every request.  It names a runtime; it never supplies a command.
    # Directories whose contents are build outputs -- renders, clips --
    # that git ignores and a merge therefore cannot carry out of the task
    # worktree. Naming them here is what lets Helm deliver them.
    deliver_value = settings.get("deliver", settings.get("deliver_paths"))
    if deliver_value is not None:
        entries = _string_list(deliver_value, f"project settings deliver: {settings_file}")
        cleaned: list[str] = []
        for entry in entries:
            candidate = entry.strip().strip("/")
            if not candidate or candidate.startswith("/") or ".." in Path(candidate).parts:
                raise HelmError(
                    f"project settings deliver must be relative paths inside the project: {settings_file}"
                )
            cleaned.append(candidate)
        result["deliver"] = cleaned
    agent_value = settings.get("agent", settings.get("default_agent", settings.get("runtime")))
    if agent_value is not None:
        if not isinstance(agent_value, str) or not agent_value.strip():
            raise HelmError(f"project settings agent must be a non-empty string: {settings_file}")
        result["agent"] = _validate_agent_id(agent_value.strip(), str(settings_file))
    # A project may also pin the model its workers run on, separately from
    # the runtime: "this project is mechanical, run it cheap" is a
    # different statement from "this project runs on Codex", and either can
    # be made without the other.
    model_value = settings.get("model", settings.get("default_model"))
    if model_value is not None:
        if not isinstance(model_value, str) or not model_value.strip():
            raise HelmError(f"project settings model must be a non-empty string: {settings_file}")
        result["model"] = _validate_model_id(model_value.strip(), str(settings_file))
    # Every project gets a foreman by default; this is how one declines.
    # It is a boolean on purpose: the project says whether it wants a
    # driver, and nothing about what that driver may do -- authority is
    # Helm's, and a project file is untrusted guidance.
    if "foreman" in settings:
        wants = settings["foreman"]
        if not isinstance(wants, bool):
            raise HelmError(f"project settings foreman must be true or false: {settings_file}")
        result["foreman"] = wants
    # Review is the same shape, and exists for the same reason a prose
    # line in knowledge.md is not enough: guidance is weighed, a setting
    # is enforced. A commander who has ruled that a project's rounds run
    # without independent review records it here once, and Helm refuses
    # the reviewer task -- instead of every future foreman having to
    # weigh a sentence against its own brief and sometimes launching a
    # reviewer the commander already declined. Like foreman, it says
    # only whether this project wants the loop, never what any agent may
    # do -- authority stays Helm's.
    if "review" in settings:
        wants = settings["review"]
        if not isinstance(wants, bool):
            raise HelmError(f"project settings review must be true or false: {settings_file}")
        result["review"] = wants
    # A project may name its own base branch explicitly rather than
    # leaving Helm to infer one from the checkout. This is the only
    # branch name accepted from project data without also verifying it
    # resolves in the repository -- that check happens where the branch
    # is actually used, so a rename or a typo fails with the task it
    # would have affected, not silently at discovery time.
    if "base_branch" in settings:
        result["base_branch"] = _validate_branch_name(
            settings["base_branch"], str(settings_file)
        )
    # A project may pin, allow, or deny its own task-varying skills. It is
    # guidance about that project's own files and nothing more: a skill
    # list cannot name another project, and it never widens what Helm may
    # do -- a denied skill is simply never offered to a worker.
    if "skills" in settings:
        declared = settings["skills"]
        if not isinstance(declared, dict):
            raise HelmError(
                f"project settings skills must be a JSON object: {settings_file}"
            )
        chosen: dict[str, list[str]] = {}
        for key in ("pin", "allow", "deny"):
            if key not in declared:
                continue
            value = declared[key]
            if not isinstance(value, list) or any(
                not isinstance(entry, str) for entry in value
            ):
                raise HelmError(
                    f"project settings skills.{key} must be a list of strings: "
                    f"{settings_file}"
                )
            chosen[key] = [entry.strip() for entry in value if entry.strip()]
        result["skills"] = chosen
    return result


def _launch_runtime_id(profile: dict[str, Any], command: Sequence[str]) -> str | None:
    """Name the runtime a command will actually start, when it is knowable.

    argv[0] wins whenever it names a runtime Helm knows, because that is
    the program that will actually run: a profile is metadata, and metadata
    claiming to be `claude` over a command that starts `pi` would hand the
    boundary the one answer that lets the launch through. Profile metadata
    is consulted only where the executable says nothing -- an opaque
    wrapper -- and an unrecognized program is treated as evidence of
    nothing at all rather than as evidence of Claude Code.
    """
    if command:
        executable = Path(str(command[0])).name
        if runtimes.builtin_runtime(executable) is not None:
            return executable
    named = profile.get("runtime") or profile.get("id")
    if runtimes.builtin_runtime(named) is not None:
        return str(named)
    return None


def _parse_frontmatter(text: str) -> dict[str, Any]:
    """Read a small YAML-ish frontmatter block: scalars and simple lists.

    Deliberately not a YAML parser. Domain metadata is a handful of strings and
    string lists, and a real YAML dependency would buy nothing but the ability
    to express things a domain header should not contain.
    """
    if not text.startswith("---"):
        return {}
    end = text.find("\n---", 3)
    if end == -1:
        return {}
    meta: dict[str, Any] = {}
    key: str | None = None
    for raw in text[3:end].splitlines():
        line = raw.rstrip()
        if not line.strip() or line.strip().startswith("#"):
            continue
        if line.lstrip().startswith("- ") and key:
            meta.setdefault(key, [])
            if isinstance(meta[key], list):
                meta[key].append(line.lstrip()[2:].strip().strip('"\''))
            continue
        if ":" not in line:
            continue
        key, _, value = line.partition(":")
        key = key.strip()
        value = value.strip().strip('"\'')
        if value.lower() in {"true", "false"}:
            meta[key] = value.lower() == "true"
        elif value:
            meta[key] = value
        else:
            meta[key] = []
    return meta
