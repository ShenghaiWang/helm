"""Model inventory, cost classification and the free-model preference.

Three questions define this module: is ``model.free`` narrow and inert enough
to sit in the preferences ladder without ever overriding a pin; does the
catalogue surface classify free from explicit evidence only, with bounded
deterministic failures and no credential contact; and does the composed
selection context carry that evidence to the dispatcher without substituting
a model itself. The ids in fixture output are fabricated on purpose: Helm
commits no catalogue facts or prices.
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import shutil
import subprocess
from pathlib import Path
from unittest import mock

from helm import cli, models, preferences
from helm import runtimes as coordinator_runtimes
from helm.core import Coordinator, StateStore

from tests.support import HelmTestCase


def _unsupported(runtime_id: str) -> models.CatalogueResult:
    return models.CatalogueResult(
        runtime=runtime_id,
        command=(),
        supported=False,
        available=False,
        reason=models.UNSUPPORTED_REASON,
    )


def _pi_output() -> str:
    """A pi-shaped table. Header row exactly as `pi --list-models` prints it."""
    return (
        "provider      model                               context  max-out  thinking  images\n"
        "example       alpha-1                              1M       128K     yes       yes   \n"
        "example       alpha-1:free                         128K     32K      yes       no    \n"
        "example       beta-2:free                          64K      16K      no        no    \n"
        "gateway       vendor/paid-model                    200K     64K      yes       yes   \n"
        "opencode      example-tiny-free                    131K     64K      yes       no    \n"
        "garbage\n"
        "example       bad!id                               200K     64K      yes       yes   \n"
    )


def _opencode_output() -> str:
    """opencode prints one `provider/model` per line, no header."""
    return (
        "example/alpha-1\n"
        "example/alpha-1:free\n"
        "opencode/example-tiny-free\n"
        "opencode/example-paid\n"
        "openrouter/vendor/paid-model\n"
        "not a model id here\n"
        "example/alpha-1:free\n"
    )


def _catalogue_result(output: str, runtime_id: str, command: tuple[str, ...]) -> models.CatalogueResult:
    parser = models.PARSERS.get(runtime_id)
    return models.CatalogueResult(
        runtime=runtime_id,
        command=command,
        supported=True,
        available=True,
        reason="ok",
        models=tuple(
            models.ModelEntry(runtime=runtime_id, id=model_id, free=models.is_free_model(model_id))
            for model_id in (parser(output) if parser is not None else [])
        ),
    )


def _pi_result() -> models.CatalogueResult:
    return _catalogue_result(_pi_output(), "pi", ("pi", "--list-models"))


def _opencode_result() -> models.CatalogueResult:
    return _catalogue_result(_opencode_output(), "opencode", ("opencode", "models"))


class CatalogueParsingTests(HelmTestCase):
    """Exact ids survive the parser; nothing else is echoed back out."""

    def test_pi_rows_parse_into_exact_model_ids(self) -> None:
        ids = models.parse_pi_catalogue(_pi_output())
        self.assertEqual(
            ids,
            [
                "alpha-1",
                "alpha-1:free",
                "beta-2:free",
                "vendor/paid-model",
                "example-tiny-free",
            ],
        )

    def test_opencode_lines_parse_into_exact_model_ids(self) -> None:
        ids = models.parse_opencode_catalogue(_opencode_output())
        self.assertEqual(
            ids,
            [
                "example/alpha-1",
                "example/alpha-1:free",
                "opencode/example-tiny-free",
                "opencode/example-paid",
                "openrouter/vendor/paid-model",
            ],
        )

    def test_free_is_classified_only_from_an_explicit_marker(self) -> None:
        """A `:free` suffix anywhere, or the opencode gateway's `-free` ids,
        are evidence; a plausible name, a display rendering, or a memory are
        not."""
        self.assertTrue(models.is_free_model("example/alpha-1:free"))
        self.assertTrue(models.is_free_model("opencode/example-tiny-free"))
        # The `-free` claim is only the opencode gateway's to make; the same
        # suffix under another provider stays unknown.
        self.assertFalse(models.is_free_model("vendor/example-tiny-free"))
        # Names that merely sound free are unknown cost, not known-free.
        self.assertFalse(models.is_free_model("example/alpha-1"))
        self.assertFalse(models.is_free_model("example/free-model"))
        self.assertFalse(models.is_free_model("example/zero-cost-display"))
        self.assertFalse(models.is_free_model("example/alpha-1:free:trial"))

    def test_catalogue_entries_carry_the_runtime_that_reported_them(self) -> None:
        pi = _pi_result()
        self.assertEqual(
            [entry.id for entry in pi.models if entry.free],
            ["alpha-1:free", "beta-2:free"],
        )
        self.assertTrue(all(entry.runtime == "pi" for entry in pi.models))
        self.assertEqual(
            list(_opencode_result().free_models),
            ["example/alpha-1:free", "opencode/example-tiny-free"],
        )


class CatalogueQueryTests(HelmTestCase):
    """Bounded, deterministic outcomes; no credential contact."""

    def _query(self, which: str | None = "/usr/bin/pi", **kwargs: object) -> models.CatalogueResult:
        return models.query_catalogue("pi", which=lambda _: which, **kwargs)

    def test_missing_executable_is_a_deterministic_reason(self) -> None:
        result = self._query(which=None)
        self.assertFalse(result.available)
        self.assertIn("catalogue executable not found: pi", result.reason)
        self.assertEqual(result.models, ())

    def test_a_nonzero_exit_is_reported_not_parsed(self) -> None:
        with mock.patch("helm.models.subprocess.run", return_value=_Completed(returncode=3)):
            result = self._query()
        self.assertFalse(result.available)
        self.assertEqual(result.reason, "catalogue command exited 3")

    def test_a_hung_catalogue_is_abandoned_within_the_bound(self) -> None:
        with mock.patch(
            "helm.models.subprocess.run",
            side_effect=subprocess.TimeoutExpired(cmd="pi --list-models", timeout=8),
        ):
            result = self._query()
        self.assertFalse(result.available)
        self.assertEqual(result.reason, "catalogue command timed out after 8s")

    def test_a_spawn_failure_is_reported_without_reaching_output(self) -> None:
        with mock.patch("helm.models.subprocess.run", side_effect=OSError("spawn failed")):
            result = self._query()
        self.assertFalse(result.available)
        self.assertIn("spawn failed", result.reason)

    def test_output_with_no_parseable_rows_is_reported_not_echoed(self) -> None:
        with mock.patch(
            "helm.models.subprocess.run", return_value=_Completed(returncode=0, stdout="wow $$$ model")
        ):
            result = self._query()
        self.assertFalse(result.available)
        self.assertIn("no parseable model rows", result.reason)

    def test_the_query_captures_stdout_only_and_never_injects_environment(self) -> None:
        with mock.patch(
            "helm.models.subprocess.run",
            return_value=_Completed(returncode=0, stdout=_pi_output()),
        ) as run:
            result = self._query()
        self.assertTrue(result.available)
        call = run.call_args
        self.assertEqual(call.args[0], ("pi", "--list-models"))
        self.assertIs(call.kwargs.get("stderr"), subprocess.DEVNULL)
        self.assertIs(call.kwargs.get("stdout"), subprocess.PIPE)
        self.assertIsNone(call.kwargs.get("env"), "a catalogue query must not inject env")

    def test_runtimes_without_a_safe_catalogue_command_are_unsupported(self) -> None:
        for runtime_id in ("claude", "codex", "omp"):
            result = models.query_catalogue(runtime_id, which=lambda _: "/usr/bin/x")
            self.assertFalse(result.supported, runtime_id)
            self.assertFalse(result.available)
            self.assertEqual(result.reason, models.UNSUPPORTED_REASON)


class _Completed:
    def __init__(self, returncode: int, stdout: str = "") -> None:
        self.returncode = returncode
        self.stdout = stdout


class FreePreferenceTests(HelmTestCase):
    """`model.free` is typed, narrow, and inert in the resolution ladder."""

    def test_the_key_is_documented_and_validates_its_vocabulary(self) -> None:
        self.assertIsNone(preferences.split_model_runtimes_key("model.free"))
        self.assertFalse(preferences.takes_list("model.free"))
        self.assertIn("model.free", preferences.SUPPORTED_KEYS)
        for value in preferences.FREE_MODEL_VALUES:
            path = Path(self.temp.name) / f"prefs-{value}.json"
            path.write_text(json.dumps({"version": preferences.PREFERENCES_VERSION,
                                        "model": {"free": value}}))
            self.assertEqual(preferences.load(path).free_model, value)
        for value in ("always", "free", "auto", 1, ""):
            with self.subTest(value=value):
                with self.assertRaises(preferences.PreferencesError):
                    preferences.apply(preferences.EMPTY, preferences.KEY_MODEL_FREE, [value])

    def test_set_unset_and_round_trip_through_the_cli(self) -> None:
        helm_root = self._helm_root("free-prefs")
        code, output = self._cli(helm_root, "prefs", "set", "model.free", "prefer")
        self.assertEqual(code, 0, output)
        loaded = preferences.load(helm_root / "preferences.json")
        self.assertEqual(loaded.free_model, "prefer")
        self.assertEqual(dict(loaded.entries())["model.free"], "prefer")
        self.assertEqual(self._cli(helm_root, "prefs", "unset", "model.free")[0], 0)
        self.assertIsNone(preferences.load(helm_root / "preferences.json").free_model)

    def test_the_preference_never_changes_the_resolution_ladder(self) -> None:
        """`model.free` is evidence for a dispatcher, not a model choice."""
        helm_root = self._helm_root("free-ladder")
        project_root = self.repo("ladder")
        shutil.move(str(project_root), str(helm_root / "projects" / "ladder"))
        self.write_preferences(
            helm_root, model={"default": "preference-model", "free": "prefer"}
        )
        coordinator = Coordinator(StateStore(helm_root / "state", helm_root=helm_root))
        project = coordinator.discover_project(helm_root, "ladder")
        with mock.patch.dict(os.environ, {"HELM_MODEL": ""}):
            task = coordinator.create_task(project["id"], "work")
        model, reason = coordinator._resolve_model(project, task)
        self.assertIsNone(task["model"])  # nothing auto-named leaked into the record
        self.assertEqual(model, "preference-model")  # model.default still wins
        self.assertNotIn("free", reason.lower())
        with mock.patch.dict(os.environ, {"HELM_MODEL": "env-model"}):
            task = coordinator.create_task(project["id"], "work")
            model, _ = coordinator._resolve_model(project, task)
        self.assertEqual(model, "env-model")  # the environment outranks everything

    def _cli(self, helm_root: Path, *argv: str) -> tuple[int, str]:
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer), contextlib.redirect_stderr(buffer):
            code = cli.main(["--root", str(helm_root), *argv])
        return code, buffer.getvalue()


class ReadinessAndCliTests(HelmTestCase):
    """Excluded runtimes read as excluded; catalogues are queried only for
    launchable, non-excluded runtimes; output is stable in text and JSON."""

    def _root(self, name: str = "models") -> tuple[Coordinator, Path]:
        helm_root = self._helm_root(f"{name}-root")
        project_root = self.repo(name)
        shutil.move(str(project_root), str(helm_root / "projects" / name))
        coordinator = Coordinator(StateStore(helm_root / "state", helm_root=helm_root))
        return coordinator, helm_root

    def _fake_query(self, raise_for: set[str], queried: list[str]) -> models.query_catalogue:
        def fake(runtime_id: str) -> models.CatalogueResult:
            queried.append(runtime_id)
            if runtime_id in raise_for:
                raise AssertionError(f"catalogue {runtime_id} must not be queried")
            if runtime_id == "pi":
                return _pi_result()
            if runtime_id == "opencode":
                return _opencode_result()
            return _unsupported(runtime_id)

        return fake

    def test_runtime_readiness_reports_exclusions_and_the_root_default(self) -> None:
        coordinator, helm_root = self._root()
        self.write_preferences(
            helm_root, agent={"default": "pi"}, model={"free": "prefer"}
        )
        # setUp forces HELM_AGENT=none for isolation; clear it here so the
        # root preference is the thing under test, not the harness default.
        with mock.patch.dict(
            os.environ, {"HELM_EXCLUDE_AGENTS": "opencode", "HELM_AGENT": ""}
        ):
            rows = coordinator.builtin_runtime_availability()
        by_id = {row["id"]: row for row in rows}
        self.assertTrue(by_id["pi"]["default"])
        self.assertFalse(by_id["pi"]["excluded"])
        self.assertTrue(by_id["opencode"]["excluded"])
        self.assertFalse(by_id["claude"]["excluded"])

    def test_runtime_default_prefers_helm_agent_over_preferences(self) -> None:
        coordinator, helm_root = self._root()
        self.write_preferences(helm_root, agent={"default": "pi"})
        with mock.patch.dict(os.environ, {"HELM_AGENT": "opencode"}):
            rows = coordinator.builtin_runtime_availability()
        by_id = {row["id"]: row for row in rows}
        self.assertTrue(by_id["opencode"]["default"])
        self.assertFalse(by_id["pi"]["default"])

    def test_runtime_default_never_marks_an_excluded_runtime(self) -> None:
        coordinator, helm_root = self._root()
        self.write_preferences(helm_root, agent={"default": "pi"})
        with mock.patch.dict(
            os.environ, {"HELM_AGENT": "pi", "HELM_EXCLUDE_AGENTS": "pi"}
        ):
            rows = coordinator.builtin_runtime_availability()
        by_id = {row["id"]: row for row in rows}
        self.assertTrue(by_id["pi"]["excluded"])
        self.assertFalse(by_id["pi"]["default"])

    def test_agent_models_queries_only_launchable_non_excluded_catalogues(self) -> None:
        coordinator, helm_root = self._root()
        self.write_preferences(helm_root, model={"free": "prefer"})
        queried: list[str] = []
        with mock.patch(
            "helm.cli.models.query_catalogue",
            side_effect=self._fake_query(raise_for={"opencode"}, queried=queried),
        ), mock.patch("helm.core.Coordinator._check_command", return_value=(True, "ok")), \
            mock.patch.dict(os.environ, {"HELM_EXCLUDE_AGENTS": "opencode"}):
            report = cli._agent_models_report(coordinator)
        self.assertNotIn("opencode", queried)
        by_id = {entry["id"]: entry for entry in report["runtimes"]}
        self.assertEqual(
            by_id["opencode"]["catalogue"]["reason"],
            "skipped: this root excludes the runtime",
        )
        self.assertEqual(by_id["pi"]["catalogue"]["available"], True)
        self.assertEqual(
            by_id["pi"]["catalogue"]["models"][1],
            {"id": "alpha-1:free", "cost": "free"},
        )

    def _write_agents(self, helm_root: Path, agents: list[dict]) -> None:
        (helm_root / "agents.json").write_text(json.dumps({"agents": agents}))

    def test_agent_models_reuses_catalogue_for_declared_runtime_with_no_command(
        self,
    ) -> None:
        coordinator, helm_root = self._root()
        self.write_preferences(helm_root, model={"free": "prefer"})
        self._write_agents(
            helm_root,
            [
                {"id": "pi-alias", "runtime": "pi"},
                {"id": "opaque", "command": ["some-wrapper-script"]},
            ],
        )
        queried: list[str] = []
        buffer = io.StringIO()
        with mock.patch(
            "helm.cli.models.query_catalogue",
            side_effect=self._fake_query(raise_for=set(), queried=queried),
        ), mock.patch("helm.core.Coordinator._check_command", return_value=(True, "ok")), \
            contextlib.redirect_stdout(buffer):
            report = cli._agent_models_report(coordinator)
            queried.clear()
            cli._print_agent_models(coordinator, as_json=False)
        # The profile's own command is never run -- and there is none here --
        # so "pi" is queried exactly once, for the built-in row.
        self.assertEqual(queried.count("pi"), 1)
        by_id = {entry["id"]: entry for entry in report["runtimes"]}
        self.assertTrue(by_id["pi-alias"]["catalogue"]["available"])
        self.assertEqual(
            by_id["pi-alias"]["catalogue"]["models"],
            by_id["pi"]["catalogue"]["models"],
        )
        self.assertFalse(by_id["opaque"]["catalogue"]["supported"])
        self.assertIn(
            "does not provably invoke a built-in runtime",
            by_id["opaque"]["catalogue"]["reason"],
        )
        self.assertIn("pi-alias   ", buffer.getvalue())
        self.assertRegex(buffer.getvalue(), r"pi-alias\s+.*\s+ok\s")

    def test_agent_models_refuses_a_declared_runtime_that_disagrees_with_its_command(
        self,
    ) -> None:
        coordinator, helm_root = self._root()
        self.write_preferences(helm_root, model={"free": "prefer"})
        self._write_agents(
            helm_root,
            [{"id": "mislabeled", "runtime": "pi", "command": ["opencode", "models"]}],
        )
        with mock.patch(
            "helm.cli.models.query_catalogue",
            side_effect=self._fake_query(raise_for=set(), queried=[]),
        ), mock.patch("helm.core.Coordinator._check_command", return_value=(True, "ok")):
            report = cli._agent_models_report(coordinator)
        by_id = {entry["id"]: entry for entry in report["runtimes"]}
        self.assertFalse(by_id["mislabeled"]["catalogue"]["supported"])
        self.assertIn(
            "does not provably invoke a built-in runtime",
            by_id["mislabeled"]["catalogue"]["reason"],
        )

    def test_agent_models_does_not_credit_a_same_named_program_at_a_different_path(
        self,
    ) -> None:
        coordinator, helm_root = self._root()
        self.write_preferences(helm_root, model={"free": "prefer"})
        self._write_agents(
            helm_root, [{"id": "impostor", "command": ["/opt/rogue/pi", "serve"]}]
        )

        def fake_which(name: str) -> str | None:
            if name == "/opt/rogue/pi":
                return "/opt/rogue/pi"
            if name == "pi":
                return "/usr/local/bin/pi"
            return None

        with mock.patch(
            "helm.cli.models.query_catalogue",
            side_effect=self._fake_query(raise_for=set(), queried=[]),
        ), mock.patch("helm.core.Coordinator._check_command", return_value=(True, "ok")), \
            mock.patch("helm.cli.shutil.which", side_effect=fake_which):
            report = cli._agent_models_report(coordinator)
        by_id = {entry["id"]: entry for entry in report["runtimes"]}
        # Same basename, different file on disk: never credited with pi's
        # catalogue, and pi's own catalogue must not have been re-queried
        # under the impostor's path.
        self.assertFalse(by_id["impostor"]["catalogue"]["supported"])
        self.assertIn(
            "does not provably invoke a built-in runtime",
            by_id["impostor"]["catalogue"]["reason"],
        )

    def test_agent_models_credits_the_exact_same_executable_by_a_different_path(
        self,
    ) -> None:
        coordinator, helm_root = self._root()
        self.write_preferences(helm_root, model={"free": "prefer"})
        self._write_agents(
            helm_root, [{"id": "vendored", "command": ["/opt/vendor/pi", "serve"]}]
        )

        def fake_which(name: str) -> str | None:
            if name in ("/opt/vendor/pi", "pi"):
                return "/usr/local/bin/pi"
            return None

        queried: list[str] = []
        with mock.patch(
            "helm.cli.models.query_catalogue",
            side_effect=self._fake_query(raise_for=set(), queried=queried),
        ), mock.patch("helm.core.Coordinator._check_command", return_value=(True, "ok")), \
            mock.patch("helm.cli.shutil.which", side_effect=fake_which):
            report = cli._agent_models_report(coordinator)
        by_id = {entry["id"]: entry for entry in report["runtimes"]}
        self.assertTrue(by_id["vendored"]["catalogue"]["available"])
        self.assertEqual(
            by_id["vendored"]["catalogue"]["models"], by_id["pi"]["catalogue"]["models"]
        )
        self.assertEqual(queried.count("pi"), 1)

    def test_agent_models_marks_a_profile_excluded_for_its_inherited_runtime(
        self,
    ) -> None:
        coordinator, helm_root = self._root()
        self.write_preferences(helm_root, model={"free": "prefer"})
        self._write_agents(helm_root, [{"id": "codex-wrapper", "runtime": "codex"}])
        queried: list[str] = []
        with mock.patch(
            "helm.cli.models.query_catalogue",
            side_effect=self._fake_query(raise_for={"codex"}, queried=queried),
        ), mock.patch("helm.core.Coordinator._check_command", return_value=(True, "ok")), \
            mock.patch.dict(os.environ, {"HELM_EXCLUDE_AGENTS": "codex"}):
            report = cli._agent_models_report(coordinator)
        by_id = {entry["id"]: entry for entry in report["runtimes"]}
        self.assertTrue(by_id["codex-wrapper"]["excluded"])
        self.assertEqual(
            by_id["codex-wrapper"]["catalogue"]["reason"],
            "skipped: this root excludes the runtime",
        )
        self.assertNotIn("codex", queried)

    def test_agent_models_default_marks_this_session_when_only_detected(
        self,
    ) -> None:
        coordinator, helm_root = self._root()
        self.write_preferences(helm_root, model={"free": "prefer"})
        queried: list[str] = []
        buffer = io.StringIO()
        with mock.patch(
            "helm.cli.models.query_catalogue",
            side_effect=self._fake_query(raise_for=set(), queried=queried),
        ), mock.patch("helm.core.Coordinator._check_command", return_value=(True, "ok")), \
            mock.patch.dict(os.environ, {"HELM_AGENT": ""}), \
            mock.patch(
                "helm.core.runtimes.detect_runtime",
                return_value=next(
                    r for r in coordinator_runtimes.BUILTIN_RUNTIMES if r.id == "pi"
                ),
            ), contextlib.redirect_stdout(buffer):
            report = cli._agent_models_report(coordinator)
            cli._print_agent_models(coordinator, as_json=False)
        by_id = {entry["id"]: entry for entry in report["runtimes"]}
        self.assertTrue(by_id["pi"]["default"])
        self.assertTrue(by_id["pi"]["default_reason"].startswith("same runtime"))
        self.assertIn("[this session]", buffer.getvalue())
        self.assertNotIn("[root default]", buffer.getvalue())

    def test_agent_models_json_is_stable_and_deterministic(self) -> None:
        coordinator, helm_root = self._root()
        self.write_preferences(helm_root, model={"free": "prefer"})
        queried: list[str] = []
        with mock.patch(
            "helm.cli.models.query_catalogue",
            side_effect=self._fake_query(raise_for=set(), queried=queried),
        ), mock.patch("helm.core.Coordinator._check_command", return_value=(True, "ok")):
            report = cli._agent_models_report(coordinator)
        self.assertEqual(report["schema_version"], 1)
        self.assertEqual(report["preference"], {"key": "model.free", "value": "prefer"})
        self.assertEqual(report["decision_order"][-1], "cost")
        self.assertEqual(len(report["rules"]), 5)
        by_id = {entry["id"]: entry for entry in report["runtimes"]}
        self.assertEqual(by_id["claude"]["catalogue"]["supported"], False)
        free_ids = {entry["id"] for entry in report["free_evidence"]}
        self.assertEqual(
            free_ids,
            {
                "alpha-1:free",
                "beta-2:free",
                "example/alpha-1:free",
                "opencode/example-tiny-free",
            },
        )
        self.assertNotIn("generated_at", report)
        self.assertNotIn("api_key", json.dumps(report))

    def test_agent_models_text_output_names_provenance_and_exclusion(self) -> None:
        coordinator, helm_root = self._root()
        self.write_preferences(helm_root, model={"free": "prefer"})
        queried: list[str] = []
        buffer = io.StringIO()
        with mock.patch(
            "helm.cli.models.query_catalogue",
            side_effect=self._fake_query(raise_for=set(), queried=queried),
        ), mock.patch("helm.core.Coordinator._check_command", return_value=(True, "ok")), \
            mock.patch.dict(os.environ, {"HELM_EXCLUDE_AGENTS": "omp"}), \
            contextlib.redirect_stdout(buffer):
            cli._print_agent_models(coordinator, as_json=False)
        output = buffer.getvalue()
        self.assertIn("preference: model.free = prefer", output)
        self.assertIn("decision order: ", output)
        self.assertIn("[excluded by root]", output)
        self.assertIn("free (explicit catalogue evidence only):", output)
        self.assertIn("alpha-1:free  (pi)", output)
        self.assertIn("opencode/example-tiny-free  (opencode)", output)


class SelectionContextTests(HelmTestCase):
    """The composed context carries preference + evidence to the dispatcher."""

    def _context_root(self) -> tuple[Coordinator, dict, Path]:
        helm_root = self._helm_root("selection-root")
        project_root = self.repo("selection")
        shutil.move(str(project_root), str(helm_root / "projects" / "selection"))
        coordinator = Coordinator(StateStore(helm_root / "state", helm_root=helm_root))
        return coordinator, coordinator.discover_project(helm_root, "selection"), helm_root

    def _task(self, coordinator: Coordinator, project: dict) -> dict:
        return coordinator.create_task(project["id"], "a well-specified change")

    def _section(self, context: dict, section_id: str) -> dict:
        return next(
            entry for entry in context["context_sections"] if entry["kind"] == section_id
        )

    def test_with_the_preference_set_the_context_carries_bounded_evidence(self) -> None:
        coordinator, project, helm_root = self._context_root()
        self.write_preferences(helm_root, model={"free": "prefer"})
        task = self._task(coordinator, project)
        with mock.patch(
            "helm.core.models.query_launchable_catalogues",
            return_value=[_pi_result(), _opencode_result()],
        ), mock.patch("helm.core.Coordinator._check_command", return_value=(True, "ok")):
            context = coordinator._context(project, task, "w-1")
        payload = json.loads(self._section(context, "model-selection")["content"])
        self.assertEqual(payload["preference"], {"key": "model.free", "value": "prefer"})
        self.assertEqual(payload["decision_order"], coordinator.MODEL_SELECTION_ORDER)
        free = {(item["id"], item["runtime"]) for item in payload["free_evidence"]}
        self.assertIn(("alpha-1:free", "pi"), free)
        self.assertIn(("opencode/example-tiny-free", "opencode"), free)
        rules = " ".join(payload["rules"])
        self.assertIn("never silently substitute a model", rules)
        self.assertIn("Fit is always filtered before cost", rules)

    def test_without_the_preference_no_catalogue_is_queried_and_no_section_appears(
        self,
    ) -> None:
        coordinator, project, helm_root = self._context_root()
        task = self._task(coordinator, project)
        with mock.patch(
            "helm.core.models.query_launchable_catalogues",
            side_effect=AssertionError("no preference, no query"),
        ):
            context = coordinator._context(project, task, "w-2")
        self.assertNotIn(
            "model-selection", [entry["kind"] for entry in context["context_sections"]]
        )

    def test_an_excluded_runtime_never_supplies_evidence(self) -> None:
        """An exclusion is a decision about what may run at all, so it also
        decides what may be queried: expensively-launchable runtimes have no
        reason to be cost-consulted behind the root's decision."""
        coordinator, project, helm_root = self._context_root()
        self.write_preferences(helm_root, model={"free": "prefer"})
        task = self._task(coordinator, project)
        launchable_requested: list[set[str]] = []
        with mock.patch(
            "helm.core.models.query_launchable_catalogues",
            side_effect=lambda ids: launchable_requested.append(set(ids)) or [],
        ), mock.patch.dict(os.environ, {"HELM_EXCLUDE_AGENTS": "pi,opencode"}), \
            mock.patch("helm.core.Coordinator._check_command", return_value=(True, "ok")):
            evidence = coordinator.model_selection_evidence()
        self.assertEqual(
            launchable_requested[-1],
            {"claude", "codex", "omp", "cursor"},
            "exclusions prune first",
        )
        self.assertEqual(evidence["free_evidence"], [])