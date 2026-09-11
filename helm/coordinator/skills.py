"""The task-varying half of the Coordinator: domains, skills, and resolution.

A mixin over `CoordinatorBase`, moved out of `core` unchanged. Like the
status mixin it resolves every cross-call through `self` at runtime, so it
imports nothing from `helm.core` and no call site had to change.
"""

from __future__ import annotations

import contextlib
import copy
import json
import os
import re
import stat
import time
from pathlib import Path
from typing import Any, Sequence

from .. import models
from .. import preferences as prefs
from .. import runtimes
from ..discovery import _discovery_settings, _launch_runtime_id, _parse_frontmatter
from ..errors import HelmError, SafetyError
from .. import git
from ..git import _StaleBaseResolution, _git, _git_root, _has_head
from ..paths import _private_dir, _safe_configuration_path, canonical, inside, overlaps
from ..state import StateStore
from ..values import (
    DELIVERY_POLICIES,
    GATE_TYPES,
    RUNTIME_DEFAULT_MODEL,
    PORTABLE_SKILL_ROOT,
    RUNTIME_SKILL_ROOTS,
    SKILL_CONTENT_LIMIT,
    SKILL_MANIFEST,
    SKILL_SELECTION_LIMIT,
    SKILL_TOTAL_LIMIT,
    TASK_ROLES,
    WORKTREELESS_ROLES,
    _MAX_DOMAIN_DEPTH,
    _ROLE_DIRECTORY,
    _SKILL_STOPWORDS,
    _safe_text,
    _string_list,
    _validate_agent_id,
    _validate_branch_name,
    _validate_domain_id,
    _validate_effort,
    _validate_model_id,
    _validate_project_id,
    _validate_ticket_id,
    new_id,
    now,
    task_branch_name,
)


class SkillsMixin:
    # ---------- task-varying skills ----------

    @staticmethod
    def _skill_roots(runtime: str | None) -> list[tuple[str, str, str]]:
        """(relative root, kind, owning runtime) for one runtime's discovery.

        The portable root is readable by every agent. A runtime root belongs to
        the runtime that defined it, so it is read only when that runtime is
        the one about to be launched -- reading another agent's root would
        offer a worker conventions written for a harness it is not running in.
        """
        roots = [(PORTABLE_SKILL_ROOT, "portable", "")]
        name = (runtime or "").strip()
        if name in RUNTIME_SKILL_ROOTS:
            roots.append((RUNTIME_SKILL_ROOTS[name], "runtime", name))
        return roots

    def _read_skill_manifest(
        self, manifest: Path, project_root: Path
    ) -> tuple[dict[str, Any] | None, str]:
        """Read one `SKILL.md`, or say what is wrong with it. Never guesses.

        A skill with no description is not given one: the description is the
        only evidence selection has that the skill bears on a task, and
        inventing it would make an unrelated skill look relevant.
        """
        if manifest.is_symlink() or manifest.parent.is_symlink():
            return None, "is a symlink, which could point outside the project"
        try:
            resolved = self._safe_configuration_path(
                manifest, project_root, "project skill"
            )
        except SafetyError:
            return None, "resolves outside the project root"
        if not resolved.is_file():
            return None, "has no SKILL.md"
        try:
            raw = resolved.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            return None, f"could not be read: {exc}"
        meta = _parse_frontmatter(raw)
        if not isinstance(meta, dict) or not meta:
            return None, "has no readable frontmatter (needs name and description)"
        description = _safe_text(meta.get("description", "")).strip()
        if not description:
            return None, "declares no description, so nothing says when it applies"
        return (
            {
                "name": _safe_text(meta.get("name", "")).strip(),
                "description": description,
                "body": raw,
            },
            "",
        )

    def discover_skills(
        self, project: dict[str, Any], runtime: str | None = None
    ) -> dict[str, Any]:
        """Every readable skill in one project, plus what could not be read.

        Reads the selected project and nothing else: a skill is a fact about
        the repository it sits in, and one project's conventions are never
        evidence about another's.
        """
        project_root = canonical(project["root"])
        skills: dict[str, dict[str, Any]] = {}
        problems: list[dict[str, str]] = []
        for relative, kind, owner in self._skill_roots(runtime):
            root = project_root / relative
            if root.is_symlink():
                problems.append({
                    "id": "", "root": relative,
                    "problem": "skill root is a symlink, which could point outside the project",
                })
                continue
            if not root.is_dir():
                continue
            for entry in sorted(root.iterdir()):
                if not entry.is_dir() or entry.is_symlink():
                    if entry.is_symlink():
                        problems.append({
                            "id": entry.name, "root": relative,
                            "problem": "skill directory is a symlink",
                        })
                    continue
                manifest, problem = self._read_skill_manifest(
                    entry / SKILL_MANIFEST, project_root
                )
                if manifest is None:
                    problems.append(
                        {"id": entry.name, "root": relative, "problem": problem}
                    )
                    continue
                record = {
                    "id": entry.name,
                    "name": manifest["name"] or entry.name,
                    "description": manifest["description"],
                    "path": f"{relative}/{entry.name}/{SKILL_MANIFEST}",
                    "root": relative,
                    "kind": kind,
                    "runtime": owner,
                    "body": manifest["body"],
                    "duplicate_of": "",
                }
                existing = skills.get(entry.name)
                if existing is None:
                    skills[entry.name] = record
                    continue
                # One skill, present twice. The runtime-specific copy is the
                # more specific answer for the runtime about to run, so it
                # wins -- and the fact that there were two is recorded rather
                # than quietly resolved.
                if kind == "runtime":
                    record["duplicate_of"] = existing["path"]
                    skills[entry.name] = record
                else:
                    existing["duplicate_of"] = record["path"]
        return {
            "skills": [skills[key] for key in sorted(skills)],
            "problems": problems,
            "roots": [relative for relative, _, _ in self._skill_roots(runtime)],
            "runtime": (runtime or "").strip(),
        }

    @staticmethod
    def _skill_terms(text: str) -> set[str]:
        """Comparable words from a brief or a skill description.

        A trailing plural is folded away, and nothing more. "Write a migration"
        must find the skill described as "database migrations" -- but a real
        stemmer would start matching words that merely look alike, and a skill
        selected on a coincidence is worse than one a driver has to pin.
        """
        terms = set()
        for word in re.findall(r"[a-z0-9]+", _safe_text(text).lower()):
            if len(word) < 3 or word in _SKILL_STOPWORDS:
                continue
            if len(word) > 3 and word.endswith("s") and not word.endswith("ss"):
                word = word[:-1]
            if word not in _SKILL_STOPWORDS:
                terms.add(word)
        return terms

    def _skill_settings(self, project: dict[str, Any]) -> dict[str, list[str]]:
        settings = self._discovery_settings(canonical(project["root"])).get("skills")
        result: dict[str, list[str]] = {"pin": [], "allow": [], "deny": []}
        if not isinstance(settings, dict):
            return result
        for key in result:
            value = settings.get(key)
            if isinstance(value, list):
                result[key] = [_safe_text(v).strip() for v in value if _safe_text(v).strip()]
        return result

    def select_skills(
        self,
        project: dict[str, Any],
        task: dict[str, Any],
        runtime: str | None = None,
        *,
        pin: Sequence[str] | None = None,
    ) -> dict[str, Any]:
        """Choose the skills this task actually needs, and say why.

        Conservative on purpose. A pin is an explicit instruction and is taken
        at its word; everything else has to earn its place by its own declared
        description overlapping this brief. Selecting the whole directory
        because it was there is how a worker's context fills with instructions
        for work it is not doing.
        """
        found = self.discover_skills(project, runtime)
        configured = self._skill_settings(project)
        pins = [*(configured["pin"]), *[_safe_text(p).strip() for p in (pin or []) if _safe_text(p).strip()]]
        allow, deny = configured["allow"], configured["deny"]
        by_id = {skill["id"]: skill for skill in found["skills"]}
        problems = list(found["problems"])
        for wanted in pins:
            if wanted not in by_id:
                # A pin naming nothing is a capability problem to report, not
                # an empty selection to shrug at: somebody asked for it.
                problems.append({
                    "id": wanted, "root": "",
                    "problem": "pinned skill was not found in this project",
                })
        brief_terms = self._skill_terms(task.get("brief", ""))
        selected: list[dict[str, Any]] = []
        skipped: list[dict[str, str]] = []
        for skill in found["skills"]:
            if skill["id"] in deny:
                # A denylist is the one judgement that outranks a pin: it is
                # the standing decision that this skill must not be used here.
                skipped.append({"id": skill["id"], "reason": "denied by project configuration"})
                continue
            if allow and skill["id"] not in allow:
                skipped.append({"id": skill["id"], "reason": "not on this project's skill allowlist"})
                continue
            if skill["id"] in pins:
                selected.append({**skill, "reason": "pinned explicitly"})
                continue
            overlap = brief_terms & self._skill_terms(
                f"{skill['id']} {skill['name']} {skill['description']}"
            )
            if overlap:
                selected.append({
                    **skill,
                    "reason": "matches this task: " + ", ".join(sorted(overlap)[:5]),
                })
            else:
                skipped.append({"id": skill["id"], "reason": "nothing in it matches this task"})
        truncated: list[dict[str, str]] = []
        if len(selected) > SKILL_SELECTION_LIMIT:
            for skill in selected[SKILL_SELECTION_LIMIT:]:
                truncated.append({
                    "id": skill["id"],
                    "reason": f"beyond the {SKILL_SELECTION_LIMIT}-skill limit for one task",
                })
            selected = selected[:SKILL_SELECTION_LIMIT]
        # The runtime loads its own root by convention, so its content is named
        # rather than pasted; anything it cannot see is provided in full,
        # bounded, because otherwise the worker starts blind to it.
        budget = SKILL_TOTAL_LIMIT
        for skill in selected:
            auto = skill["kind"] == "runtime" and skill["runtime"] == (runtime or "").strip()
            skill["auto_loaded"] = bool(auto)
            body = skill.pop("body", "")
            if auto:
                skill["content"] = ""
                skill["delivery"] = "named; the runtime loads this root itself"
                continue
            if len(body) > SKILL_CONTENT_LIMIT:
                body = body[:SKILL_CONTENT_LIMIT]
                truncated.append({
                    "id": skill["id"],
                    "reason": f"content trimmed to {SKILL_CONTENT_LIMIT} characters",
                })
            if len(body) > budget:
                body = ""
                skill["delivery"] = "named only; the context budget for skills was spent"
                truncated.append({
                    "id": skill["id"],
                    "reason": "named only; the context budget for skills was spent",
                })
            else:
                budget -= len(body)
                skill["delivery"] = "provided in full; this runtime does not load that root"
            skill["content"] = body
        return {
            "selected": selected,
            "skipped": skipped,
            "problems": problems,
            "truncated": truncated,
            "roots": found["roots"],
            "runtime": found["runtime"],
            "reason": (
                f"{len(selected)} of {len(found['skills'])} project skill(s) match this task"
                if found["skills"]
                else "none: this project declares no readable skills"
            ),
        }

    @staticmethod
    def _skills_section_text(selection: dict[str, Any]) -> str:
        """Render the selection for a worker to read.

        A skill the runtime loads for itself is named, not pasted: two copies
        of the same instructions in one context window is waste at best, and a
        contradiction the moment one of them is trimmed. A skill the runtime
        cannot see is provided in full, because otherwise naming a path it
        will never open is the same as saying nothing.
        """
        lines: list[str] = []
        for skill in selection.get("selected", []):
            lines.append(
                f"## {skill['name']} ({skill['id']}) -- {skill['path']}\n"
                f"Selected because: {skill.get('reason', '')}\n"
                f"Delivery: {skill.get('delivery', '')}"
            )
            if skill.get("auto_loaded"):
                lines.append(
                    "This runtime loads that directory itself; read it there "
                    "rather than expecting it repeated here."
                )
            elif skill.get("content"):
                lines.append(skill["content"])
            else:
                lines.append(f"Read it at {skill['path']}.")
        for problem in selection.get("problems", []):
            # Reported, never invented. A skill somebody asked for that cannot
            # be read is a capability gap the worker should raise, not
            # something to improvise an equivalent for.
            lines.append(
                f"UNAVAILABLE SKILL {problem.get('id') or '(root)'}: "
                f"{problem.get('problem', '')}. Do not invent a replacement; "
                "report it if the task needs it."
            )
        for dropped in selection.get("truncated", []):
            lines.append(
                f"NOT FULLY INCLUDED {dropped.get('id')}: {dropped.get('reason')}."
            )
        return "\n\n".join(lines).strip()

    @staticmethod
    def _skill_record(selection: dict[str, Any]) -> dict[str, Any]:
        """The durable, content-free summary kept on the task.

        Paths and reasons, never bodies: the content lives in the project, and
        copying it into Helm's own state would both bloat the record and put a
        managed project's material somewhere it does not belong.
        """
        return {
            "reason": selection.get("reason", ""),
            "runtime": selection.get("runtime", ""),
            "selected": [
                {
                    "id": skill["id"],
                    "path": skill["path"],
                    "reason": skill.get("reason", ""),
                    "auto_loaded": skill.get("auto_loaded", False),
                    "delivery": skill.get("delivery", ""),
                }
                for skill in selection.get("selected", [])
            ],
            "problems": selection.get("problems", []),
            "truncated": selection.get("truncated", []),
        }

    #: Moved to `helm.discovery`; aliased so every existing call site is unchanged.
    _discovery_settings = staticmethod(_discovery_settings)






    @staticmethod
    def _project_domains(project: dict[str, Any]) -> list[str]:
        configured = project.get("domains")
        if configured:
            return [_validate_domain_id(domain) for domain in _string_list(configured, "project domains")]
        settings_file = canonical(project["root"]) / ".helm" / "project.json"
        if not settings_file.exists():
            return []
        settings = _discovery_settings(canonical(project["root"]))
        return list(settings.get("domains", []))

    @staticmethod
    def _project_agent(project: dict[str, Any]) -> str | None:
        """Read a project's pinned runtime, preferring the persisted record."""
        configured = project.get("agent")
        if configured:
            return _validate_agent_id(configured, "project record")
        root = canonical(project["root"])
        if not (root / ".helm" / "project.json").exists():
            return None
        return _discovery_settings(root).get("agent")

    @staticmethod
    def _project_model(project: dict[str, Any]) -> str | None:
        """Read a project's pinned model, preferring the persisted record."""
        configured = project.get("model")
        if configured:
            return _validate_model_id(configured, "project record")
        root = canonical(project["root"])
        if not (root / ".helm" / "project.json").exists():
            return None
        pinned = _discovery_settings(root).get("model")
        return _validate_model_id(pinned, "project .helm/project.json") if pinned else None

    def _resolve_model(
        self, project: dict[str, Any], task: dict[str, Any]
    ) -> tuple[str | None, str]:
        """Choose the model a task runs on, most-specific-first.

        Same shape as runtime resolution, and for the same reason: anything
        stated outranks anything inferred. The task's own choice wins, then the
        project's pin, then a root default. There is deliberately no detection
        step -- guessing a model is not like guessing a runtime, where a wrong
        guess fails loudly on a missing executable. A wrong model runs, bills,
        and answers, so the last resort is to say nothing and let the runtime
        use its own default.
        """
        def refuse_if_excluded(model_id: str, source: str) -> str:
            """A model this root has recorded as unusable is refused, not swapped.

            Refused rather than silently replaced, for the same reason a
            missing runtime executable is an error: a substitution hides which
            of the two you actually got. And refused however the id arrived --
            a task's own choice included -- because the exclusion records a
            fact about this machine, not a preference about defaults. An
            exclusion an explicit choice could step over would not have stopped
            any of the nine reviewer tasks that drew the model this exists for.
            """
            excluded = self.preferences().excluded_models
            if model_id in excluded:
                raise HelmError(
                    f"model {model_id} is excluded by this root's preferences "
                    f"(set at model.exclude) and was named by {source}. "
                    f"Choose another model, or lift it with: "
                    f"helm prefs unset model.exclude"
                )
            return model_id

        def runtime_default(source: str) -> tuple[str, str]:
            """Stop the ladder and leave the model to the runtime."""
            return "", f"{source} asks for the runtime's own default model"

        chosen = task.get("model")
        if chosen == RUNTIME_DEFAULT_MODEL:
            return runtime_default("the task")
        if chosen:
            return (
                refuse_if_excluded(_validate_model_id(chosen, "task record"), "the task"),
                f"task names model {chosen}",
            )
        pinned = self._project_model(project)
        if pinned == RUNTIME_DEFAULT_MODEL:
            return runtime_default(f"project {project['id']}")
        if pinned:
            return (
                refuse_if_excluded(pinned, f"project {project['id']}"),
                f"project {project['id']} pins model {pinned}",
            )
        configured = os.environ.get("HELM_MODEL", "").strip()
        if configured == RUNTIME_DEFAULT_MODEL:
            return runtime_default("HELM_MODEL")
        if configured:
            return (
                refuse_if_excluded(_validate_model_id(configured, "HELM_MODEL"), "HELM_MODEL"),
                f"HELM_MODEL sets model {configured}",
            )
        # The root's own preference file sits below the environment, because a
        # variable is a session override somebody set on purpose for this run
        # and the file is the standing choice.
        preferred = self.preferences().default_model
        if preferred == RUNTIME_DEFAULT_MODEL:
            return runtime_default("root preferences")
        if preferred:
            return (
                refuse_if_excluded(preferred, "root preferences"),
                f"root preferences set model {preferred}",
            )
        return None, ""

    def _resolve_effort(
        self, project: dict[str, Any], task: dict[str, Any]
    ) -> tuple[str | None, str, str]:
        """Choose the reasoning effort a task runs at, most-specific-first.

        The same ladder as the model, and unset means the runtime's own
        default rather than a level Helm invented. Effort is a cost the
        commander pays and a quality difference they cannot see afterwards, so
        it is stated or it is left alone.
        """
        chosen = task.get("effort")
        if chosen:
            return (
                _validate_effort(chosen, "task record"),
                f"task asks for {chosen} effort",
                "task",
            )
        pinned = project.get("effort")
        if pinned:
            return (
                _validate_effort(str(pinned), f"project {project['id']}"),
                f"project {project['id']} pins {pinned} effort",
                "project",
            )
        configured = os.environ.get("HELM_EFFORT", "").strip()
        if configured:
            return (
                _validate_effort(configured, "HELM_EFFORT"),
                f"HELM_EFFORT sets {configured} effort",
                "env",
            )
        preferred = self.preferences().default_effort
        if preferred:
            return preferred, f"root preferences set {preferred} effort", "preference"
        return None, "", ""

    def _effort_expressible(self, effort: str, runtime_id: str | None) -> bool:
        """Whether this runtime can be told this level at all, by any means."""
        runtime = self._effort_capability(runtime_id)
        if runtime is None:
            return True
        return bool(runtime.accepts_effort(effort) or runtime.effort_model(effort))

    def _effort_capability(self, runtime_id: str | None):
        """This root's effort capability for a runtime, override first.

        A shipped capability is a default, not a limit. CLIs grow flags and
        models grow levels between Helm releases, so a root can teach its own
        installation with `effort.runtimes.<runtime>` and have it apply
        everywhere the shipped table would have.
        """
        if not runtime_id:
            return None
        taught = self.preferences().effort_runtimes.get(runtime_id)
        if taught is not None:
            mechanism, argument, levels = taught
            if mechanism == "model":
                # levels holds (level, model-id) pairs for a swap runtime.
                mapping = dict(levels)
                return runtimes.AgentRuntime(
                    id=runtime_id,
                    name=runtime_id,
                    interactive=(),
                    noninteractive=(),
                    env_passthrough=(),
                    detect_env=(),
                    effort_mechanism=runtimes.EFFORT_MODEL,
                    effort_models=mapping,
                    effort_levels=tuple(sorted(mapping)),
                )
            return runtimes.AgentRuntime(
                id=runtime_id,
                name=runtime_id,
                interactive=(),
                noninteractive=(),
                env_passthrough=(),
                detect_env=(),
                effort_mechanism=(
                    runtimes.EFFORT_FLAG if mechanism == "flag"
                    else runtimes.EFFORT_CONFIG
                ),
                effort_argument=argument,
                effort_levels=tuple(sorted(levels)),
            )
        return runtimes.builtin_runtime(runtime_id)

    def _require_effort_supported(
        self, effort: str | None, runtime_id: str | None, reason: str
    ) -> None:
        """Refuse an effort the resolved runtime cannot express.

        Dropping it instead would spend the commander's money at a level they
        did not choose, and leave nothing in the record to show the
        difference. Refusing names the runtime, the level, and where the level
        came from, so the fix is obvious from the message alone.
        """
        if not effort:
            return
        runtime = self._effort_capability(runtime_id)
        if runtime is None:
            return
        if runtime.accepts_effort(effort):
            return
        if runtime.effort_mechanism == runtimes.EFFORT_UNSUPPORTED:
            raise HelmError(
                f"runtime {runtime.id} cannot be told a reasoning effort, but "
                f"{reason} asks for {effort}. Drop the effort, or choose a "
                "runtime that accepts one (claude, codex)."
            )
        raise HelmError(
            f"runtime {runtime.id} does not accept effort {effort} ({reason}); "
            f"it takes {', '.join(runtime.effort_levels)}."
        )

    #: Moved to `helm.discovery`; aliased so every existing call site is unchanged.
    _launch_runtime_id = staticmethod(_launch_runtime_id)

    def preferences(self) -> prefs.Preferences:
        """This root's operator preferences, or the empty set.

        Read fresh rather than cached: the file is a few hundred bytes, and a
        coordinator that kept a stale copy would go on excluding a runtime the
        commander had just re-allowed. A root with no file gets
        `prefs.EMPTY`, which is the shipped default -- generic Helm imposes no
        operator choice on anyone.
        """
        if self._preferences_source is not None:
            return self._preferences_source
        try:
            root = self.store.configured_root()
        except SafetyError:
            raise
        except HelmError:
            root = None
        try:
            return prefs.load(prefs.preferences_path(root))
        except prefs.PreferencesError as exc:
            raise HelmError(str(exc)) from exc

    def _require_model_runtime(
        self,
        model: str | None,
        runtime_id: str | None,
        reason: str = "",
        *,
        named: str | None = None,
    ) -> None:
        """Refuse a model whose family this root restricts to other runtimes.

        The classifier that decides a model's family is generic metadata and
        refuses nothing by itself. The refusal exists only where a root has
        written the constraint into its own `preferences.json`, which is why
        this is an instance method reading that file rather than a static rule
        compiled into the product.

        Where a root *has* asked for one, it is checked wherever a model and a
        runtime meet -- ordinary worker selection and both reviewer paths --
        rather than at the one entry point somebody happened to notice.
        """
        if not model:
            return
        constraint = self.preferences().constraint_for(model)
        if constraint is None:
            return
        family, allowed = constraint
        if runtime_id in allowed:
            return
        raise HelmError(
            runtimes.family_pairing_error(
                model, family, allowed, named or runtime_id, reason
            )
        )

    def _with_model(
        self,
        profile: dict[str, Any],
        command: Sequence[str],
        model: str | None,
        reason: str,
    ) -> list[str]:
        """Put a resolved model into a launch command, or refuse to guess.

        Only a built-in runtime publishes the flag that selects its model. A
        profile that spells out its own command does not, so Helm has nothing
        to insert and must not invent one. Refusing is the point: silently
        dropping the model would leave the coordinator believing it had
        instructed a model it never sent, and the bill is the only place that
        difference would show up.
        """
        actual = list(command)
        launch_runtime = _launch_runtime_id(profile, actual)
        # A model does not have to arrive through Helm's model field. A profile
        # or a caller-supplied command can put `--model` straight into argv,
        # and a constraint checked only where Helm places a model itself would
        # be one the command walks past. Checked whether or not a model was
        # also resolved, and before the model below, since the argv is what
        # would actually be launched.
        baked = runtimes.model_in_command(actual)
        if baked is not None:
            self._require_model_runtime(
                baked,
                launch_runtime,
                "its launch command selects that model",
                named=launch_runtime or profile["id"],
            )
        if not model:
            return actual
        if baked is not None:
            # The command already selects a model, so adding the flag again
            # would pass it twice. Some runtimes then parse the value as a
            # list and die on it, taking the pane with them -- and the flag
            # Helm would add is the one already there.
            return actual
        # A restricted family is bound to its runtimes whatever chose the model
        # -- the CLI, the task, the project pin, or HELM_MODEL. Checked before
        # the flag question below, because for a profile that inherits a
        # built-in runtime "no model flag" would be the wrong reason to refuse.
        effective = profile.get("runtime") or profile.get("id")
        if runtimes.builtin_runtime(effective) is not None:
            self._require_model_runtime(
                model, str(effective), reason, named=profile["id"]
            )
        runtime = runtimes.builtin_runtime(profile["id"])
        if runtime is None or not profile.get("builtin"):
            raise HelmError(
                f"cannot run agent {profile['id']} on model {model} ({reason}): only "
                "built-in runtimes publish a model flag, and this agent supplies its "
                "own command. Put the model in that command, or drop the model."
            )
        # Same placement as AgentRuntime.with_model -- immediately after the
        # executable, so a variadic option later in the argv cannot swallow it
        # -- but applied to the already-resolved command, whose argv[0] may be
        # an absolute path the launch check found.
        return [actual[0], runtime.model_flag, model, *actual[1:]]

    def _domain_root(self, project: dict[str, Any]) -> Path | None:
        root = self.store.configured_root()
        if root is not None:
            return root / "domains"
        project_root = canonical(project["root"])
        if project_root.parent.name == "projects":
            return project_root.parent.parent / "domains"
        # StateStore(state_dir=<helm-root>/state) is a common library setup
        # even when initialize_root has not yet persisted the root setting.
        if self.store.directory.name == "state":
            return self.store.directory.parent / "domains"
        return None

    def _domain_extends(self, domain_root: Path, domain_id: str) -> list[str]:
        """Read one domain's declared bases from its optional domain.json."""
        manifest = self._safe_configuration_path(
            domain_root / domain_id / "domain.json", domain_root, "domain manifest"
        )
        if not manifest.is_file() or manifest.is_symlink():
            return []
        try:
            declared = json.loads(manifest.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise HelmError(f"invalid domain manifest for {domain_id}: {exc}") from exc
        if not isinstance(declared, dict):
            raise HelmError(f"invalid domain manifest for {domain_id}: expected an object")
        return [
            _validate_domain_id(base)
            for base in _string_list(declared.get("extends"), f"domain {domain_id} extends")
        ]

    def _domain_chain(self, domain_root: Path | None, domain_id: str | None) -> list[str]:
        """Resolve a domain to its base-first composition order.

        Shared practice belongs in a base domain that topical domains extend, so
        a task inherits it automatically instead of every project restating it.
        Bases come first and the selected domain last, so the most specific
        guidance is read last and a cycle can never loop.
        """
        if domain_root is None or not domain_id:
            return []
        ordered: list[str] = []
        visiting: set[str] = set()

        def visit(current: str, depth: int) -> None:
            if current in ordered:
                return
            if depth > _MAX_DOMAIN_DEPTH:
                raise HelmError(f"domain inheritance for {domain_id} is nested too deeply")
            if current in visiting:
                raise HelmError(f"domain inheritance for {domain_id} contains a cycle at {current}")
            visiting.add(current)
            for base in self._domain_extends(domain_root, current):
                if not (domain_root / base).is_dir():
                    raise HelmError(f"domain {current} extends unknown domain {base}")
                visit(base, depth + 1)
            visiting.discard(current)
            ordered.append(current)

        visit(domain_id, 0)
        return ordered

    def _known_domain_ids(self, project: dict[str, Any]) -> list[str]:
        domain_root = self._domain_root(project)
        if domain_root is None or not domain_root.is_dir():
            return []
        names: list[str] = []
        for entry in domain_root.iterdir():
            if not entry.is_dir() or entry.is_symlink():
                continue
            if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}", entry.name):
                continue
            # A small domain is a building block, composed via `extends` by a
            # domain a task actually resolves to. Left inferable, generic words
            # like "verification" or "progress" match almost any brief and
            # every task becomes ambiguous -- the cost of small domains, paid
            # in the wrong place. Marking them keeps composition cheap without
            # turning them into rival answers.
            settings = entry / "domain.json"
            if settings.is_file():
                try:
                    payload = json.loads(settings.read_text(encoding="utf-8"))
                except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
                    raise HelmError(f"cannot read domain settings {settings}: {exc}") from exc
                if isinstance(payload, dict) and payload.get("inferable") is False:
                    continue
            names.append(entry.name)
        return sorted(names)

    def domain_catalogue(self, project: dict[str, Any]) -> list[dict[str, Any]]:
        """What each domain is for, in its own words.

        Choosing a domain is a judgement about the nature of the work, not a
        string match on its brief. Keyword matching read "script" as software
        and "verification" as almost anything, so it is gone: domains now
        declare what they apply to, and the caller -- a coordinator that can
        actually read -- decides which fits.
        """
        domain_root = self._domain_root(project)
        if domain_root is None or not domain_root.is_dir():
            return []
        catalogue: list[dict[str, Any]] = []
        for name in self._all_domain_ids(project):
            meta = self.domain_meta(project, name)
            catalogue.append({
                "id": name,
                "applies_to": _safe_text(meta.get("applies_to", "")).strip(),
                "use_when": [str(v) for v in meta.get("use_when", []) or []],
                "not_for": [str(v) for v in meta.get("not_for", []) or []],
                "selectable": bool(meta.get("selectable", True)),
                "extends": list(meta.get("extends", []) or []),
            })
        return catalogue

    def domain_meta(self, project: dict[str, Any], domain_id: str) -> dict[str, Any]:
        """A domain's own declaration of what it is for.

        Frontmatter in `knowledge.md` is the source of truth, so the
        description cannot drift away from the knowledge it describes -- they
        are the same file. `domain.json` still works and fills gaps, for roots
        that predate this.
        """
        domain_root = self._domain_root(project)
        if domain_root is None:
            return {}
        meta: dict[str, Any] = {}
        settings = domain_root / domain_id / "domain.json"
        if settings.is_file():
            try:
                loaded = json.loads(settings.read_text(encoding="utf-8"))
                if isinstance(loaded, dict):
                    meta.update(loaded)
                    if loaded.get("inferable") is False:
                        meta["selectable"] = False
            except (OSError, UnicodeDecodeError, json.JSONDecodeError):
                pass
        knowledge = domain_root / domain_id / "knowledge.md"
        if knowledge.is_file():
            with contextlib.suppress(OSError):
                meta.update(_parse_frontmatter(knowledge.read_text(encoding="utf-8")))
        return meta

    def _all_domain_ids(self, project: dict[str, Any]) -> list[str]:
        domain_root = self._domain_root(project)
        if domain_root is None or not domain_root.is_dir():
            return []
        return sorted(
            entry.name
            for entry in domain_root.iterdir()
            if entry.is_dir()
            and not entry.is_symlink()
            and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}", entry.name)
        )

    def set_project_domains(
        self, project_id: str, domains: Sequence[str]
    ) -> dict[str, Any]:
        """Record a project's default domains in Helm's own state.

        Without one, every task on the project needs `--domain` by hand or
        fails outright, and the escape hatch a hurried coordinator reaches for
        -- `--no-domain` -- silently ships a worker without code-review,
        verification, or definition-of-done. This lives in Helm state rather
        than in the project's `.helm/project.json` so that setting it never
        writes to the project's repository: the state record already wins the
        precedence, and a project file is untrusted guidance that cannot
        expand its own scope.
        """
        with self.store.locked() as data:
            project = self._project(data, project_id)
            known = self._all_domain_ids(project)
            selected = []
            for domain in _string_list(domains, "project domains"):
                domain = _validate_domain_id(domain)
                if known and domain not in known:
                    raise HelmError(
                        f"unknown domain: {domain} (available: {', '.join(known)})"
                    )
                if domain not in selected:
                    selected.append(domain)
            project["domains"] = selected
            return project

    def resolve_domain(
        self,
        project: dict[str, Any],
        brief: str,
        *,
        explicit: str | None = None,
        no_domain: bool = False,
    ) -> tuple[str | None, str]:
        """Resolve one domain without guessing from the words in a brief.

        Helm no longer infers. A domain is chosen by whoever understands the
        task -- the coordinator picks from `helm domain list`, where each
        domain says what work it applies to -- or by the project's own default.
        Anything else resolves to no domain, which is honest: the worker gets
        core safety rules rather than a pack matched on a coincidence.
        """
        if explicit is not None:
            selected = _validate_domain_id(explicit)
            known = self._all_domain_ids(project)
            if known and selected not in known:
                raise HelmError(
                    f"unknown domain: {selected} (available: {', '.join(known)})"
                )
            return selected, "explicit --domain override"
        configured = self._project_domains(project)
        if len(configured) == 1:
            return configured[0], "project default domain"
        if len(configured) > 1:
            choices = ", ".join(configured)
            raise HelmError(
                f"project {project['id']} lists several default domains ({choices}); "
                "pass --domain <domain-id> to say which this task needs"
            )
        if no_domain:
            return None, "explicitly run without a domain"
        selectable = [
            entry["id"] for entry in self.domain_catalogue(project) if entry["selectable"]
        ]
        if not selectable:
            return None, "no domains exist in this root"
        # Resolving to nothing silently is indistinguishable from a task that
        # genuinely has no domain, and that is how knowledge that exists never
        # reaches the worker that needed it. Make the caller say which.
        raise HelmError(
            f"no domain chosen for this task. Pick one by what the task IS "
            f"(helm domain list): {', '.join(selectable)}. "
            "Pass --domain <id>, or --no-domain if none applies."
        )




    #: Directory/file permission pairs a read-only workspace is locked to.
    #: Read and traverse stay available -- an agent still has to look around --
    #: only the write bit is gone, so `open(..., "w")`, `mkdir`, `unlink`, and
    #: every git operation that needs to touch a tracked file all fail at the
    #: filesystem itself rather than on a flag nothing downstream reads.
    #:
    #: Locking and unlocking mask/restore only the write bits (0o222) against
    #: whatever mode a path already had, rather than stamping a fixed mode --
    #: a fixed mode would silently drop an executable bit on a script or a
    #: private (owner-only) mode on a file that had one before the lock, and
    #: neither of those is what a read-only guard is supposed to change.


    #: The one writable place inside a locked read-only workspace. A read-only
    #: task still has a DELIVERABLE -- a findings report, a contact sheet, a
    #: transcript -- and with the whole worktree stripped of write bits it had
    #: nowhere to put one: writing into the worktree failed, and Helm then
    #: rejected the session scratchpad as outside the assigned workspace. Two
    #: projects lost real evidence to that dead end, one of them twice. The
    #: read-only guarantee that matters is "do not alter what is under review",
    #: which a dedicated output directory does not touch.
    READ_ONLY_OUTPUT_DIR = ".helm-out"

