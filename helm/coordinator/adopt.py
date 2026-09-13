"""`helm adopt`: bring a repository under Helm in one command.

Registering a project by hand is four commands and two files a newcomer has
to know about: where the checkout must live, what `.helm/project.json`
takes, which domain to name, how the base branch is resolved, and then the
foreman. The first experience decides whether anyone tries the second, so
this does all of it, says what it decided, and finishes with the preflight
that would have caught a mistake.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path
from typing import Any

from ..errors import HelmError, SafetyError
from ..git import _git
from ..paths import canonical
from ..values import _validate_domain_id, _validate_project_id


class AdoptMixin:
    def adopt_project(
        self,
        source: str,
        *,
        helm_root: Path,
        project_id: str | None = None,
        label: str | None = None,
        delivery_policy: str = "local",
        domains: list[str] | None = None,
        base_branch: str | None = None,
        foreman: bool = True,
        review: bool = True,
        agent: str | None = None,
        model: str | None = None,
        effort: str | None = None,
    ) -> dict[str, Any]:
        """Put a repository under `projects/`, describe it, register it.

        A repository outside `projects/` is cloned there -- from its own
        `origin` when it has one, so the clone's upstream is the real remote,
        otherwise from the path. Nothing is moved and the original is not
        touched. A repository already under `projects/` is adopted in place.
        The description is written to `.helm/project.json` only when there
        is none; an existing file is the owner's, and is read instead.
        """
        self.authority("adopting a project")
        source_path = canonical(source)
        if not source_path.is_dir():
            raise HelmError(f"not a directory: {source}")
        if not (source_path / ".git").exists():
            raise HelmError(
                f"{source_path} is not a Git repository; every Helm project is a committed "
                "Git repository (git init, then commit, then adopt)"
            )
        try:
            _git(source_path, "rev-parse", "--verify", "HEAD")
        except HelmError as exc:
            raise HelmError(f"{source_path} has no commit yet; commit first, then adopt") from exc
        projects_root = canonical(helm_root) / "projects"
        if not projects_root.is_dir():
            raise HelmError(f"{helm_root} has no projects/ directory; is it a Helm root?")
        project_id = _validate_project_id(project_id or source_path.name)
        destination = projects_root / project_id
        cloned_from: str | None = None
        in_place = destination.exists() and source_path == canonical(destination)
        if in_place:
            pass
        elif destination.exists():
            raise SafetyError(
                f"projects/{project_id} already exists; pick another --id, or adopt that directory in place"
            )
        else:
            origin = ""
            try:
                origin = _git(source_path, "remote", "get-url", "origin").strip()
            except HelmError:
                origin = ""
            clone_source = origin or str(source_path)
            result = subprocess.run(
                ["git", "clone", "--quiet", clone_source, str(destination)],
                text=True, capture_output=True, check=False,
            )
            if result.returncode != 0:
                raise HelmError(f"git clone of {clone_source} failed: {(result.stderr or result.stdout).strip()[:300]}")
            cloned_from = clone_source
        settings_file = destination / ".helm" / "project.json"
        written: dict[str, Any] | None = None
        if settings_file.exists():
            try:
                existing = json.loads(settings_file.read_text(encoding="utf-8"))
            except (OSError, ValueError) as exc:
                raise HelmError(f"{settings_file} is unreadable: {exc}") from exc
        else:
            existing = None
            written = {"label": label or project_id, "delivery_policy": delivery_policy}
            written["domains"] = [_validate_domain_id(d) for d in (domains or ["software-delivery"])]
            if base_branch:
                written["base_branch"] = base_branch
            if not foreman:
                written["foreman"] = False
            if not review:
                written["review"] = False
            if agent:
                written["agent"] = agent
            if model:
                written["model"] = model
            if effort:
                written["effort"] = effort
            settings_file.parent.mkdir(parents=True, exist_ok=True)
            settings_file.write_text(json.dumps(written, indent=2) + "\n", encoding="utf-8")
        # The first pass registers; the second applies the description the
        # record does not carry yet -- foreman, review, base branch -- which
        # is what discovery does for an already-known project.
        self.discover_project(helm_root, project_id)
        self.discover_project(helm_root, project_id)
        project = self.get_project(project_id)
        return {
            "project": project,
            "project_id": project_id,
            "root": str(destination),
            "cloned_from": cloned_from,
            "settings_file": str(settings_file),
            "settings_written": written,
            "settings_existing": existing,
        }
