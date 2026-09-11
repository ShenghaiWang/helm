"""Resolving which runtime, model and effort a task runs under.

A mixin over `CoordinatorBase`, moved out of `core` unchanged. It resolves
every cross-call through `self` at runtime and imports nothing from
`helm.core`.
"""

from __future__ import annotations

import json
import os
import re
import shlex
import shutil
import subprocess
import sys

from pathlib import Path
from typing import Any, Sequence

from .. import models
from .. import preferences as prefs
from .. import runtimes
from ..errors import HelmError, SafetyError
from ..launching import _command_executable_available, worker_environment
from ..paths import canonical
from ..values import (
    RUNTIME_DEFAULT_MODEL,
    _safe_text,
    _string_list,
    _validate_agent_id,
    _validate_domain_id,
    _words,
)


class AgentsMixin:
    # ---------- configured agents ----------

    def _agent_root(self) -> Path | None:
        root = self.store.configured_root()
        if root is not None:
            return root / "agents"
        if self.store.directory.name == "state":
            return self.store.directory.parent / "agents"
        return None

    @staticmethod
    def _profile_entries(payload: Any, source: Path) -> list[dict[str, Any]]:
        if isinstance(payload, dict) and ("agents" in payload or "profiles" in payload):
            payload = payload.get("agents", payload.get("profiles"))
        if isinstance(payload, dict):
            entries = []
            for profile_id, profile in payload.items():
                if not isinstance(profile, dict):
                    raise HelmError(f"agent profile must be an object: {source}")
                entries.append({"id": profile_id, **profile})
            return entries
        if isinstance(payload, list) and all(isinstance(profile, dict) for profile in payload):
            return list(payload)
        raise HelmError(f"agent profiles must be a list or object: {source}")

    def _load_agent_profiles(self) -> list[dict[str, Any]]:
        root = self._agent_root()
        files: list[Path] = []
        allowed_root = root.parent if root is not None else None

        def add_file(candidate: Path, label: str) -> None:
            if allowed_root is not None:
                candidate = self._safe_configuration_path(candidate, allowed_root, label)
            elif candidate.is_symlink():
                raise SafetyError(f"agent configuration must not be a symlink without a Helm root: {candidate}")
            if candidate.is_file():
                files.append(candidate)

        configured_file = Path(os.environ["HELM_AGENTS_FILE"]).expanduser() if os.environ.get("HELM_AGENTS_FILE") else None
        if configured_file is not None:
            add_file(configured_file, "agent configuration")
        elif root is not None:
            root = self._safe_configuration_path(root, allowed_root, "Helm agents directory")
            add_file(root.parent / "agents.json", "agent configuration")
            add_file(root.parent / ".helm" / "agents.json", "agent configuration")
            if root.is_dir():
                for entry in sorted(root.iterdir()):
                    if entry.is_symlink():
                        self._safe_configuration_path(entry, allowed_root, "agent configuration")
                    if entry.is_file() and entry.suffix == ".json":
                        add_file(entry, "agent configuration")
                    elif entry.is_dir():
                        add_file(entry / "profile.json", "agent profile configuration")
        profiles: list[dict[str, Any]] = []
        seen: set[str] = set()
        for source in files:
            if not source.is_file():
                continue
            try:
                payload = json.loads(source.read_text(encoding="utf-8"))
            except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise HelmError(f"cannot read agent profiles {source}: {exc}") from exc
            for raw in self._profile_entries(payload, source):
                profile_id = raw.get("id")
                if not isinstance(profile_id, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}", profile_id):
                    raise HelmError(f"agent profile id is invalid: {source}")
                if profile_id in seen:
                    raise HelmError(f"duplicate agent profile: {profile_id}")
                seen.add(profile_id)
                domains = [_validate_domain_id(domain) for domain in _string_list(raw.get("domains", raw.get("domain")), f"agent {profile_id} domains")]
                capabilities = _string_list(raw.get("capabilities"), f"agent {profile_id} capabilities")
                capacity = raw.get("capacity", 1)
                if isinstance(capacity, bool) or not isinstance(capacity, int) or capacity < 1:
                    raise HelmError(f"agent {profile_id} capacity must be a positive integer")
                command = raw.get("command", raw.get("launch_command", raw.get("worker_command")))
                command_args: list[str] | None = None
                if command is not None:
                    if isinstance(command, str):
                        try:
                            command_args = shlex.split(command)
                        except ValueError as exc:
                            raise HelmError(f"invalid command for agent {profile_id}: {exc}") from exc
                    elif isinstance(command, list) and all(isinstance(item, str) for item in command):
                        command_args = list(command)
                    else:
                        raise HelmError(f"agent {profile_id} command must be a string or list of strings")
                check = raw.get("check_command", raw.get("availability_command"))
                check_args: list[str] | None = None
                if check is not None:
                    if isinstance(check, str):
                        try:
                            check_args = shlex.split(check)
                        except ValueError as exc:
                            raise HelmError(f"invalid availability check for agent {profile_id}: {exc}") from exc
                    elif isinstance(check, list) and all(isinstance(item, str) for item in check):
                        check_args = list(check)
                    else:
                        raise HelmError(f"agent {profile_id} availability check must be a string or list")
                runtime_id = raw.get("runtime", raw.get("agent"))
                if runtime_id is not None:
                    runtime_id = _validate_agent_id(runtime_id, str(source))
                    if runtimes.builtin_runtime(runtime_id) is None:
                        known = ", ".join(runtimes.builtin_runtime_ids())
                        raise HelmError(
                            f"agent {profile_id} names unknown runtime {runtime_id} "
                            f"(built in: {known}): {source}"
                        )
                env_passthrough = _string_list(
                    raw.get("env_passthrough"), f"agent {profile_id} env_passthrough"
                )
                for name in env_passthrough:
                    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name):
                        raise HelmError(
                            f"agent {profile_id} env_passthrough must name environment "
                            f"variables: {source}"
                        )
                profiles.append({
                    "id": profile_id,
                    "name": _safe_text(raw.get("name", profile_id)),
                    "domains": domains,
                    "capabilities": capabilities,
                    "capacity": capacity,
                    "command": command_args,
                    "check_command": check_args,
                    "runtime": runtime_id,
                    "env_passthrough": env_passthrough,
                    "source": str(source),
                })
        return sorted(profiles, key=lambda profile: profile["id"])

    def list_agent_profiles(self) -> list[dict[str, Any]]:
        """Return configured profiles without treating configuration as availability."""
        return self._load_agent_profiles()

    #: How long a runtime probe may take before Helm stops waiting. A probe is
    #: a real round-trip to the vendor, so this is generous rather than tight.
    PROBE_TIMEOUT_SECONDS = 60.0

    #: What an authentication failure looks like coming back from an agent CLI.
    #: Each vendor words it differently and none of them exit with a
    #: distinguishable code, so the text is all there is.
    _PROBE_AUTH_SIGNATURES = (
        "authentication",
        "auth error",
        "not logged in",
        "please log in",
        "log in again",
        "unauthorized",
        "invalid api key",
        "no credentials",
    )

    def probe_runtime(self, runtime_id: str) -> dict[str, Any]:
        """Ask a runtime to actually answer something, and report what happened.

        Executable-on-PATH is not availability.  On 2026-08-23 a foreman
        launched on a runtime whose binary was present, took its brief, and
        died on `Your stored authentication is invalid` -- while that CLI's own
        `status` command reported a successful login.  Helm had already
        recorded a live worker for it.  A PATH test cannot catch that and
        neither can the tool's own status command, so this runs the thing.

        THE VERDICT NEVER MARKS A RUNTIME UNAVAILABLE, and that is deliberate.
        The probe uses the non-interactive form, and a runtime can fail there
        while working perfectly in the interactive form Helm actually launches
        into a pane -- cursor did exactly that within an hour of the auth
        failure above, dying on `RetriableError: WritableIterable is closed`
        under `--print` while running fine interactively.  A probe that
        downgraded availability would have hidden a usable runtime, which is
        the worse error: an unavailable runtime you can still start costs a
        try, and an available one you were told not to costs the whole option.
        """
        runtime = runtimes.builtin_runtime(runtime_id)
        if runtime is None:
            raise HelmError(f"unknown runtime: {runtime_id}")
        command = list(runtime.command(interactive=False))
        if not command:
            return {"id": runtime_id, "verdict": "unprobeable",
                    "detail": "no non-interactive form to probe"}
        if not shutil.which(command[0]):
            return {"id": runtime_id, "verdict": "absent",
                    "detail": f"{command[0]} is not on PATH"}
        command = [
            "helm probe: reply with the single word OK and nothing else"
            if part == runtimes.PROMPT_PLACEHOLDER else part
            for part in command
        ]
        try:
            result = subprocess.run(
                command,
                capture_output=True,
                text=True,
                timeout=self.PROBE_TIMEOUT_SECONDS,
                check=False,
            )
        except subprocess.TimeoutExpired:
            return {"id": runtime_id, "verdict": "timeout",
                    "detail": f"no answer within {int(self.PROBE_TIMEOUT_SECONDS)}s"}
        except OSError as error:
            return {"id": runtime_id, "verdict": "failed", "detail": str(error)}
        output = f"{result.stdout}\n{result.stderr}".strip()
        lowered = output.lower()
        if any(signature in lowered for signature in self._PROBE_AUTH_SIGNATURES):
            return {"id": runtime_id, "verdict": "auth",
                    "detail": output.splitlines()[-1][:200] if output else "authentication rejected"}
        if result.returncode != 0:
            return {"id": runtime_id, "verdict": "failed",
                    "detail": (output.splitlines()[-1][:200] if output else f"exit {result.returncode}")}
        return {"id": runtime_id, "verdict": "ok", "detail": ""}

    def builtin_runtime_availability(self) -> list[dict[str, Any]]:
        """Report which built-in agent runtimes this machine can actually start.

        ``available`` is executable presence alone. It must not read as "this
        root will let you use it", so the same rows also carry the root's own
        verdicts: ``excluded`` when the root will not start the runtime at
        all, and ``default`` when `_root_agent_default` resolves to it --
        `HELM_AGENT`, this root's own `agent.default` preference, or (lacking
        either) the runtime this Helm session is itself detected to be
        running under. ``default_reason`` names which of the three it was, so
        a caller can tell a standing choice from a same-session guess. An
        excluded runtime stays *excluded*, never "unavailable" and never
        quietly "available", and it is never marked ``default`` either.
        """
        detected = runtimes.detect_runtime()
        integrations = runtimes.herdr_integration_status()
        excluded = self.excluded_agents()
        default_agent, default_reason = self._root_agent_default()
        result: list[dict[str, Any]] = []
        for runtime in runtimes.BUILTIN_RUNTIMES:
            valid, reason = self._check_command(runtime.command(interactive=True))
            integration = integrations.get(runtime.id) if integrations is not None else None
            result.append({
                "id": runtime.id,
                "name": runtime.name,
                "configured": False,
                "builtin": True,
                "available": valid,
                "excluded": runtime.id in excluded,
                # An excluded runtime never reads as the effective default:
                # dispatch refuses it outright (see `_default_agent_id`), so
                # a report that still marked it "default" would describe a
                # choice the root cannot actually act on.
                "default": runtime.id == default_agent and runtime.id not in excluded,
                "default_reason": default_reason if runtime.id == default_agent else "",
                "reason": reason,
                "detected": detected is not None and detected.id == runtime.id,
                "command": runtime.command(interactive=True),
                "herdr_integration": integration,
                "herdr_integrated": bool(
                    isinstance(integration, str) and integration.startswith("current")
                ),
            })
        return result

    def _launchable_runtime_ids(self) -> list[str]:
        """Built-in runtimes this machine can start, minus root exclusions.

        Deliberately not `builtin_runtime_availability`, which additionally
        shells out to `herdr integration status`: a dispatch-time decision
        only needs the executable-and-exclusion answer, and Herdr recognition
        says nothing about whether Helm can launch a runtime.
        """
        excluded = self.excluded_agents()
        launchable: list[str] = []
        for runtime in runtimes.BUILTIN_RUNTIMES:
            if runtime.id in excluded:
                continue
            valid, _ = self._check_command(runtime.command(interactive=True))
            if valid:
                launchable.append(runtime.id)
        return launchable

    #: The order a dispatcher must weigh evidence in, documented verbatim in
    #: the composed selection context, the CLI, and the model-selection domain.
    MODEL_SELECTION_ORDER = [
        "repository skills and requirements",
        "task capability tier",
        "live availability",
        "cost",
    ]

    #: The hard rules that bound a free-model preference. Evidence, never
    #: authority: nothing here substitutes a model or weakens a pin, exclusion,
    #: family restriction, or review-independence rule that core already enforces.
    MODEL_SELECTION_RULES = [
        "Fit is always filtered before cost: prefer a free model only among "
        "candidates judged competent for the task.",
        "Never force a weak model, never override a task, project, HELM_MODEL "
        "or model.default choice, never resurrect an excluded runtime, and "
        "never silently substitute a model.",
        "Classify free only from explicit catalogue evidence (`:free` marker "
        "in the id, or the opencode gateway's own `-free` ids); anything else "
        "is unknown cost and stays unknown.",
        "Uncertain work is never downgraded automatically. The dispatcher "
        "decides competence; Helm only supplies evidence.",
        "Reviewer independence and model-family/runtime restrictions are "
        "unchanged and outrank cost.",
    ]

    def model_selection_evidence(self) -> dict[str, Any]:
        """The live, bounded evidence a model decision may use.

        Read-only and deterministic apart from the catalogue query itself,
        which runs only when this root's `model.free` preference says `prefer`
        -- an operator who has not asked for cost awareness does not pay for
        the query. The returned document carries the preference, the decision
        order, the rules, and the explicitly-free ids the live catalogues of
        launchable, non-excluded runtimes reported, each with its provenance.
        It never names a recommended model: competence is judged by the
        dispatcher at dispatch time, not by this function.
        """
        preference = self.preferences().free_model
        evidence: dict[str, Any] = {
            "preference": (
                {"key": prefs.KEY_MODEL_FREE, "value": preference}
                if preference
                else None
            ),
            "decision_order": list(self.MODEL_SELECTION_ORDER),
            "rules": list(self.MODEL_SELECTION_RULES),
        }
        if preference != "prefer":
            return evidence
        results = models.query_launchable_catalogues(self._launchable_runtime_ids())
        evidence["catalogues"] = [
            {
                "runtime": result.runtime,
                "command": list(result.command),
                "available": result.available,
                "reason": result.reason,
                "model_count": len(result.models),
            }
            for result in results
            if result.supported
        ]
        evidence["free_evidence"] = [
            {"id": entry.id, "runtime": entry.runtime}
            for result in results
            for entry in result.models
            if entry.free
        ]
        return evidence

    def herdr_integration_availability(self) -> list[dict[str, Any]]:
        """Report Herdr-recognized agent kinds, including ones Helm cannot launch."""
        statuses = runtimes.herdr_integration_status()
        if statuses is None:
            return []
        builtins = set(runtimes.builtin_runtime_ids())
        return [
            {
                "id": agent_id,
                "status": status,
                "builtin": agent_id in builtins,
                "helm_launchable": agent_id in builtins or any(
                    profile["id"] == agent_id for profile in self._load_agent_profiles()
                ),
            }
            for agent_id, status in sorted(statuses.items())
        ]

    def agent_availability(self) -> list[dict[str, Any]]:
        """Check configured profiles without allocating a task or worktree."""
        data = self.store.load()
        profiles = self._load_agent_profiles()
        if not profiles:
            try:
                command = self._worker_command(None)
            except HelmError:
                # No configured profile and no override: the built-in runtimes
                # are what a task would actually be delegated to.
                return self.builtin_runtime_availability()
            valid, reason = self._check_command(command)
            return [{
                "id": "default",
                "name": "default",
                "configured": False,
                "available": valid,
                "reason": reason,
                "capacity": 1,
                "active": 0,
                "command": command,
            }]
        result: list[dict[str, Any]] = []
        for configured in profiles:
            profile = self._resolve_profile(configured, interactive=True)
            active = self._active_agent_count(data, profile["id"])
            valid, reason, actual = self._validate_agent_launch(profile, None)
            if self._capacity_exhausted(active, profile):
                valid = False
                reason = f"capacity exhausted ({self._capacity_text(active, profile)})"
            result.append({
                "id": profile["id"],
                "name": profile["name"],
                "configured": True,
                "available": valid,
                "reason": reason,
                "capacity": profile["capacity"],
                "active": active,
                "command": actual,
                "source": profile["source"],
            })
        return result

    @staticmethod
    def _capacity(profile: dict[str, Any]) -> int | None:
        """A profile's worker limit; ``None`` means the runtime sets none.

        A configured profile's capacity is a deliberate throttle. A built-in
        runtime is just a CLI, so it carries no limit of its own -- capping it
        at one would stop Helm running two workers at once, which is the point
        of delegating.
        """
        capacity = profile.get("capacity")
        return None if capacity is None else int(capacity)

    @classmethod
    def _capacity_exhausted(cls, active: int, profile: dict[str, Any]) -> bool:
        capacity = cls._capacity(profile)
        return capacity is not None and active >= capacity

    @classmethod
    def _capacity_text(cls, active: int, profile: dict[str, Any]) -> str:
        capacity = cls._capacity(profile)
        return f"{active}/{capacity}" if capacity is not None else f"{active}/unlimited"

    @staticmethod
    def _active_agent_count(data: dict[str, Any], profile_id: str) -> int:
        return sum(
            1
            for worker in data.get("workers", {}).values()
            if worker.get("agent_id", worker.get("agent")) == profile_id and worker.get("status") == "running"
        )

    @staticmethod
    def _check_command(command: Sequence[str], *, cwd: Path | None = None) -> tuple[bool, str]:
        available, reason = _command_executable_available(command, cwd=cwd)
        if not available:
            return False, reason
        return True, reason

    def _validate_agent_launch(
        self,
        profile: dict[str, Any],
        command: Sequence[str] | None,
        *,
        cwd: Path | None = None,
    ) -> tuple[bool, str, list[str] | None]:
        actual = list(command) if command else profile.get("command")
        if not actual and os.environ.get("HELM_WORKER_COMMAND"):
            actual = self._worker_command(None)
        if not actual:
            return False, "no launch command configured", None
        available, reason = self._check_command(actual, cwd=cwd)
        if not available:
            return False, reason, actual
        check = profile.get("check_command")
        if check:
            check_available, check_reason = self._check_command(check)
            if not check_available:
                return False, f"availability check unavailable: {check_reason}", actual
            try:
                result = subprocess.run(
                    check,
                    cwd=None,
                    env=worker_environment(),
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    timeout=3,
                    check=False,
                )
            except (OSError, subprocess.SubprocessError) as exc:
                return False, f"availability check failed: {exc}", actual
            if result.returncode != 0:
                return False, f"availability check exited {result.returncode}", actual
            reason = f"{reason}; live availability check passed"
        return True, reason, actual

    @staticmethod
    def _builtin_profile(runtime: runtimes.AgentRuntime, *, interactive: bool) -> dict[str, Any]:
        """Present a built-in runtime through the same shape as a profile."""
        return {
            "id": runtime.id,
            "name": runtime.name,
            "domains": [],
            "capabilities": [],
            "capacity": None,
            "command": runtime.command(interactive=interactive),
            "check_command": None,
            "env_passthrough": list(runtime.env_passthrough),
            "builtin": True,
            "source": "helm built-in runtime",
        }

    @staticmethod
    def _resolve_profile(profile: dict[str, Any], *, interactive: bool) -> dict[str, Any]:
        """Fill a profile's launch details from the runtime it names.

        A profile that spells out its own command keeps it; one that only says
        `"runtime": "codex"` inherits that runtime's argv and credential list,
        so pointing a domain at a different agent stays a one-line change.
        """
        named = profile.get("runtime")
        if not named and not profile.get("command"):
            # A profile called `codex` that supplies no command means the
            # built-in runtime of that name, not a missing configuration.
            named = profile.get("id")
        runtime = runtimes.builtin_runtime(named)
        if runtime is None:
            return profile
        resolved = dict(profile)
        if not resolved.get("command"):
            resolved["command"] = runtime.command(interactive=interactive)
        if not resolved.get("env_passthrough"):
            resolved["env_passthrough"] = list(runtime.env_passthrough)
        return resolved

    def _profile_for_agent_id(
        self,
        profiles: Sequence[dict[str, Any]],
        agent_id: str,
        *,
        interactive: bool,
    ) -> dict[str, Any]:
        """Resolve one named agent: a configured profile first, then a runtime."""
        for profile in profiles:
            if profile["id"] == agent_id:
                return self._resolve_profile(profile, interactive=interactive)
        runtime = runtimes.builtin_runtime(agent_id)
        if runtime is not None:
            return self._builtin_profile(runtime, interactive=interactive)
        known = sorted({*(profile["id"] for profile in profiles), *runtimes.builtin_runtime_ids()})
        raise HelmError(f"unknown agent: {agent_id} (known agents: {', '.join(known)})")

    def _default_agent_id(self, project: dict[str, Any]) -> tuple[str | None, str]:
        """Choose the runtime for a task nobody named an agent for.

        Most specific wins: the project's own pin, then a root default, and
        only then the runtime this Helm session appears to be running under.
        Detection is last because it is a guess, and it is a guess Helm makes
        instead of demanding configuration for the common case where every
        project should simply use the same agent as the coordinator.
        """
        pinned = self._project_agent(project)
        if pinned:
            return pinned, f"project {project['id']} pins agent {pinned}"
        return self._root_agent_default()

    def _root_agent_default(self) -> tuple[str | None, str]:
        """The runtime a task would get with no project pin in play.

        Shared by `_default_agent_id` (per-task resolution) and any
        root-level report that has no single project to consult: `HELM_AGENT`
        as a session override, then this root's own `agent.default`
        preference, then the runtime this Helm session appears to be running
        under. Detection is last because it is a guess.
        """
        configured = os.environ.get("HELM_AGENT", "").strip()
        if configured:
            if configured.lower() == "none":
                return None, "HELM_AGENT=none requires an explicitly named agent"
            return _validate_agent_id(configured, "HELM_AGENT"), "HELM_AGENT root default"
        preferred = self.preferences().default_agent
        if preferred:
            return preferred, f"root preferences default to agent {preferred}"
        detected = runtimes.detect_runtime()
        if detected is not None:
            return detected.id, f"same runtime as this Helm session ({detected.name})"
        return None, "no pinned, configured, or detectable agent runtime"

    def excluded_agents(self) -> set[str]:
        """Runtimes this root will not start at all.

        Which runtimes are worth paying for is the human's call and changes
        without Helm changing, so it lives in the root's own files rather than
        in this code. Helm ships no exclusion at all: a clone of this
        repository starts every runtime it can find.

        Three sources, in precedence order. `HELM_EXCLUDE_AGENTS` is a session
        override and replaces the rest outright, including with an empty value,
        so a deliberate one-off run is possible. Otherwise the root's
        `preferences.json` and any legacy `state.config` entry are *unioned* --
        a cost limit is a restriction, and merging restrictions is the only
        combination that cannot accidentally widen one. A project file appears
        nowhere in this list and cannot.

        This is deliberately about *starting* a runtime, not about reviewing.
        Scoping it to reviews left the expensive runtime one `--agent` away, or
        one project pin, or one lucky detection -- and a cost policy that only
        covers the path somebody happened to notice is not a policy.
        """
        raw = os.environ.get("HELM_EXCLUDE_AGENTS")
        if raw is None:
            raw = os.environ.get("HELM_REVIEW_EXCLUDE_AGENTS")
        if raw is not None:
            return {part.strip() for part in raw.split(",") if part.strip()}
        excluded = set(self.preferences().excluded_agents)
        config = self.store.load().get("config", {})
        config = config if isinstance(config, dict) else {}
        # `review_exclude_agents` is the narrower name this policy was first
        # written under; roots configured either way keep working untouched,
        # and `helm prefs migrate` moves them into the file when they are ready.
        configured = config.get("excluded_agents", config.get("review_exclude_agents"))
        if isinstance(configured, list):
            legacy = ",".join(str(item) for item in configured)
        else:
            legacy = str(configured or "")
        excluded.update(part.strip() for part in legacy.split(",") if part.strip())
        return excluded

    def legacy_excluded_agents(self) -> set[str]:
        """Exclusions still held in `state.config`, for the migration path."""
        config = self.store.load().get("config", {})
        config = config if isinstance(config, dict) else {}
        configured = config.get("excluded_agents", config.get("review_exclude_agents"))
        if isinstance(configured, list):
            raw = ",".join(str(item) for item in configured)
        else:
            raw = str(configured or "")
        return {part.strip() for part in raw.split(",") if part.strip()}

    def review_excluded_agents(self) -> set[str]:
        """Back-compatible alias; the exclusion is no longer review-only."""
        return self.excluded_agents()

    def pick_reviewer_agent(
        self,
        author_agent_id: str | None,
        *,
        explicit: str | None = None,
        model: str | None = None,
        interactive: bool = True,
        exclude: list[str] | None = None,
    ) -> dict[str, Any]:
        """Choose something other than the author to review the author's work.

        An agent reviewing its own output re-runs the reasoning that produced
        the bug, so independence is the whole point. Preference order matches
        the `code-review` domain: a different runtime, then a different model
        on the same runtime, and a same-model review only when it is labelled
        as the weak check it is.

        ``exclude`` rules out runtimes for this pick alone -- the retry after a
        reviewer dies on infrastructure passes the one that just died, so the
        second attempt is not the same runtime failing the same way. It is the
        caller's situational judgement rather than the root's cost policy, so
        it narrows the automatic search only; an `explicit` choice is still
        refused solely by the root's own exclusions, whose message tells the
        commander to edit a preference that would have nothing to do with this.
        """
        # The runtime-default sentinel means "pass no model", and it has to be
        # turned into None HERE, before anything below reads it. This path
        # builds the reviewer's command directly through `with_model` rather
        # than through the resolution ladder, so the sentinel that the ladder
        # understands would otherwise be baked into argv as a literal
        # `--model runtime` -- the exact shape of failure it exists to prevent.
        if model == RUNTIME_DEFAULT_MODEL:
            model = None
        excluded = self.review_excluded_agents()
        situational = {name for name in (exclude or []) if name}
        if explicit is not None:
            if explicit in excluded:
                # A cost policy an agent could route around by naming the
                # runtime explicitly would not be a policy.  Changing it is a
                # human edit to the root's config, not a flag on one review.
                raise HelmError(
                    f"reviewer {explicit} is excluded from reviews in this Helm root. "
                    f"Run `helm prefs set agent.exclude ...` without {explicit} (or "
                    "set HELM_REVIEW_EXCLUDE_AGENTS) to allow it again."
                )
            self._require_model_runtime(model, explicit, f"explicit reviewer {explicit}")
            runtime = runtimes.builtin_runtime(explicit)
            command = (
                runtime.with_model(model, interactive=interactive) if runtime else None
            )
            independence = "different-runtime" if explicit != author_agent_id else (
                "different-model" if model else "same-agent"
            )
            return {
                "agent": explicit,
                "command": command,
                "independence": independence,
                "reason": f"explicit reviewer {explicit}"
                + (f" on model {model}" if model else ""),
            }
        # THE ROOT'S STANDING ANSWER TO "WHO CHECKS THE WORK", below an
        # explicit --reviewer-agent and above the automatic search. Without it
        # the choice had to be repeated to every foreman, and was lost whenever
        # one died mid-instruction: three reviews in one afternoon landed on a
        # runtime the commander had not asked for, each time silently, because
        # a fallen-through default looks identical to a considered pick.
        #
        # It is a preference, not an override. A preferred runtime that is
        # excluded, unavailable, situationally ruled out, or would be the
        # author's own is skipped and the ordinary search runs -- independence
        # is the point of the whole function and a convenience must not buy
        # past it.
        preferred = self.preferences().review_agent
        if (
            preferred
            and preferred != author_agent_id
            and preferred not in excluded
            and preferred not in situational
            and any(
                entry["id"] == preferred and entry["available"]
                for entry in self.builtin_runtime_availability()
            )
        ):
            try:
                self._require_model_runtime(model, preferred, f"reviewer {preferred}")
            except HelmError:
                pass  # A restricted model cannot run here; fall through.
            else:
                runtime = runtimes.builtin_runtime(preferred)
                return {
                    "agent": preferred,
                    "command": (
                        runtime.with_model(model, interactive=interactive)
                        if runtime
                        else None
                    ),
                    "independence": "different-runtime",
                    "reason": f"root review.agent preference ({preferred})"
                    + (f" on model {model}" if model else ""),
                }
        available = [
            entry["id"]
            for entry in self.builtin_runtime_availability()
            if entry["available"]
            and entry["id"] not in excluded
            and entry["id"] not in situational
        ]
        constraint = self.preferences().constraint_for(model)
        if constraint is not None:
            # Independence is chosen from what may actually run this model, so
            # a restricted reviewer model never quietly lands on a
            # cross-provider runtime that would happily accept the name and
            # bill for it.
            family, permitted = constraint
            available = [
                candidate for candidate in available if candidate in permitted
            ]
            if not available and author_agent_id not in permitted:
                assert model is not None
                raise HelmError(
                    runtimes.family_pairing_error(
                        model,
                        family,
                        permitted,
                        None,
                        "no installed reviewer runtime may run that model family",
                    )
                )
        for candidate in available:
            if candidate != author_agent_id:
                runtime = runtimes.builtin_runtime(candidate)
                return {
                    "agent": candidate,
                    "command": runtime.with_model(model, interactive=interactive),
                    "independence": "different-runtime",
                    "reason": f"{candidate} is installed and is not the author ({author_agent_id})",
                }
        runtime = runtimes.builtin_runtime(author_agent_id)
        if runtime is not None and model:
            self._require_model_runtime(
                model, author_agent_id, "it is the only installed runtime"
            )
            return {
                "agent": author_agent_id,
                "command": runtime.with_model(model, interactive=interactive),
                "independence": "different-model",
                "reason": (
                    f"only {author_agent_id} is installed; reviewing on model {model} "
                    "instead of a different runtime"
                ),
            }
        raise HelmError(
            "no independent reviewer is available: the only installed runtime is the "
            f"author's ({author_agent_id})."
            + (
                f" Excluded from reviews in this root: {', '.join(sorted(excluded))}."
                if excluded
                else ""
            )
            + f" Install a second runtime ({', '.join(runtimes.builtin_runtime_ids())}) "
            "or pass --reviewer-model so the review is at least run by a different model."
        )

    def _select_agent(
        self,
        data: dict[str, Any],
        project: dict[str, Any],
        task: dict[str, Any],
        command: Sequence[str] | None,
        explicit: str | None = None,
        *,
        interactive: bool = True,
    ) -> dict[str, Any]:
        profiles = self._load_agent_profiles()
        excluded = self.excluded_agents()
        model, model_reason = self._resolve_model(project, task)
        if explicit is not None and explicit in excluded:
            # Refused rather than substituted: silently running the task on a
            # different runtime would hide that the request was overridden.
            raise HelmError(
                f"agent {explicit} is excluded from this Helm root and will not be "
                f"started. Run `helm prefs set agent.exclude ...` without {explicit} "
                "(or set HELM_EXCLUDE_AGENTS) to allow it again."
            )
        if explicit is not None:
            profile = self._profile_for_agent_id(profiles, explicit, interactive=interactive)
            valid, reason, actual = self._validate_agent_launch(
                profile, command, cwd=canonical(task["workspace"])
            )
            active = self._active_agent_count(data, explicit)
            if self._capacity_exhausted(active, profile):
                valid = False
                reason = f"capacity exhausted ({self._capacity_text(active, profile)})"
            if not valid:
                raise HelmError(f"agent {explicit} is unavailable: {reason}")
            selection_reason = (
                f"explicit --agent override; {reason}; "
                f"capacity {self._capacity_text(active + 1, profile)}"
                + (f"; {model_reason}" if model_reason else "")
            )
            return {
                "id": explicit,
                "name": profile["name"],
                "reason": selection_reason,
                "command": self._with_model(profile, actual, model, model_reason),
                "model": model,
                "profile": profile,
            }

        # An explicit command, then the advanced ambient override, then a
        # named default, then configured matching, then detection.  Anything a
        # caller stated outranks anything Helm inferred.
        if command or (not profiles and os.environ.get("HELM_WORKER_COMMAND")):
            actual = list(command) if command else self._worker_command(None)
            valid, reason = self._check_command(actual, cwd=canonical(task["workspace"]))
            if not valid:
                raise HelmError(f"default worker command is unavailable: {reason}")
            default_profile = {"id": "default", "name": "default", "capacity": 1, "domains": [], "capabilities": []}
            return {
                "id": "default",
                "name": "default",
                "reason": f"caller-supplied worker command ({reason})",
                # A command Helm did not build has no known model flag, so a
                # resolved model is refused here rather than dropped.
                "command": self._with_model(default_profile, actual, model, model_reason),
                "model": model,
                "profile": default_profile,
            }

        named = self._project_agent(project)
        named_reason = f"project {project['id']} pins agent {named}" if named else ""
        if named is None and not profiles:
            # Configured profiles mean an operator asked for Helm's matching.
            # Only reach for a root default or this session's own runtime when
            # there is nothing configured to match against.
            named, named_reason = self._default_agent_id(project)
        if named is not None and named in excluded:
            # A pin, a root default, or detection landing on an excluded
            # runtime is still an attempt to start it. Say which source chose
            # it, because that is the thing the human has to go and change.
            raise HelmError(
                f"agent {named} is excluded from this Helm root and will not be "
                f"started, but was selected because {named_reason or 'it was the default'}. "
                "Name a different agent, or drop it from this root's agent.exclude "
                "preference."
            )
        if named is not None:
            profile = self._profile_for_agent_id(profiles, named, interactive=interactive)
            valid, reason, actual = self._validate_agent_launch(
                profile, None, cwd=canonical(task["workspace"])
            )
            active = self._active_agent_count(data, named)
            if self._capacity_exhausted(active, profile):
                valid = False
                reason = f"capacity exhausted ({self._capacity_text(active, profile)})"
            if not valid:
                raise HelmError(f"agent {named} is unavailable: {reason}")
            return {
                "id": named,
                "name": profile["name"],
                "reason": f"{named_reason}; {reason}"
                + (f"; {model_reason}" if model_reason else ""),
                "command": self._with_model(profile, actual, model, model_reason),
                "model": model,
                "profile": profile,
            }

        if not profiles:
            raise HelmError(
                "no worker runtime is available: no agent profile is configured, no project "
                "pins one, and this session's runtime could not be detected. Name one with "
                f"--agent (built in: {', '.join(runtimes.builtin_runtime_ids())}), pin one in "
                ".helm/project.json, or set HELM_AGENT."
            )

        domain = task.get("domain")
        task_words = _words(task.get("brief", ""))
        candidates: list[dict[str, Any]] = []
        unavailable: list[str] = []
        for configured in profiles:
            profile = self._resolve_profile(configured, interactive=interactive)
            active = self._active_agent_count(data, profile["id"])
            if self._capacity_exhausted(active, profile):
                unavailable.append(
                    f"{profile['id']} capacity {self._capacity_text(active, profile)}"
                )
                continue
            valid, reason, actual = self._validate_agent_launch(
                profile, command, cwd=canonical(task["workspace"])
            )
            if not valid:
                unavailable.append(f"{profile['id']} {reason}")
                continue
            profile_domains = {value.lower() for value in profile["domains"]}
            domain_match = bool(domain and str(domain).lower() in profile_domains)
            matched_capabilities = [
                capability
                for capability in profile["capabilities"]
                if capability.lower() in task_words or capability.lower().rstrip("s") in task_words
            ]
            capability_matches = len(matched_capabilities)
            candidates.append({
                "id": profile["id"],
                "name": profile["name"],
                "profile": profile,
                "command": actual,
                "active": active,
                "domain_match": int(domain_match),
                "capability_matches": capability_matches,
                "matched_capabilities": matched_capabilities,
                "remaining": (
                    sys.maxsize if self._capacity(profile) is None
                    else self._capacity(profile) - active
                ),
                "availability_reason": reason,
            })
        if not candidates:
            detail = "; ".join(unavailable) if unavailable else "no profiles passed validation"
            raise HelmError(f"no available configured agent profile: {detail}")
        # Profiles are loaded in lexical ID order; max() keeps the first
        # candidate on a complete score tie.
        selected = max(
            candidates,
            key=lambda item: (
                item["domain_match"],
                item["capability_matches"],
                item["remaining"],
                -item["active"],
            ),
        )
        domain_text = "domain match" if selected["domain_match"] else "no domain match"
        capability_text = (
            f"{selected['capability_matches']} task capability match(es)"
            + (f" [{', '.join(selected['matched_capabilities'])}]" if selected["matched_capabilities"] else "")
        )
        reason = (
            f"selected from configured available profiles by {domain_text}, {capability_text}, "
            f"capacity {self._capacity_text(selected['active'] + 1, selected['profile'])}; "
            f"{selected['availability_reason']}"
        )
        selected["reason"] = reason + (f"; {model_reason}" if model_reason else "")
        selected["command"] = self._with_model(
            selected["profile"], selected["command"], model, model_reason
        )
        selected["model"] = model
        return selected
