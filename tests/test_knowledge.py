"""Helm learns from the commander's own rulings and from what recurs in the work."""

from __future__ import annotations

import contextlib
import datetime as _dt
import io
import shutil
import sys
from pathlib import Path

from helm import cli
from helm.core import Coordinator
from helm.errors import HelmError
from helm.state import StateStore
from tests.support import SHIPPED_DOMAINS, HelmTestCase


class KnowledgeTests(HelmTestCase):
    def _rooted(self, name: str) -> tuple[Path, Coordinator, dict]:
        helm_root = self._helm_root(f"{name}-root")
        shutil.rmtree(helm_root / "domains")
        shutil.copytree(SHIPPED_DOMAINS, helm_root / "domains")
        shutil.move(str(self.repo(name)), str(helm_root / "projects" / name))
        coordinator = Coordinator(StateStore(helm_root / "state", helm_root=helm_root))
        project = coordinator.discover_project(helm_root, name)
        coordinator.set_project_domains(name, ["software-delivery"])
        return helm_root, coordinator, coordinator.get_project(name)

    def test_the_commander_teaches_a_rule_and_it_is_applied_at_once(self) -> None:
        helm_root, coordinator, project = self._rooted("taught")
        taught = coordinator.teach(
            "Run the full suite before reporting a result.", domain="software-delivery", note="my rule"
        )
        self.assertEqual(taught["status"], "applied")
        self.assertEqual(taught["origin"], "commander")
        knowledge = (helm_root / "domains" / "software-delivery" / "knowledge.md").read_text()
        self.assertIn("Run the full suite before reporting a result.", knowledge)
        self.assertIn(taught["id"], knowledge)
        local = coordinator.teach("This project's tests need the dev database up.", project_id=project["id"])
        self.assertIn("dev database", (helm_root / "projects" / project["id"] / ".helm" / "knowledge.md").read_text())
        with self.assertRaisesRegex(HelmError, "already proposed or applied"):
            coordinator.teach("Run the full suite before reporting a result.", domain="software-delivery")
        with self.assertRaisesRegex(HelmError, "say where it applies"):
            coordinator.teach("a rule without a home")
        self.assertEqual(coordinator.knowledge_stats()["by_origin"].get("commander"), 2)

    def _reviewed_task(self, coordinator, project, name: str, finding: str) -> dict:
        task = coordinator.create_task(project["id"], f"change {name}")
        coordinator.launch_worker(task["id"], [sys.executable, "-c", ""])
        review = coordinator.create_task(project["id"], "review", role="reviewer", reviews=task["id"], read_only=True)
        reviewer = coordinator.launch_worker(review["id"], [sys.executable, "-c", ""])
        coordinator.record_worker_message(reviewer["id"], "result", finding)
        return task

    def test_recurring_findings_and_answers_become_proposals_with_their_evidence(self) -> None:
        helm_root, coordinator, project = self._rooted("mined")
        one = self._reviewed_task(coordinator, project, "one", "CHANGES-REQUESTED: missing test for the new endpoint. Add one.")
        two = self._reviewed_task(coordinator, project, "two", "CHANGES-REQUESTED: missing test for the new endpoint again.")
        self._reviewed_task(coordinator, project, "three", "CHANGES-REQUESTED: the migration lacks a down step.")
        # Answers the coordinator gave twice are the same kind of signal.
        from helm.values import new_id, now
        with coordinator.store.locked() as data:
            for task in (one, two):
                worker = next(w for w in data["workers"].values() if w["task_id"] == task["id"])
                data["messages"].append({
                    "id": new_id("m"), "project_id": project["id"], "task_id": task["id"],
                    "worker_id": worker["id"], "kind": "answer", "status": None,
                    "text": "Hotfixes branch off main, never off a release branch.", "payload": {},
                    "created_at": now(),
                })
        preview = coordinator.mine_learnings(days=1, dry_run=True)
        self.assertEqual(preview["clusters"], 2)
        self.assertEqual([p for p in coordinator.waiting_learnings() if str(p.get("origin", "")).startswith("mined")], [])

        mined = coordinator.mine_learnings(days=1)

        facts = {p["proposed_fact"]: p for p in mined["proposed"]}
        self.assertEqual(len(facts), 2, facts)
        finding = next(p for f, p in facts.items() if "missing test" in f)
        self.assertEqual(finding["origin"], "mined: review finding")
        self.assertEqual(finding["domain_id"], "software-delivery")
        self.assertEqual(len(finding["source_message_ids"]), 2)
        self.assertNotIn("CHANGES-REQUESTED", finding["proposed_fact"])
        answer = next(p for f, p in facts.items() if "Hotfixes" in f)
        self.assertEqual(answer["origin"], "mined: answer")
        # Mining again proposes nothing new.
        self.assertEqual(coordinator.mine_learnings(days=1)["proposed"], [])

        # Triage decides several at once; approved ones are applied.
        decided = coordinator.triage_learnings(approve=[finding["id"]], reject=[answer["id"]], note="triaged")
        self.assertEqual([p["id"] for p in decided["applied"]], [finding["id"]])
        self.assertEqual([p["id"] for p in decided["rejected"]], [answer["id"]])
        knowledge = (helm_root / "domains" / "software-delivery" / "knowledge.md").read_text()
        self.assertIn("missing test for the new endpoint", knowledge)
        # And a later finding that restates it is counted as knowledge not followed.
        self.assertEqual(
            coordinator.learning_not_followed("CHANGES-REQUESTED: still missing a test for the new endpoint", "software-delivery"),
            [finding["id"]],
        )
        self.assertEqual(coordinator.learning_not_followed("CHANGES-REQUESTED: typo in the changelog", "software-delivery"), [])

    def test_waiting_proposals_reach_pending_after_a_week_and_the_cli_triages(self) -> None:
        helm_root, coordinator, project = self._rooted("waiting")
        self._reviewed_task(coordinator, project, "a", "CHANGES-REQUESTED: no error handling on the upload path.")
        self._reviewed_task(coordinator, project, "b", "CHANGES-REQUESTED: no error handling on the upload path either.")
        coordinator.mine_learnings(days=1)
        waiting = [p for p in coordinator.waiting_learnings() if str(p.get("origin", "")).startswith("mined")]
        self.assertEqual(len(waiting), 1)
        with coordinator.store.locked() as data:
            for proposal in data["learning_proposals"]:
                proposal["created_at"] = (
                    _dt.datetime.now(_dt.timezone.utc) - _dt.timedelta(days=8)
                ).strftime("%Y-%m-%dT%H:%M:%SZ")
        self.assertGreaterEqual(len(coordinator.stale_learnings()), 1)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            cli.main(["--root", str(helm_root), "pending"])
            cli.main(["--root", str(helm_root), "learning", "triage"])
            cli.main(["--root", str(helm_root), "learning", "stats"])
            cli.main(["--root", str(helm_root), "learning", "triage", "--approve", waiting[0]["id"], "--note", "yes"])
        text = out.getvalue()
        self.assertRegex(text, r"\d+ learning proposal\(s\) waiting more than 7 days")
        self.assertIn("mined: review finding", text)
        self.assertIn("waiting more than 7 days", text)
        self.assertIn(f"applied  {waiting[0]['id']}", text)
        self.assertEqual([p for p in coordinator.waiting_learnings() if str(p.get("origin", "")).startswith("mined")], [])

    def test_auto_proposals_skip_reviewers_and_reports_and_a_proposal_outlives_its_project(self) -> None:
        helm_root, coordinator, project = self._rooted("filtered")
        # A worker whose result reads as a fact is proposed; a report is not.
        task = coordinator.create_task(project["id"], "fix the thing")
        worker = coordinator.launch_worker(task["id"], [sys.executable, "-c", ""])
        proposals = coordinator.generate_learning_proposals(
            task["id"], fact="A retry loop must cap its attempts and say so in the log."
        )
        self.assertEqual(len(proposals), 1)
        report_task = coordinator.create_task(project["id"], "ship it")
        report_worker = coordinator.launch_worker(report_task["id"], [sys.executable, "-c", ""])
        with coordinator.store.locked() as data:
            data["messages"].append({
                "id": "m-report", "project_id": project["id"], "task_id": report_task["id"],
                "worker_id": report_worker["id"], "kind": "result", "status": None,
                "text": "TCK-1 delivered: PR #12 opened (https://example.test/pull/12), tests exit 0.",
                "payload": {}, "created_at": "2026-09-12T00:00:00Z",
            })
        with self.assertRaisesRegex(HelmError, "no concise result"):
            coordinator.generate_learning_proposals(report_task["id"])
        review = coordinator.create_task(project["id"], "review", role="reviewer", reviews=task["id"], read_only=True)
        reviewer = coordinator.launch_worker(review["id"], [sys.executable, "-c", ""])
        coordinator.record_worker_message(reviewer["id"], "result", "APPROVED — nothing to change.")
        with self.assertRaisesRegex(HelmError, "its verdict is not a learning"):
            coordinator.generate_learning_proposals(review["id"])
        # The project is forgotten; its learning can still be applied to the domain.
        coordinator.cleanup_task(task["id"], delete_branch=True)
        coordinator.cleanup_task(report_task["id"], delete_branch=True)
        coordinator.cleanup_task(review["id"])
        shutil.rmtree(helm_root / "projects" / project["id"])
        coordinator.remove_project(project["id"])
        decided = coordinator.triage_learnings(approve=[proposals[0]["id"]], note="still true")
        self.assertEqual([p["id"] for p in decided["applied"]], [proposals[0]["id"]], decided)
        self.assertIn("retry loop", (helm_root / "domains" / "software-delivery" / "knowledge.md").read_text())

    def test_an_applied_learning_carries_no_identifier_from_the_root_that_learned_it(self) -> None:
        """A domain file is tracked product content; the evidence stays in state."""
        helm_root, coordinator, _ = self._rooted("scrubbed")
        taught = coordinator.teach(
            "On t-0123456789ab the reviewer m-fedcba987654 caught what ABC-12 missed: check the seam.",
            domain="software-delivery",
            note="seen on w-abcdef012345 during ABC-12",
        )
        knowledge = (helm_root / "domains" / "software-delivery" / "knowledge.md").read_text()
        for identifier in ("t-0123456789ab", "m-fedcba987654", "w-abcdef012345", "ABC-12", "source_message_ids", "source_task_id"):
            self.assertNotIn(identifier, knowledge, identifier)
        self.assertIn("On a task the reviewer a message caught what a ticket missed", knowledge)
        self.assertIn(taught["id"], knowledge, "the proposal id is the one tie back to provenance")
        # The record in state keeps the words the commander actually used.
        stored = next(p for p in coordinator.store.load()["learning_proposals"] if p["id"] == taught["id"])
        self.assertIn("t-0123456789ab", stored["proposed_fact"])
        self.assertIn("ABC-12", stored["rationale"])
