"""A worker pulls its guidance; a reviewer is pushed the standards; the size is a number."""

from __future__ import annotations

import contextlib
import io
import json
import shutil
import sys
from pathlib import Path

from helm import cli
from helm.core import Coordinator
from helm.state import StateStore
from helm.values import CONTEXT_BASE_KNOWLEDGE_BUDGET_BYTES, REVIEW_DOMAINS
from tests.support import SHIPPED_DOMAINS, HelmTestCase


class ContextDietTests(HelmTestCase):
    def _rooted(self, name: str) -> tuple[Path, Coordinator, dict]:
        helm_root = self._helm_root(f"{name}-root")
        shutil.rmtree(helm_root / "domains")
        shutil.copytree(SHIPPED_DOMAINS, helm_root / "domains")
        shutil.move(str(self.repo(name)), str(helm_root / "projects" / name))
        coordinator = Coordinator(StateStore(helm_root / "state", helm_root=helm_root))
        coordinator.discover_project(helm_root, name)
        coordinator.set_project_domains(name, ["software-delivery"])
        return helm_root, coordinator, coordinator.get_project(name)

    @staticmethod
    def _by_kind(context: dict, kind: str) -> list[dict]:
        return [s for s in context["context_sections"] if s["kind"] == kind]

    def test_a_worker_gets_the_selected_domain_whole_and_the_bases_within_budget(self) -> None:
        helm_root, coordinator, project = self._rooted("dieted")
        task = coordinator.create_task(project["id"], "do the thing")
        worker = coordinator.launch_worker(task["id"], [sys.executable, "-c", ""])
        context = json.loads(Path(worker["context_file"]).read_text(encoding="utf-8"))
        full = self._by_kind(context, "domain-knowledge")
        indexes = self._by_kind(context, "domain-index")
        self.assertTrue(indexes, "the shipped software-delivery chain is far over any sensible budget")
        # The selected domain is always whole, and every guardrails file is.
        selected = (helm_root / "domains" / "software-delivery" / "knowledge.md").read_text(encoding="utf-8")
        self.assertEqual(context["domain"]["knowledge"], selected)
        self.assertEqual(
            len(self._by_kind(context, "domain-guardrails")), len(context["domain_chain"]),
        )
        # The bases handed over whole fit the budget; the rest are indexed.
        budgeted = [
            s for s in full
            if not s["source"].endswith("software-delivery/knowledge.md")
            and "always_in_full: true" not in s["content"]
        ]
        self.assertLessEqual(sum(len(s["content"]) for s in budgeted), CONTEXT_BASE_KNOWLEDGE_BUDGET_BYTES)
        for section in indexes:
            domain_id = Path(section["source"]).parent.name
            self.assertIn(domain_id, context["domain"]["indexed"])
            self.assertIn(f"{domain_id}:", section["content"])
            self.assertIn("Sections:", section["content"])
            self.assertIn(f"-m helm --state-dir", section["content"])
            self.assertIn(f"guide {domain_id}", section["content"])
        # Bases first, selected last, still.
        kinds = [s["kind"] for s in context["context_sections"]]
        self.assertEqual(kinds[0], "core-safety")
        self.assertEqual(kinds[-1], "task")
        # The size is recorded where inspect and the ledger read it.
        self.assertEqual(context["size"]["bytes"], sum(len(s["content"]) for s in context["context_sections"]))
        self.assertLess(context["size"]["bytes"], 80_000)
        # A domain that declares itself always in full is never indexed.
        full_ids = {Path(s["source"]).parent.name for s in full}
        self.assertIn("spec-driven-development", full_ids)
        self.assertNotIn("spec-driven-development", context["domain"]["indexed"])
        recorded = coordinator.store.load()["tasks"][task["id"]]
        self.assertEqual(recorded["context_bytes"], context["size"]["bytes"])
        self.assertEqual(recorded["context_indexed"], context["domain"]["indexed"])
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(cli.main(["--root", str(helm_root), "task", "inspect", task["id"]]), 0)
        self.assertIn("KB handed to the worker; indexed rather than in full:", out.getvalue())
        row = coordinator.ledger(days=1)["rows"][0]
        self.assertAlmostEqual(row["context_kb"], round(context["size"]["bytes"] / 1000, 1))
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(cli.main(["--root", str(helm_root), "ledger", "--days", "1"]), 0)
        self.assertIn("| ctx KB |", out.getvalue())

    def test_a_reviewer_is_pushed_the_standards_whatever_the_budget(self) -> None:
        helm_root, coordinator, project = self._rooted("reviewed")
        task = coordinator.create_task(project["id"], "do the thing")
        coordinator.launch_worker(task["id"], [sys.executable, "-c", ""])
        review = coordinator.create_task(
            project["id"], "review it", role="reviewer", reviews=task["id"], read_only=True
        )
        reviewer = coordinator.launch_worker(review["id"], [sys.executable, "-c", ""])
        context = json.loads(Path(reviewer["context_file"]).read_text(encoding="utf-8"))
        full_ids = {Path(s["source"]).parent.name for s in self._by_kind(context, "domain-knowledge")}
        for domain_id in REVIEW_DOMAINS:
            if domain_id in context["domain_chain"]:
                self.assertIn(domain_id, full_ids, f"a reviewer is pushed {domain_id} in full")
                self.assertNotIn(domain_id, context["domain"]["indexed"])
        # And the biggest non-standard base is still only indexed for it.
        self.assertIn("model-selection", context["domain"]["indexed"])

    def test_helm_guide_prints_a_domain_in_full_and_refuses_an_unknown_one(self) -> None:
        helm_root, coordinator, project = self._rooted("guided")
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(cli.main(["--root", str(helm_root), "guide", "code-review"]), 0)
        text = out.getvalue()
        self.assertIn("# Code review domain", text)
        self.assertIn("--- guardrails ---", text)
        for bad in ("no-such-domain", "../secrets", "code-review/../../state"):
            out = io.StringIO()
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
                code = cli.main(["--root", str(helm_root), "guide", bad])
            self.assertNotEqual(code, 0, bad)
            self.assertIn("unknown domain", out.getvalue(), bad)
