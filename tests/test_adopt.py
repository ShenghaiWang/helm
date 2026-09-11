"""`helm adopt`: one command from a repository to a registered project."""

from __future__ import annotations

import contextlib
import io
import json
import shutil
import subprocess
from pathlib import Path

from helm import cli
from helm.core import Coordinator
from helm.errors import HelmError, SafetyError
from helm.state import StateStore
from tests.support import SHIPPED_DOMAINS, HelmTestCase


class AdoptTests(HelmTestCase):
    def _root(self, name: str) -> tuple[Path, Coordinator]:
        helm_root = self._helm_root(name)
        shutil.rmtree(helm_root / "domains")
        shutil.copytree(SHIPPED_DOMAINS, helm_root / "domains")
        return helm_root, Coordinator(StateStore(helm_root / "state", helm_root=helm_root))

    def test_a_repository_elsewhere_is_cloned_described_and_registered(self) -> None:
        helm_root, coordinator = self._root("adopt-root")
        original = self.repo("widgets")
        adopted = coordinator.adopt_project(
            str(original), helm_root=helm_root, delivery_policy="pr", domains=["software-delivery"],
            label="Widgets", review=False,
        )
        self.assertEqual(adopted["project_id"], "widgets")
        self.assertEqual(Path(adopted["cloned_from"]).resolve(), original.resolve())
        destination = helm_root / "projects" / "widgets"
        self.assertTrue((destination / ".git").is_dir())
        self.assertTrue((original / ".git").is_dir(), "the original is untouched")
        settings = json.loads((destination / ".helm" / "project.json").read_text())
        self.assertEqual(settings["label"], "Widgets")
        self.assertEqual(settings["delivery_policy"], "pr")
        self.assertEqual(settings["domains"], ["software-delivery"])
        self.assertFalse(settings["review"])
        self.assertNotIn("foreman", settings)
        project = coordinator.get_project("widgets")
        self.assertEqual(project["delivery_policy"], "pr")
        self.assertEqual(project["name"], "Widgets")
        self.assertIs(project.get("review"), False)
        # Adopting the same id again is refused; adopting in place is not.
        with self.assertRaisesRegex(SafetyError, "already exists"):
            coordinator.adopt_project(str(self.repo("widgets-again")), helm_root=helm_root, project_id="widgets")
        again = coordinator.adopt_project(str(destination), helm_root=helm_root)
        self.assertIsNone(again["cloned_from"])
        self.assertIsNone(again["settings_written"], "an existing description is the owner's")
        self.assertEqual(again["settings_existing"]["label"], "Widgets")

    def test_a_repository_without_git_or_commits_is_refused(self) -> None:
        helm_root, coordinator = self._root("refuse-root")
        plain = Path(self.temp.name) / "plain"
        plain.mkdir()
        with self.assertRaisesRegex(HelmError, "not a Git repository"):
            coordinator.adopt_project(str(plain), helm_root=helm_root)
        empty = Path(self.temp.name) / "empty"
        empty.mkdir()
        subprocess.run(["git", "init", "-q", str(empty)], check=True)
        with self.assertRaisesRegex(HelmError, "no commit yet"):
            coordinator.adopt_project(str(empty), helm_root=helm_root)

    def test_the_cli_says_what_it_decided_and_runs_the_preflight(self) -> None:
        helm_root, _ = self._root("cli-root")
        original = self.repo("gadgets")
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = cli.main([
                "--root", str(helm_root), "adopt", str(original), "--id", "gadgets",
                "--domain", "software-delivery", "--no-foreman",
            ])
        text = out.getvalue()
        self.assertEqual(code, 0, text)
        self.assertIn("Adopted gadgets at", text)
        self.assertIn("cloned from", text)
        self.assertIn("foreman: no", text)
        self.assertIn("helm doctor: root", text)
        self.assertIn("project gadgets", text)
        self.assertIn('Next: helm route gadgets', text)
        self.assertIn("ok       project.domains", text)
        # A project whose own preflight fails says so in the exit code.
        broken = self.repo("broken")
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = cli.main(["--root", str(helm_root), "adopt", str(broken), "--domain", "no-such-domain"])
        self.assertEqual(code, 1)
        self.assertIn("error    project.domains", out.getvalue())
