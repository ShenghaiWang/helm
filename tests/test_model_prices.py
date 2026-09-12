"""A root's own price list turns transcript tokens into dollars, honestly."""

from __future__ import annotations

import contextlib
import io
import json
import sys
from pathlib import Path
from unittest import mock

from helm import cli, costs, preferences
from helm.core import Coordinator
from helm.state import StateStore
from tests.support import HelmTestCase


def _cli(helm_root: Path, *argv: str) -> tuple[int, str]:
    buffer = io.StringIO()
    with contextlib.redirect_stdout(buffer), contextlib.redirect_stderr(buffer):
        code = cli.main(["--root", str(helm_root), *argv])
    return code, buffer.getvalue()


class ModelPricePreferenceTests(HelmTestCase):
    def _load(self, document: dict) -> preferences.Preferences:
        path = Path(self.temp.name) / "prices-preferences.json"
        path.write_text(json.dumps(document), encoding="utf-8")
        return preferences.load(path)

    def test_prices_round_trip_and_resolve_by_exact_id_or_longest_prefix(self) -> None:
        loaded = self._load({
            "version": 1,
            "model": {"prices": {
                "some-model-5": "in=1,out=5,cache_read=0.1,cache_write=1.25",
                "some-model-5-mini": "in=0.2,out=1",
            }},
        })
        self.assertEqual(loaded.model_prices["some-model-5"]["out"], 5.0)
        self.assertEqual(loaded.price_for("some-model-5")["in"], 1.0)
        self.assertEqual(loaded.price_for("some-model-5-20260101")["cache_write"], 1.25)
        self.assertEqual(loaded.price_for("some-model-5-mini-20260101")["in"], 0.2)
        self.assertIsNone(loaded.price_for("other-model-9"))
        self.assertIsNone(loaded.price_for(None))
        # A flat rate covers every model not priced by name, a nameless turn
        # included; a name still wins over it.
        flat = self._load({"version": 1, "model": {"prices": {
            "*": "in=2,out=2,cache_read=2,cache_write=2",
            "some-model-5": "in=1,out=5",
        }}})
        self.assertEqual(flat.price_for("other-model-9")["out"], 2.0)
        self.assertEqual(flat.price_for("")["in"], 2.0)
        self.assertEqual(flat.price_for("some-model-5-20260101")["out"], 5.0)
        self.assertEqual(flat.document()["model"]["prices"]["*"], "in=2,out=2,cache_read=2,cache_write=2")
        self.assertIn(("model.prices.*", "in=2,out=2,cache_read=2,cache_write=2"), flat.entries())
        document = loaded.document()
        self.assertEqual(document["model"]["prices"]["some-model-5-mini"], "in=0.2,out=1")
        self.assertIn(("model.prices.some-model-5", "in=1,out=5,cache_read=0.1,cache_write=1.25"), loaded.entries())

    def test_a_bad_price_is_refused_at_load_with_the_field_named(self) -> None:
        for spec, message in (
            ("in=1", "must give out="),
            ("out=1", "must give in="),
            ("in=1,out=-5", "between 0 and"),
            ("in=1,out=five", "must be a number"),
            ("in=1,out=5,per_call=2", "is not one of"),
            ("in=1,out=5,in=2", "given twice"),
            ("", "must be a string"),
        ):
            with self.assertRaisesRegex(preferences.PreferencesError, message, msg=spec):
                self._load({"version": 1, "model": {"prices": {"some-model-5": spec}}})
        with self.assertRaises(preferences.PreferencesError):
            self._load({"version": 1, "model": {"prices": {"not a model id!": "in=1,out=5"}}})

    def test_the_cli_sets_shows_and_unsets_a_price(self) -> None:
        helm_root = self._helm_root("priced-root")
        code, output = _cli(helm_root, "prefs", "set", "model.prices.some-model-5", "in=1,out=5")
        self.assertEqual(code, 0, output)
        code, output = _cli(helm_root, "prefs", "show")
        self.assertIn("model.prices.some-model-5 = in=1,out=5", output)
        code, output = _cli(helm_root, "prefs", "set", "model.prices.some-model-5", "in=1")
        self.assertNotEqual(code, 0)
        self.assertEqual(_cli(helm_root, "prefs", "unset", "model.prices.some-model-5")[0], 0)
        self.assertEqual(preferences.load(helm_root / "preferences.json").model_prices, {})
        code, output = _cli(helm_root, "prefs", "set", "model.prices.*", "in=3,out=3,cache_read=3,cache_write=3")
        self.assertEqual(code, 0, output)
        self.assertEqual(preferences.load(helm_root / "preferences.json").price_for("anything-at-all")["in"], 3.0)
        self.assertEqual(_cli(helm_root, "prefs", "unset", "model.prices.*")[0], 0)


class PriceUsageTests(HelmTestCase):
    def test_tokens_are_priced_per_model_and_a_gap_leaves_the_figure_blank(self) -> None:
        prices = {"some-model-5": {"in": 1.0, "out": 5.0, "cache_read": 0.1, "cache_write": 1.25}}
        price_for = lambda model: prices.get(model)  # noqa: E731
        by_model = {"some-model-5": {
            "input_tokens": 1_000_000, "output_tokens": 200_000,
            "cache_read_input_tokens": 10_000_000, "cache_creation_input_tokens": 400_000,
        }}
        priced = costs.price_usage(by_model, price_for)
        # 1 + 1 + 1 + 0.5
        self.assertAlmostEqual(priced["cost_usd"], 3.5)
        self.assertEqual(priced["priced"], ["some-model-5"])
        # A second model with no price blanks the whole figure and says which.
        by_model["other-model-9"] = {"input_tokens": 10, "output_tokens": 0,
                                     "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0}
        priced = costs.price_usage(by_model, price_for)
        self.assertIsNone(priced["cost_usd"])
        self.assertEqual(priced["unpriced"], ["other-model-9"])
        # Cache tokens spent with no cache rate are a gap too, named as such.
        prices["some-model-5"] = {"in": 1.0, "out": 5.0}
        del by_model["other-model-9"]
        priced = costs.price_usage(by_model, price_for)
        self.assertIsNone(priced["cost_usd"])
        self.assertIn("no cache_read, cache_write rate", priced["unpriced"][0])
        # A model that spent nothing is not a gap.
        self.assertEqual(costs.price_usage({"": {f: 0 for f in costs.USAGE_FIELDS}}, price_for)["unpriced"], [])
        self.assertIsNone(costs.price_usage({}, price_for)["cost_usd"])


class PricedLedgerTests(HelmTestCase):
    def _usage(self, worker: dict) -> dict:
        usage = {field: 0 for field in costs.USAGE_FIELDS}
        usage.update({
            "worker_id": worker.get("id"), "agent": "claude", "transcripts": ["t"], "turns": 3,
            "models": ["some-model-5"], "cost_usd": None, "cost_source": None, "metered": True,
            "input_tokens": 2_000_000, "output_tokens": 100_000,
            "by_model": {"some-model-5": {
                "input_tokens": 2_000_000, "output_tokens": 100_000,
                "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0,
            }},
        })
        return usage

    def test_task_cost_and_the_ledger_price_what_the_runtime_did_not_report(self) -> None:
        helm_root = self._helm_root("ledger-priced")
        coordinator = Coordinator(StateStore(helm_root / "state", helm_root=helm_root))
        (helm_root / "preferences.json").write_text(
            json.dumps({"version": 1, "model": {"prices": {"some-model-5": "in=1,out=5"}}}), encoding="utf-8"
        )
        repo = self.repo("priced")
        project = coordinator.register_project("Priced", str(repo), project_id="priced")
        task = coordinator.create_task(project["id"], "change something")
        coordinator.launch_worker(task["id"], [sys.executable, "-c", ""])
        with mock.patch.object(costs, "worker_usage", side_effect=self._usage):
            usage = coordinator.task_usage(task["id"])
            self.assertAlmostEqual(usage["workers"][0]["cost_usd"], 2.5)
            self.assertEqual(usage["workers"][0]["cost_source"], "priced")
            self.assertTrue(usage["total"]["cost_known"])
            self.assertEqual(usage["total"]["priced"], 1)
            report = coordinator.ledger(days=1)
            self.assertEqual(report["rows"][0]["cost_source"], "priced")
            self.assertAlmostEqual(report["rows"][0]["cost_usd"], 2.5)
            self.assertEqual(report["totals"]["priced_for"], 1)
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                self.assertEqual(cli.main(["--root", str(helm_root), "task", "cost", task["id"]]), 0)
                self.assertEqual(cli.main(["--root", str(helm_root), "ledger", "--days", "1"]), 0)
            text = out.getvalue()
            self.assertIn("cost=$2.50 (priced)", text)
            self.assertIn("1 priced from model.prices", text)
        # Without a price the figure stays blank and the model is named.
        (helm_root / "preferences.json").unlink()
        with mock.patch.object(costs, "worker_usage", side_effect=self._usage):
            usage = coordinator.task_usage(task["id"])
            self.assertIsNone(usage["workers"][0]["cost_usd"])
            self.assertEqual(usage["total"]["unpriced_models"], ["some-model-5"])
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                cli.main(["--root", str(helm_root), "task", "cost", task["id"]])
            self.assertIn("unpriced: some-model-5 (set model.prices.<model>)", out.getvalue())
