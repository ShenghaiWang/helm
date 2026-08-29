"""Root-local operator preferences: isolation, schema, precedence, CLI.

The layer exists so that Helm the product stays generic while one installation
keeps durable answers of its own. Every test here is really about one of two
questions: does a preference actually take effect, and can anything other than
the commander at the root put one there.
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import shutil
import subprocess
import sys
import threading
from pathlib import Path
from unittest import mock

from helm import cli, preferences, runtimes
from helm.core import Coordinator, HelmError, StateStore

from tests.support import HelmTestCase


class PreferenceFileTests(HelmTestCase):
    """Where the file lives, and who is allowed to write one."""

    def test_shipped_helm_imposes_no_operator_preference_at_all(self) -> None:
        """A fresh clone must behave as though nobody had an opinion.

        This is the test that keeps the whole layer honest. If Helm refused
        anything here, an operator's choice would have leaked into the product
        and every clone would inherit it.
        """
        empty = self.coordinator.preferences()
        self.assertFalse(empty.present)
        self.assertIsNone(empty.default_agent)
        self.assertIsNone(empty.default_model)
        self.assertEqual(empty.excluded_agents, frozenset())
        self.assertEqual(dict(empty.model_runtimes), {})
        self.assertEqual(empty.entries(), [])
        # The classifier still classifies -- it is technical metadata -- but no
        # classification produces a constraint on its own.
        self.assertTrue(runtimes.is_claude_model("claude-opus-5"))
        self.assertEqual(runtimes.model_families("claude-opus-5"), ("claude",))
        self.assertIsNone(empty.constraint_for("claude-opus-5"))

    def test_preferences_are_read_from_the_root_and_nowhere_else(self) -> None:
        """A project file is untrusted guidance and cannot become policy."""
        helm_root = self._helm_root("prefs-root")
        project_root = self.repo("scoped")
        shutil.move(str(project_root), str(helm_root / "projects" / "scoped"))
        coordinator = Coordinator(StateStore(helm_root / "state", helm_root=helm_root))

        # A project that writes the file inside itself changes nothing.
        settings = helm_root / "projects" / "scoped" / ".helm"
        settings.mkdir()
        (helm_root / "projects" / "scoped" / preferences.PREFERENCES_FILENAME).write_text(
            json.dumps({"version": 1, "agent": {"exclude": ["claude"]}})
        )
        (settings / "preferences.json").write_text(
            json.dumps({"version": 1, "agent": {"exclude": ["claude"]}})
        )
        self.assertEqual(coordinator.excluded_agents(), set())

        # The same content at the root does take effect.
        self.write_preferences(helm_root, agent={"exclude": ["claude"]})
        self.assertEqual(coordinator.excluded_agents(), {"claude"})

    def test_a_symlinked_preferences_file_is_refused(self) -> None:
        """Same rule as every Helm-owned path: a symlink is somebody else's file."""
        helm_root = self._helm_root("prefs-link")
        outside = Path(self.temp.name) / "elsewhere.json"
        outside.write_text(json.dumps({"version": 1, "agent": {"exclude": ["pi"]}}))
        (helm_root / preferences.PREFERENCES_FILENAME).symlink_to(outside)
        coordinator = Coordinator(StateStore(helm_root / "state", helm_root=helm_root))
        with self.assertRaisesRegex(HelmError, r"must not be a symlink"):
            coordinator.preferences()

    def test_the_preferences_file_is_ignored_and_never_tracked(self) -> None:
        """A committed answer would ship one machine's cost policy to everyone."""
        self.assertEqual(
            subprocess.run(
                ["git", "check-ignore", "--no-index", "--quiet", "preferences.json"],
                check=False,
                cwd=str(Path.cwd()),
            ).returncode,
            0,
        )
        tracked = subprocess.run(
            ["git", "ls-files"], text=True, stdout=subprocess.PIPE, check=True
        ).stdout.split()
        self.assertNotIn("preferences.json", tracked)


class PreferenceSchemaTests(HelmTestCase):
    """Narrow validation, so this can never become a place to park a secret."""

    def _load(self, document: object) -> preferences.Preferences:
        path = Path(self.temp.name) / "prefs.json"
        path.write_text(json.dumps(document))
        return preferences.load(path)

    def test_absence_is_not_an_error(self) -> None:
        missing = preferences.load(Path(self.temp.name) / "nothing.json")
        self.assertFalse(missing.present)
        self.assertIsNone(preferences.preferences_path(None, {}))

    def test_a_valid_document_round_trips_through_every_field(self) -> None:
        loaded = self._load({
            "version": 1,
            "agent": {"default": "claude", "exclude": ["codex", "omp"]},
            "model": {"default": "some-model-5", "runtimes": {"claude": ["claude"]}},
        })
        self.assertTrue(loaded.present)
        self.assertEqual(loaded.default_agent, "claude")
        self.assertEqual(loaded.default_model, "some-model-5")
        self.assertEqual(loaded.excluded_agents, {"codex", "omp"})
        self.assertEqual(loaded.model_runtimes["claude"], {"claude"})
        self.assertEqual(loaded.document()["version"], preferences.PREFERENCES_VERSION)

    def test_malformed_and_unknown_fields_are_refused_with_useful_errors(self) -> None:
        """An ignored field is a preference that silently does nothing.

        A typo'd exclusion that reads as "no exclusion" is a cost limit
        switched off without anybody being told, which is worse than a file
        that fails to load. It is also the property that stops a credential
        being parked in an unread key.
        """
        cases = [
            ({"agent": {}}, r"must state a version"),
            ({"version": 99}, r"unsupported preferences version"),
            ({"version": "1"}, r"unsupported preferences version"),
            ([], r"must be a JSON object"),
            ({"version": 1, "secrets": {"api_key": "x"}}, r"unknown preference field"),
            ({"version": 1, "agent": {"defualt": "pi"}}, r"unknown preference field"),
            ({"version": 1, "agent": {"token": "sk-x"}}, r"agent.token"),
            ({"version": 1, "model": {"runtimes": {"gpt": ["codex"]}}}, r"unknown model family"),
            ({"version": 1, "model": {"runtimes": {"claude": []}}}, r"at least one runtime"),
            ({"version": 1, "agent": {"exclude": "codex"}}, r"must be a list"),
            ({"version": 1, "agent": {"default": "pi; rm -rf /"}}, r"agent id must be"),
            ({"version": 1, "agent": {"default": 7}}, r"agent id must be"),
            ({"version": 1, "model": {"default": "a b"}}, r"model must be"),
            ({"version": 1, "agent": "claude"}, r"agent.* must be an object"),
        ]
        for document, pattern in cases:
            with self.subTest(document=document):
                with self.assertRaisesRegex(preferences.PreferencesError, pattern):
                    self._load(document)

    def test_an_oversized_or_unparseable_file_is_refused_before_it_is_believed(self) -> None:
        path = Path(self.temp.name) / "big.json"
        path.write_text("x" * (preferences.MAX_BYTES + 1))
        with self.assertRaisesRegex(preferences.PreferencesError, r"too large"):
            preferences.load(path)
        path.write_text("{not json")
        with self.assertRaisesRegex(preferences.PreferencesError, r"cannot parse"):
            preferences.load(path)

    def test_every_supported_key_is_documented_and_no_other_key_is_accepted(self) -> None:
        """`prefs keys` is the documentation, so it has to be complete."""
        self.assertEqual(
            set(preferences.SUPPORTED_KEYS),
            {
                "agent.default",
                "agent.exclude",
                "model.default",
                "model.free",
                "model.runtimes.<family>",
                "review.agent",
                "cleanup.after_merge",
                "effort.default",
                "effort.runtimes.<runtime>",
            },
        )
        for family in runtimes.model_family_ids():
            self.assertEqual(
                preferences.split_model_runtimes_key(f"model.runtimes.{family}"), family
            )
        with self.assertRaises(preferences.PreferencesError):
            preferences.takes_list("agent.credentials")


class PreferencePrecedenceTests(HelmTestCase):
    """More specific wins; a restriction is never weakened by anything below."""

    def _root_with_project(self, name: str, settings: dict | None = None) -> tuple[Coordinator, dict, Path]:
        helm_root = self._helm_root(f"helm-{name}")
        shutil.move(str(self.repo(name)), str(helm_root / "projects" / name))
        if settings is not None:
            (helm_root / "projects" / name / ".helm").mkdir()
            (helm_root / "projects" / name / ".helm" / "project.json").write_text(
                json.dumps(settings)
            )
        coordinator = Coordinator(StateStore(helm_root / "state", helm_root=helm_root))
        return coordinator, coordinator.discover_project(helm_root, name), helm_root

    def test_a_root_default_model_sits_below_the_task_the_project_and_the_env(self) -> None:
        coordinator, project, helm_root = self._root_with_project("modelorder")
        self.write_preferences(helm_root, model={"default": "preference-model"})

        with mock.patch.dict(os.environ, {"HELM_MODEL": ""}):
            task = coordinator.create_task(project["id"], "work")
            model, reason = coordinator._resolve_model(project, task)
            self.assertEqual(model, "preference-model")
            self.assertIn("root preferences", reason)

            # An environment variable is a session override and outranks it.
            with mock.patch.dict(os.environ, {"HELM_MODEL": "session-model"}):
                model, reason = coordinator._resolve_model(project, task)
                self.assertEqual(model, "session-model")

            # A task's own choice outranks everything.
            named = coordinator.create_task(project["id"], "work", model="task-model")
            self.assertEqual(coordinator._resolve_model(project, named)[0], "task-model")

        # ...as does the project's pin, which is more specific than the root.
        pinned, pinned_project, pinned_root = self._root_with_project(
            "modelpin", {"model": "project-model"}
        )
        self.write_preferences(pinned_root, model={"default": "preference-model"})
        pinned_task = pinned.create_task(pinned_project["id"], "work")
        self.assertEqual(
            pinned._resolve_model(pinned_project, pinned_task)[0], "project-model"
        )

    def test_a_root_default_agent_sits_below_the_env_and_above_detection(self) -> None:
        coordinator, project, helm_root = self._root_with_project("agentorder")
        self.write_preferences(helm_root, agent={"default": "opencode"})
        # `detect_env` would otherwise answer this; the preference is stated,
        # and anything stated outranks anything inferred.
        with mock.patch.dict(os.environ, {"HELM_AGENT": "", "CLAUDECODE": "1"}):
            chosen, reason = coordinator._default_agent_id(project)
            self.assertEqual(chosen, "opencode")
            self.assertIn("root preferences", reason)
            with mock.patch.dict(os.environ, {"HELM_AGENT": "pi"}):
                self.assertEqual(coordinator._default_agent_id(project)[0], "pi")
        # A project pin is more specific still.
        pinned, pinned_project, pinned_root = self._root_with_project(
            "agentpin", {"agent": "codex"}
        )
        self.write_preferences(pinned_root, agent={"default": "opencode"})
        with mock.patch.dict(os.environ, {"HELM_AGENT": ""}):
            self.assertEqual(pinned._default_agent_id(pinned_project)[0], "codex")

    def test_exclusions_are_unioned_and_a_project_can_never_shrink_them(self) -> None:
        """Merging is the only combination that cannot accidentally widen one."""
        helm_root = self._helm_root("union-root")
        shutil.move(str(self.repo("union")), str(helm_root / "projects" / "union"))
        coordinator = Coordinator(StateStore(helm_root / "state", helm_root=helm_root))
        with coordinator.store.locked() as data:
            data["config"]["excluded_agents"] = ["codex"]
        self.write_preferences(helm_root, agent={"exclude": ["omp"]})
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("HELM_EXCLUDE_AGENTS", None)
            os.environ.pop("HELM_REVIEW_EXCLUDE_AGENTS", None)
            self.assertEqual(coordinator.excluded_agents(), {"codex", "omp"})
            self.assertEqual(coordinator.legacy_excluded_agents(), {"codex"})

        # A project asking for fewer exclusions is not consulted at all.
        (helm_root / "projects" / "union" / ".helm").mkdir()
        (helm_root / "projects" / "union" / ".helm" / "project.json").write_text(
            json.dumps({"excluded_agents": [], "agent": "codex"})
        )
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("HELM_EXCLUDE_AGENTS", None)
            os.environ.pop("HELM_REVIEW_EXCLUDE_AGENTS", None)
            self.assertEqual(coordinator.excluded_agents(), {"codex", "omp"})

    def test_the_environment_stays_a_whole_session_override(self) -> None:
        """Backward compatibility: a root configured by variable is untouched.

        The variable replaces the file rather than merging with it, including
        with an empty value, because that is what it did before this layer
        existed and it is the escape hatch for a deliberate one-off run.
        """
        helm_root = self._helm_root("envoverride")
        coordinator = Coordinator(StateStore(helm_root / "state", helm_root=helm_root))
        self.write_preferences(helm_root, agent={"exclude": ["omp"]})
        with mock.patch.dict(os.environ, {"HELM_EXCLUDE_AGENTS": "pi"}):
            self.assertEqual(coordinator.excluded_agents(), {"pi"})
        with mock.patch.dict(os.environ, {"HELM_EXCLUDE_AGENTS": ""}):
            self.assertEqual(coordinator.excluded_agents(), set())
        # The legacy review-only spelling still works with no file at all.
        bare = self._helm_root("legacyonly")
        legacy = Coordinator(StateStore(bare / "state", helm_root=bare))
        with legacy.store.locked() as data:
            data["config"]["review_exclude_agents"] = ["codex"]
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("HELM_EXCLUDE_AGENTS", None)
            os.environ.pop("HELM_REVIEW_EXCLUDE_AGENTS", None)
            self.assertEqual(legacy.excluded_agents(), {"codex"})


class PreferenceLaunchTests(HelmTestCase):
    """The same launch and the same review, with and without a restriction."""

    def _fake_agent_cli(self, *names: str) -> Path:
        bin_dir = Path(self.temp.name) / f"bin-{'-'.join(names)}"
        bin_dir.mkdir(exist_ok=True)
        for name in names:
            executable = bin_dir / name
            executable.write_text("#!/bin/sh\nexit 0\n")
            executable.chmod(0o755)
        return bin_dir

    def _root(self, name: str) -> tuple[Coordinator, dict, Path]:
        helm_root = self._helm_root(f"helm-{name}")
        shutil.move(str(self.repo(name)), str(helm_root / "projects" / name))
        coordinator = Coordinator(StateStore(helm_root / "state", helm_root=helm_root))
        return coordinator, coordinator.discover_project(helm_root, name), helm_root

    def test_without_a_restriction_any_runtime_may_run_any_model(self) -> None:
        coordinator, project, _ = self._root("nopolicy")
        bin_dir = self._fake_agent_cli("pi", "claude")
        task = coordinator.create_task(
            project["id"], "port the parser", model="claude-opus-5", agent="pi"
        )
        env = {"PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}", "HELM_MODEL": ""}
        with mock.patch.dict(os.environ, env):
            worker = coordinator.prepare_external_worker(
                task["id"], None, execution="herdr"
            )
        self.assertEqual(worker["agent_id"], "pi")
        self.assertEqual(worker["command"][1:3], ["--model", "claude-opus-5"])

    def test_with_a_restriction_the_same_launch_is_refused_and_says_why(self) -> None:
        coordinator, project, helm_root = self._root("policy")
        self.write_preferences(helm_root, model={"runtimes": {"claude": ["claude"]}})
        bin_dir = self._fake_agent_cli("pi", "claude")
        task = coordinator.create_task(
            project["id"], "port the parser", model="claude-opus-5", agent="pi"
        )
        env = {"PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}", "HELM_MODEL": ""}
        with mock.patch.dict(os.environ, env), self.assertRaises(HelmError) as caught:
            coordinator.prepare_external_worker(task["id"], None, execution="herdr")
        message = str(caught.exception)
        self.assertIn("claude model family", message)
        self.assertIn("preferences restrict", message)
        self.assertIn("helm prefs unset model.runtimes.claude", message)
        # No substitution: the task is not quietly run on the allowed runtime.
        self.assertEqual(coordinator.inspect_task(task["id"])["workers"], [])

    def test_reviewer_selection_is_coherent_with_and_without_a_restriction(self) -> None:
        """Independence is chosen from what may actually run the model.

        Without a restriction the reviewer is simply somebody other than the
        author. With one, the field narrows to the allowed runtimes first, so a
        restricted model never lands on a runtime that would accept the name
        and bill for it.
        """
        bin_dir = self._fake_agent_cli("claude", "pi", "opencode")
        with mock.patch.dict(os.environ, {"PATH": str(bin_dir)}):
            open_choice = self.coordinator.pick_reviewer_agent("pi", model="claude-opus-5")
            self.assertNotEqual(open_choice["agent"], "pi")
            self.assertEqual(open_choice["independence"], "different-runtime")

        self.write_preferences(model={"runtimes": {"claude": ["claude"]}})
        with mock.patch.dict(os.environ, {"PATH": str(bin_dir)}):
            restricted = self.coordinator.pick_reviewer_agent("pi", model="claude-opus-5")
            self.assertEqual(restricted["agent"], "claude")
            # A non-restricted model is untouched by the restriction.
            other = self.coordinator.pick_reviewer_agent("claude", model="some-model-5")
            self.assertNotEqual(other["agent"], "claude")


class PreferenceCliTests(HelmTestCase):
    """The ergonomic surface, and the authority boundary around it."""

    def _cli(self, helm_root: Path, *argv: str) -> tuple[int, str]:
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer), contextlib.redirect_stderr(buffer):
            code = cli.main(["--root", str(helm_root), *argv])
        return code, buffer.getvalue()

    def test_set_show_unset_and_keys_round_trip_without_hand_editing(self) -> None:
        helm_root = self._helm_root("cli-prefs")
        code, output = self._cli(helm_root, "prefs", "show")
        self.assertEqual(code, 0)
        self.assertIn("nothing set", output)

        self.assertEqual(self._cli(helm_root, "prefs", "set", "agent.default", "claude")[0], 0)
        self.assertEqual(
            self._cli(helm_root, "prefs", "set", "agent.exclude", "codex", "omp")[0], 0
        )
        self.assertEqual(
            self._cli(helm_root, "prefs", "set", "model.runtimes.claude", "claude")[0], 0
        )
        code, output = self._cli(helm_root, "prefs", "show")
        self.assertEqual(code, 0)
        self.assertIn("agent.default = claude", output)
        self.assertIn("agent.exclude = codex, omp", output)
        self.assertIn("model.runtimes.claude = claude", output)

        code, output = self._cli(helm_root, "prefs", "path")
        self.assertIn(str(helm_root / "preferences.json"), output)
        code, output = self._cli(helm_root, "prefs", "keys")
        for key in preferences.SUPPORTED_KEYS:
            self.assertIn(key, output)

        self.assertEqual(self._cli(helm_root, "prefs", "unset", "agent.exclude")[0], 0)
        loaded = preferences.load(helm_root / "preferences.json")
        self.assertEqual(loaded.excluded_agents, frozenset())
        self.assertEqual(loaded.default_agent, "claude")

    def test_a_rejected_value_leaves_the_file_exactly_as_it_was(self) -> None:
        """Validation happens against the whole document before anything lands."""
        helm_root = self._helm_root("cli-reject")
        self._cli(helm_root, "prefs", "set", "agent.default", "claude")
        before = (helm_root / "preferences.json").read_text()
        for argv in (
            ("prefs", "set", "agent.secret", "sk-not-a-preference"),
            ("prefs", "set", "agent.default", "pi; rm -rf /"),
            ("prefs", "set", "model.runtimes.nosuchfamily", "pi"),
            ("prefs", "set", "agent.default", "pi", "codex"),
        ):
            with self.subTest(argv=argv):
                code, output = self._cli(helm_root, *argv)
                self.assertEqual(code, 2)
                self.assertEqual((helm_root / "preferences.json").read_text(), before)
                self.assertNotIn("sk-not-a-preference", output)

    def test_a_preference_write_is_never_observable_half_applied(self) -> None:
        """A reader catching a truncated file reads a root with no exclusions.

        That is a cost limit silently switched off for as long as the window
        lasts, so the write is a temporary file plus one `os.replace`.
        """
        path = Path(self.temp.name) / "atomic.json"
        current = preferences.load(path)
        preferences.save(
            preferences.apply(
                preferences.Preferences(path=path), "agent.exclude", ["codex"]
            )
        )
        wide = preferences.apply(
            preferences.Preferences(path=path),
            "agent.exclude",
            [f"agent-{index}" for index in range(400)],
        )
        seen: list[str] = []
        stop = threading.Event()

        def reader() -> None:
            while not stop.is_set():
                with contextlib.suppress(OSError):
                    if path.exists():
                        seen.append(path.read_text(encoding="utf-8"))

        watcher = threading.Thread(target=reader, daemon=True)
        watcher.start()
        try:
            for _ in range(25):
                preferences.save(wide)
        finally:
            stop.set()
            watcher.join(timeout=5)
        self.assertTrue(seen, "the reader never managed to observe the file")
        for observed in seen:
            self.assertEqual(json.loads(observed)["version"], 1)

    def test_migrate_copies_legacy_state_config_without_changing_behaviour(self) -> None:
        helm_root = self._helm_root("cli-migrate")
        coordinator = Coordinator(StateStore(helm_root / "state", helm_root=helm_root))
        with coordinator.store.locked() as data:
            data["config"]["review_exclude_agents"] = ["codex"]
        code, output = self._cli(helm_root, "prefs", "migrate")
        self.assertEqual(code, 0)
        self.assertIn("codex", output)
        self.assertEqual(
            preferences.load(helm_root / "preferences.json").excluded_agents, {"codex"}
        )
        # The legacy entry is deliberately left alone, and the two are unioned,
        # so an older Helm reading this root still sees the same exclusion.
        self.assertEqual(coordinator.legacy_excluded_agents(), {"codex"})
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("HELM_EXCLUDE_AGENTS", None)
            os.environ.pop("HELM_REVIEW_EXCLUDE_AGENTS", None)
            self.assertEqual(coordinator.excluded_agents(), {"codex"})
        code, output = self._cli(helm_root, "prefs", "migrate")
        self.assertIn("Nothing to migrate", self._cli(self._helm_root("m2"), "prefs", "migrate")[1])

    def test_writing_a_preference_is_the_commanders_and_reading_it_is_not(self) -> None:
        """An agent that could lift its own cost limit has authorized itself.

        Same shape as approving its own merge, so writes join the root-only
        list while inspection deliberately stays open -- an agent that cannot
        read the policy cannot report it either.
        """
        helm_root = self._helm_root("cli-authority")
        with mock.patch.dict(os.environ, {"HELM_WORKER_ID": "w-someworker"}):
            for argv in (
                ("prefs", "set", "agent.default", "pi"),
                ("prefs", "unset", "agent.default"),
                ("prefs", "migrate"),
            ):
                with self.subTest(argv=argv):
                    code, output = self._cli(helm_root, *argv)
                    self.assertEqual(code, 2)
                    self.assertIn("is the human's", output)
            self.assertEqual(self._cli(helm_root, "prefs", "show")[0], 0)
            self.assertEqual(self._cli(helm_root, "prefs", "keys")[0], 0)
        self.assertFalse((helm_root / "preferences.json").exists())


class PreferenceDocumentationTests(HelmTestCase):
    """The layer only works if a coordinator can tell the layers apart."""

    def test_both_agent_surfaces_distinguish_the_four_guidance_layers(self) -> None:
        agents = Path("AGENTS.md").read_text(encoding="utf-8")
        readme = Path("README.md").read_text(encoding="utf-8")
        for required in (
            "Root-local operator preferences",
            "preferences.json",
            "helm prefs show",
            "cannot authorize",
            "Non-secret only",
            "Backward compatible",
        ):
            self.assertIn(required, agents)
        for required in (
            "Root-local operator preferences",
            "preferences.json",
            "prefs set agent.exclude",
            "never carries the answers",
        ):
            self.assertIn(required, readme)

    def test_tracked_files_carry_no_operators_actual_choices(self) -> None:
        """The audit this layer exists to make permanent.

        Dated catalogue facts, prices, one root's cost verdicts and one
        commander's dated decisions all read as product policy once they are
        committed, and they are all wrong for the next person who clones this.
        """
        tracked = subprocess.run(
            ["git", "ls-files"], text=True, stdout=subprocess.PIPE, check=True
        ).stdout.splitlines()
        surfaces = [
            path
            for path in tracked
            if path in {"README.md", "AGENTS.md"}
            or path.startswith(("helm/", "domains/", "docs/", "scripts/"))
        ]
        banned = [
            # A price or a retirement date is true on one day and misleading
            # after it, and neither belongs in a committed file.
            (r"\$\d+ */ *\$\d+", "a price"),
            (r"retires? on \d{4}-\d{2}-\d{2}", "a retirement date"),
            (r"correct on \d{4}-\d{2}-\d{2}", "a dated catalogue snapshot"),
            # One root's decision, dated, reads as a rule everyone inherits.
            (r"commander granted that on", "a dated commander decision"),
            (r"is excluded on cost", "one root's exclusion stated as product policy"),
            (r"surprised this root", "a root-specific anecdote"),
            (r"on this laptop", "a claim about one machine"),
        ]
        offenders: list[str] = []
        for path in surfaces:
            try:
                text = Path(path).read_text(encoding="utf-8")
            except (UnicodeDecodeError, OSError):
                continue
            for pattern, why in banned:
                import re

                if re.search(pattern, text, re.IGNORECASE):
                    offenders.append(f"{path}: {why} ({pattern})")
        self.assertEqual(offenders, [])


if __name__ == "__main__":  # pragma: no cover
    import unittest

    unittest.main()


class EffortPreferenceTests(HelmTestCase):
    """A shipped capability is a default, not a limit: agents grow flags and
    models grow levels between Helm releases."""

    _roots = 0

    def _load(self, document: dict):
        import json
        from helm import preferences
        EffortPreferenceTests._roots += 1
        root = self._helm_root(f"effortprefs{EffortPreferenceTests._roots}")
        path = root / preferences.PREFERENCES_FILENAME
        path.write_text(
            json.dumps({"version": preferences.PREFERENCES_VERSION, **document}),
            encoding="utf-8",
        )
        return preferences.load(path)

    def test_a_default_effort_is_read(self) -> None:
        loaded = self._load({"effort": {"default": "high"}})
        self.assertEqual(loaded.default_effort, "high")

    def test_an_unknown_level_is_refused_at_load(self) -> None:
        from helm import preferences
        with self.assertRaises(preferences.PreferencesError):
            self._load({"effort": {"default": "turbo"}})

    def test_a_root_can_teach_a_runtime_helm_does_not_ship(self) -> None:
        loaded = self._load(
            {"effort": {"runtimes": {"newagent": "flag:--thinking:low,high"}}}
        )
        mechanism, argument, levels = loaded.effort_runtimes["newagent"]
        self.assertEqual((mechanism, argument), ("flag", "--thinking"))
        self.assertEqual(levels, frozenset({"low", "high"}))

    def test_a_taught_capability_cannot_smuggle_a_command(self) -> None:
        """The declaration is three narrow fields, not free text: an argument
        that is not a plain flag or key is refused rather than launched."""
        from helm import preferences
        with self.assertRaises(preferences.PreferencesError):
            self._load(
                {"effort": {"runtimes": {"x": "flag:--m; rm -rf /:low"}}}
            )

    def test_a_malformed_declaration_is_refused(self) -> None:
        from helm import preferences
        for spec in ("flag:--x", "sideways:--x:low", "flag:--x:"):
            with self.assertRaises(preferences.PreferencesError, msg=spec):
                self._load({"effort": {"runtimes": {"x": spec}}})


class ReviewAgentPreferenceTests(HelmTestCase):
    """A root's standing answer to "who checks the work".

    The reviewer runtime had to be repeated to every foreman, and was lost
    whenever one died mid-instruction. Three reviews in one afternoon landed on
    a runtime the commander had not asked for, each time silently -- a
    fallen-through default looks exactly like a considered pick.
    """

    def _rooted(self):
        from helm.core import Coordinator, StateStore

        root = self.repo("reviewpref").parent.parent
        return Coordinator(StateStore(root / "state", helm_root=root)), root

    def test_the_preference_survives_a_write_and_a_reload(self) -> None:
        from helm import preferences

        current = preferences.Preferences()
        updated = preferences.apply(current, "review.agent", ["cursor"])
        self.assertEqual(updated.review_agent, "cursor")
        # Through the on-disk shape and back: a value the serialiser drops is a
        # value that vanishes on the next command, which is how this key failed
        # the first time it was written.
        reloaded = preferences._from_document(updated.document(), None)
        self.assertEqual(reloaded.review_agent, "cursor")

    def test_unsetting_removes_the_whole_section(self) -> None:
        from helm import preferences

        current = preferences.apply(preferences.Preferences(), "review.agent", ["cursor"])
        cleared = preferences.apply(current, "review.agent", None)
        self.assertIsNone(cleared.review_agent)
        self.assertNotIn("review", cleared.document())

    def test_it_is_listed_by_prefs_keys_and_shown_when_set(self) -> None:
        from helm import preferences

        self.assertIn("review.agent", preferences.SUPPORTED_KEYS)
        current = preferences.apply(preferences.Preferences(), "review.agent", ["cursor"])
        self.assertIn(
            ("review.agent", "cursor"),
            [(key, value) for key, value in current.entries()],
        )

    def test_an_unknown_runtime_is_refused_rather_than_stored(self) -> None:
        from helm import preferences

        with self.assertRaises(preferences.PreferencesError):
            preferences.apply(preferences.Preferences(), "review.agent", ["not a runtime"])


class CleanupAfterMergeTests(PreferenceSchemaTests.__bases__[0]):
    def test_round_trips_and_rejects_unknown_values(self) -> None:
        path = Path(self.temp.name) / "prefs.json"
        path.write_text('{"version": 1, "cleanup": {"after_merge": "auto"}}')
        loaded = preferences.load(path)
        self.assertEqual(loaded.cleanup_after_merge, "auto")
        self.assertEqual(dict(loaded.document())["cleanup"], {"after_merge": "auto"})
        path.write_text('{"version": 1, "cleanup": {"after_merge": "always"}}')
        with self.assertRaisesRegex(preferences.PreferencesError, r"after_merge.*one of"):
            preferences.load(path)
        path.write_text('{"version": 1, "cleanup": {"pause": true}}')
        with self.assertRaisesRegex(preferences.PreferencesError, r"unknown|not supported|pause"):
            preferences.load(path)
