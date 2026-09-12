"""A worker sees the authored knowledge whole and the newest learnings that fit."""

from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path

from helm import doctor
from helm.core import Coordinator
from helm.learned import bound_learned_knowledge, split_learned
from helm.state import StateStore
from helm.values import LEARNED_KNOWLEDGE_BUDGET_BYTES
from tests.support import SHIPPED_DOMAINS, HelmTestCase


def _block(number: int, size: int) -> str:
    body = f"learning number {number} " * (size // 20)
    return (
        f"\n## Approved learning: lp-{number:012d}\n"
        f'<!-- helm-learning: {{"proposal_id":"lp-{number:012d}"}} -->\n'
        f"- Fact: {body}\n- Rationale: seen more than once.\n"
        f"<!-- /helm-learning: lp-{number:012d} -->\n"
    )


class BoundLearnedKnowledgeTests(HelmTestCase):
    def test_the_newest_learnings_fit_the_budget_and_the_rest_are_counted(self) -> None:
        authored = "# Domain\n\nThe authored body stays whole.\n"
        text = authored + "".join(_block(n, 1500) for n in range(1, 6))
        kept_authored, blocks = split_learned(text)
        self.assertEqual(kept_authored, authored)
        self.assertEqual(len(blocks), 5)

        bounded, omitted = bound_learned_knowledge(text, "domains/x/knowledge.md", budget=4000)
        self.assertEqual(omitted, 3)
        self.assertTrue(bounded.startswith(authored))
        self.assertIn("learning number 4 ", bounded)
        self.assertIn("learning number 5 ", bounded)
        self.assertNotIn("learning number 1 ", bounded)
        self.assertIn("3 earlier approved learning(s) left out", bounded)
        self.assertIn("domains/x/knowledge.md", bounded)
        # The kept learnings keep their file order.
        self.assertLess(bounded.index("learning number 4 "), bounded.index("learning number 5 "))

        # Nothing learned: nothing changes, nothing is said.
        self.assertEqual(bound_learned_knowledge(authored, "x"), (authored, 0))
        # The newest learning is kept even when it alone is over budget.
        single = authored + _block(9, 5000)
        bounded, omitted = bound_learned_knowledge(single, "x", budget=100)
        self.assertEqual(omitted, 0)
        self.assertIn("learning number 9 ", bounded)

    def _rooted(self, name: str) -> tuple[Path, Coordinator, dict]:
        helm_root = self._helm_root(f"{name}-root")
        shutil.rmtree(helm_root / "domains")
        shutil.copytree(SHIPPED_DOMAINS, helm_root / "domains")
        shutil.move(str(self.repo(name)), str(helm_root / "projects" / name))
        coordinator = Coordinator(StateStore(helm_root / "state", helm_root=helm_root))
        coordinator.discover_project(helm_root, name)
        coordinator.set_project_domains(name, ["software-delivery"])
        return helm_root, coordinator, coordinator.get_project(name)

    def test_a_composed_context_is_bounded_and_says_what_it_left_out(self) -> None:
        helm_root, coordinator, project = self._rooted("bounded")
        knowledge = helm_root / "domains" / "software-delivery" / "knowledge.md"
        authored, existing = split_learned(knowledge.read_text(encoding="utf-8"))
        knowledge.write_text(authored + "".join(_block(n, 2500) for n in range(1, 9)), encoding="utf-8")

        task = coordinator.create_task(project["id"], "do the thing")
        worker = coordinator.launch_worker(task["id"], [sys.executable, "-c", ""])
        context = json.loads(Path(worker["context_file"]).read_text(encoding="utf-8"))
        domain = context["domain"]
        self.assertGreater(domain["omitted_learnings"], 0)
        self.assertIn("learning number 8 ", domain["knowledge"])
        self.assertNotIn("learning number 1 ", domain["knowledge"])
        self.assertIn("left out of this context for size", domain["knowledge"])
        section = next(
            s for s in context["context_sections"]
            if s["kind"] == "domain-knowledge" and s["source"].endswith("software-delivery/knowledge.md")
        )
        self.assertEqual(section["omitted_learnings"], domain["omitted_learnings"])
        self.assertLessEqual(
            len(domain["knowledge"]) - len(authored), LEARNED_KNOWLEDGE_BUDGET_BYTES + 2500 + 300,
        )

        # Doctor names the domain and the fix.
        report = doctor.run(
            Coordinator(StateStore(helm_root / "state", helm_root=helm_root, read_only=True)), helm_root
        )
        finding = next(f for f in report.findings if f.id == "root.domains")
        self.assertEqual(finding.severity, doctor.WARNING)
        self.assertIn("software-delivery (8 learnings", finding.message)
        self.assertIn("fold the older approved learnings", finding.remediation)
