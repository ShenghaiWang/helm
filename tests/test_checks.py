"""A project's declared checks are the worker's to run and Helm's to judge."""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path

from helm.core import Coordinator
from helm.errors import HelmError, SafetyError
from helm.state import StateStore
from tests.support import HelmTestCase


class DeclaredChecksTests(HelmTestCase):
    def _rooted(self, name: str, checks) -> tuple[Path, Coordinator, dict]:
        helm_root = self._helm_root(f"{name}-root")
        shutil.move(str(self.repo(name)), str(helm_root / "projects" / name))
        (helm_root / "projects" / name / ".helm").mkdir()
        (helm_root / "projects" / name / ".helm" / "project.json").write_text(
            json.dumps({"checks": checks}), encoding="utf-8"
        )
        coordinator = Coordinator(StateStore(helm_root / "state", helm_root=helm_root))
        coordinator.discover_project(helm_root, name)
        return helm_root, coordinator, coordinator.get_project(name)

    def _worked(self, coordinator: Coordinator, project: dict, brief: str, **kwargs) -> tuple[dict, str]:
        task = coordinator.create_task(project["id"], brief, **kwargs)
        code = (
            "from pathlib import Path; import subprocess; "
            "Path('change.txt').write_text('worker'); "
            "subprocess.run(['git','add','change.txt'],check=True); "
            "subprocess.run(['git','commit','-qm','worker change'],check=True)"
        )
        worker = coordinator.launch_worker(task["id"], [sys.executable, "-c", code])
        tip = subprocess.run(
            ["git", "-C", task["workspace"], "rev-parse", "HEAD"], text=True, capture_output=True, check=True
        ).stdout.strip()
        return task, tip[:10], worker

    def test_the_declaration_is_read_handed_to_the_worker_and_gates_approval(self) -> None:
        helm_root, coordinator, project = self._rooted("checked", [
            {"name": "unit", "command": "make test", "cases": True},
            "make lint",
        ])
        self.assertEqual(
            project["checks"],
            [
                {"name": "unit", "command": "make test", "cases": True},
                {"name": "make", "command": "make lint", "cases": False},
            ],
        )
        task, tip, worker = self._worked(coordinator, project, "a feature")
        context = json.loads(Path(worker["context_file"]).read_text(encoding="utf-8"))
        handed = context["task"]["checks"]
        self.assertEqual([c["name"] for c in handed], ["unit", "make"])
        self.assertIn(f"helm task evidence {task['id']} --tip <sha> --check unit", handed[0]["record_with"])
        self.assertIn("--cases <n>", handed[0]["record_with"])
        self.assertTrue(handed[0]["gates_approval"])

        with self.assertRaisesRegex(SafetyError, "no green record.*unit.*make"):
            coordinator.approve_task(task["id"], "ok")
        # A green run of the suite that says nothing about what ran is not the unit check.
        coordinator.record_task_evidence(task["id"], tip=tip, command="make test", exit_code=0, check="unit")
        coordinator.record_task_evidence(task["id"], tip=tip, command="make lint", exit_code=0)
        with self.assertRaisesRegex(SafetyError, "unit \\(`make test`, with --cases\\)"):
            coordinator.approve_task(task["id"], "ok")
        coordinator.record_task_evidence(task["id"], tip=tip, command="make test", exit_code=0, check="unit", cases=12)
        self.assertEqual(coordinator.approve_task(task["id"], "ok")["status"], "approved")

    def test_a_small_task_runs_the_checks_but_is_not_gated_on_them(self) -> None:
        helm_root, coordinator, project = self._rooted("smallcheck", ["make test"])
        task, tip, worker = self._worked(coordinator, project, "rename a label", shape="small")
        context = json.loads(Path(worker["context_file"]).read_text(encoding="utf-8"))
        self.assertFalse(context["task"]["checks"][0]["gates_approval"])
        self.assertEqual(coordinator.approve_task(task["id"], "ok")["status"], "approved")

    def test_a_bad_declaration_is_refused_at_discovery(self) -> None:
        for index, (bad, message) in enumerate((
            ("make test", "must be a JSON list"),
            ([{"name": "unit"}], "needs a one-line command"),
            ([{"name": "bad name!", "command": "x"}], "needs a short name"),
            ([{"name": "unit", "command": "x"}, {"name": "unit", "command": "y"}], "twice"),
            ([{"name": "unit", "command": "x", "cases": "yes"}], "cases must be true or false"),
        )):
            name = f"badcheck{index}"
            helm_root = self._helm_root(f"{name}-root")
            shutil.move(str(self.repo(name)), str(helm_root / "projects" / name))
            (helm_root / "projects" / name / ".helm").mkdir()
            (helm_root / "projects" / name / ".helm" / "project.json").write_text(
                json.dumps({"checks": bad}), encoding="utf-8"
            )
            coordinator = Coordinator(StateStore(helm_root / "state", helm_root=helm_root))
            with self.assertRaisesRegex(HelmError, message, msg=str(bad)):
                coordinator.discover_project(helm_root, name)


class EvidenceIsAboutTheTipUnderReviewTests(HelmTestCase):
    """Fix, verify, tidy, commit is the natural order of a round.

    It also silently produces a green suite result that describes the commit
    before the one under review. Three reviews were spent on exactly that in
    one morning, across two branches, each correctly refusing evidence that
    was true about a revision nobody was reading.
    """

    def _task_with_a_worker(self) -> dict:
        root = self.repo("tipcheck")
        project = self.coordinator.register_project(
            "Tip Check", str(root), project_id="tipcheck"
        )
        task = self.coordinator.create_task(project["id"], "a change")
        self.coordinator.launch_worker(task["id"], [sys.executable, "-c", ""])
        return task

    def _head(self, task_id: str) -> str:
        data = self.coordinator.store.load()
        return self.coordinator._evidence_head(data["tasks"][task_id])

    def test_evidence_at_the_current_tip_is_recorded(self) -> None:
        task = self._task_with_a_worker()
        report = self.coordinator.record_task_evidence(
            task["id"], tip=self._head(task["id"]), command="make test", exit_code=0, cases=7
        )
        self.assertEqual(report["cases"], 7)

    def test_evidence_naming_an_older_revision_is_refused(self) -> None:
        task = self._task_with_a_worker()
        head = self._head(task["id"])
        workspace = Path(self.coordinator.store.load()["tasks"][task["id"]]["workspace"])
        (workspace / "tidy.txt").write_text("a doc-only commit after the suite ran\n")
        subprocess.run(["git", "add", "tidy.txt"], cwd=workspace, check=True)
        subprocess.run(["git", "commit", "-m", "tidy"], cwd=workspace, check=True)

        with self.assertRaises(HelmError) as caught:
            self.coordinator.record_task_evidence(
                task["id"], tip=head, command="make test", exit_code=0, cases=7
            )
        message = str(caught.exception)
        self.assertIn(head[:12], message)
        self.assertIn("ran before the tip under review", message)
        # The refusal has to name what to do, or it is just an obstacle.
        self.assertIn("git rev-parse HEAD", message)

    def test_an_abbreviation_too_short_to_name_one_commit_is_refused(self) -> None:
        """A one-character tip matches any head that starts with it."""
        task = self._task_with_a_worker()
        head = self._head(task["id"])
        with self.assertRaises(HelmError):
            self.coordinator.record_task_evidence(
                task["id"], tip=head[:1], command="make test", exit_code=0, cases=7
            )
        report = self.coordinator.record_task_evidence(
            task["id"], tip=head[:7], command="make test", exit_code=0, cases=7
        )
        self.assertEqual(report["tip"], head[:7])

    def test_an_unreadable_worktree_does_not_block_recording(self) -> None:
        """Unverifiable is weaker evidence, never a refusal."""
        task = self._task_with_a_worker()
        with self.coordinator.store.locked() as data:
            data["tasks"][task["id"]]["workspace"] = "/nonexistent/worktree"

        report = self.coordinator.record_task_evidence(
            task["id"], tip="abc1234", command="make test", exit_code=0, cases=7
        )
        self.assertEqual(report["tip"], "abc1234")


class TheReviewerIsToldWhetherEvidenceIsFreshTests(HelmTestCase):
    """Comparing two revisions Helm already holds is not the reviewer's work.

    The brief used to quote the tip the AUTHOR claimed and stop there, so
    every reviewer had to establish by hand whether that was the revision in
    front of it. Three did, correctly, in one morning across two branches.
    """

    def _evidence_brief(self, claimed_tip: str, *, real_tip: str = "") -> str:
        from helm.herdr import HerdrAdapter

        data = {
            "messages": [
                {
                    "task_id": "t-x",
                    "kind": "status",
                    "created_at": "2026-01-01T00:00:00Z",
                    "payload": {
                        "full_suite": {
                            "tip": claimed_tip,
                            "command": "make test",
                            "exit": 0,
                            "cases": 12,
                        }
                    },
                }
            ]
        }
        return HerdrAdapter._full_suite_evidence(data, "t-x", real_tip)

    def test_evidence_at_the_reviewed_tip_is_called_fresh(self) -> None:
        brief = self._evidence_brief("abcdef1234567890", real_tip="abcdef1234567890")
        self.assertIn("HELM CHECKED", brief)
        self.assertIn("FRESH by revision", brief)

    def test_evidence_at_another_tip_is_called_stale_with_both_revisions(self) -> None:
        brief = self._evidence_brief("1111111111111111", real_tip="2222222222222222")
        self.assertIn("THE EVIDENCE IS STALE", brief)
        self.assertIn("1111111111111111", brief)
        self.assertIn("2222222222222222", brief)
        # The reviewer must hand it back, not resolve it by rerunning.
        self.assertIn("do NOT run the suite", brief)

    def test_an_unknown_tip_makes_no_claim_either_way(self) -> None:
        """A repository Helm cannot read is never a stale-evidence finding."""
        brief = self._evidence_brief("1111111111111111", real_tip="")
        self.assertNotIn("HELM CHECKED", brief)

    def test_evidence_stating_no_tip_is_called_unverifiable(self) -> None:
        brief = self._evidence_brief("", real_tip="2222222222222222")
        self.assertIn("STATES NO TIP", brief)
        self.assertIn("unverifiable", brief)


class ProseAfterARecordDoesNotEraseItTests(HelmTestCase):
    """A hand-rolled payload must not bury a correctly-filed one.

    `full_suite` holds whatever the author put there: the structured record
    `helm task evidence` writes, or a bare string of prose. Reading the last
    entry unconditionally let one prose message land after a correct record
    and erase its tip -- so the reviewer was told the evidence was
    unverifiable while an exact record sat one message above it. A false
    stale-evidence finding is worse than the staleness it looks for.
    """

    def _brief(self, reports: list, review_tip: str) -> str:
        from helm.herdr import HerdrAdapter

        data = {
            "messages": [
                {
                    "task_id": "t-x",
                    "kind": "status",
                    "created_at": f"2026-01-01T00:00:0{index}Z",
                    "payload": {"full_suite": report},
                }
                for index, report in enumerate(reports)
            ]
        }
        return HerdrAdapter._full_suite_evidence(data, "t-x", review_tip)

    def test_a_prose_payload_after_a_record_keeps_the_records_tip(self) -> None:
        record = {"tip": "abcdef1234567890", "command": "make test", "exit": 0, "cases": 181}
        brief = self._brief([record, "re-ran the nine suites, exit 0"], "abcdef1234567890")
        self.assertIn("FRESH by revision", brief)
        self.assertNotIn("STATES NO TIP", brief)

    def test_a_genuinely_stale_record_is_still_called_stale(self) -> None:
        record = {"tip": "1111111111111111", "command": "make test", "exit": 0}
        brief = self._brief([record, "some prose"], "2222222222222222")
        self.assertIn("THE EVIDENCE IS STALE", brief)

    def test_prose_alone_still_states_no_tip(self) -> None:
        brief = self._brief(["only prose, never filed properly"], "2222222222222222")
        self.assertIn("STATES NO TIP", brief)
