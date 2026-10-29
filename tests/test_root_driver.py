"""The root as a driver: work it launched itself gets no lead appointed beside it."""

from __future__ import annotations

import sys
from pathlib import Path
from unittest import mock

from helm import cli, watchdog
from tests.support import HelmTestCase


class RootDriverTests(HelmTestCase):
    def _project(self, name: str):
        root = self.repo(name)
        return self.coordinator.register_project(name.title(), str(root), project_id=name)

    def test_a_task_the_root_created_is_driven_by_the_root(self) -> None:
        """No evidence of a lead used to mean "the project's lead, or nobody"."""
        project = self._project("rooted")
        task = self.coordinator.create_task(project["id"], "work the root drives")
        self.assertEqual(task["driven_by"], "root")
        self.assertEqual(self.coordinator.driver_resolution(task["id"]), (None, "root"))
        # A lead task is nobody's charge but its own.
        lead = self.coordinator.create_foreman_task(project["id"])
        self.assertIsNone(lead["driven_by"])

    def test_the_watchdog_does_not_adopt_root_driven_work(self) -> None:
        """Five leads were appointed for one task in a day, each stopped by the root."""
        project = self._project("adopted")
        task = self.coordinator.create_task(project["id"], "root-driven work")
        self.coordinator.prepare_external_worker(task["id"], [sys.executable, "-c", ""])
        appointed: list[str] = []

        def appoint(coordinator, project_id, **kwargs):
            appointed.append(project_id)
            return {"worker": {"id": "w-unwanted"}}

        memory = Path(self.temp.name) / "adopted.json"
        with mock.patch.object(type(self.coordinator), "worker_health", return_value=[]), \
             mock.patch.object(cli, "_start_foreman", side_effect=appoint), \
             mock.patch("helm.core.Coordinator", return_value=self.coordinator), \
             mock.patch.dict("os.environ", {"HELM_STATE_DIR": str(self.state.directory)}):
            reports = watchdog.heal_pass(None, memory)
        self.assertEqual(appointed, [])
        self.assertFalse(any("appointed" in line for line in reports), reports)

    def test_launching_root_driven_work_appoints_no_lead(self) -> None:
        project = self._project("launched")
        task = self.coordinator.create_task(project["id"], "root-driven work")
        started: list[str] = []
        with mock.patch.object(
            cli, "_start_foreman", side_effect=lambda c, p, **k: started.append(p) or {"worker": {"id": "w-x"}}
        ):
            self.assertIsNone(cli._ensure_foreman(self.coordinator, project["id"], task_id=task["id"]))
            # Without a task to judge by, the project-level rule still holds.
            cli._ensure_foreman(self.coordinator, project["id"])
        self.assertEqual(started, [project["id"]])

    def test_stopping_a_lead_hands_its_work_to_the_root(self) -> None:
        """Standing the lead down IS taking over; the record has to say so."""
        project = self._project("handed")
        lead_task = self.coordinator.create_foreman_task(project["id"])
        lead = self.coordinator.prepare_external_worker(lead_task["id"], [sys.executable, "-c", ""])
        with mock.patch.dict("os.environ", {"HELM_WORKER_ID": lead["id"]}):
            for kind, text in (
                ("requirement", "goal: a unit; Done means: X; Out of scope: Y"),
                ("solution", "approach: one; verification: tests"),
            ):
                self.coordinator.propose_gate(lead_task["id"], kind, text)
                with mock.patch.dict("os.environ", {"HELM_WORKER_ID": ""}):
                    self.coordinator.decide_gate(lead_task["id"], kind, confirm=True, skip=False)
            task = self.coordinator.create_task(project["id"], "the lead's work")
        self.assertEqual(self.coordinator.driver_resolution(task["id"])[1], "creator")
        # An open PR still needs a driver; a merged one does not.
        with mock.patch.dict("os.environ", {"HELM_WORKER_ID": lead["id"]}):
            self.coordinator.propose_gate(lead_task["id"], "requirement", "goal: b; Done means: X; Out of scope: Y")
            with mock.patch.dict("os.environ", {"HELM_WORKER_ID": ""}):
                self.coordinator.decide_gate(lead_task["id"], "requirement", confirm=True, skip=False)
            self.coordinator.propose_gate(lead_task["id"], "solution", "approach: two; verification: tests")
            with mock.patch.dict("os.environ", {"HELM_WORKER_ID": ""}):
                self.coordinator.decide_gate(lead_task["id"], "solution", confirm=True, skip=False)
            shipped = self.coordinator.create_task(project["id"], "the lead's shipped work")
        with self.coordinator.store.locked() as data:
            data["tasks"][shipped["id"]]["status"] = "merged"
            # Work another live lead adopted stays that lead's.
            data["tasks"][task["id"]]["adopted_by"] = "w-other-live-lead"
        self.coordinator.stop_worker(lead["id"], reason="the root takes over")
        self.assertEqual(self.coordinator.hand_lead_work_to_root(lead["id"]), [])
        with self.coordinator.store.locked() as data:
            data["tasks"][task["id"]]["adopted_by"] = None
            data["tasks"][task["id"]]["status"] = "pr-open"

        handed = self.coordinator.hand_lead_work_to_root(lead["id"])
        self.assertEqual(handed, [task["id"]])
        self.assertEqual(self.coordinator.driver_resolution(task["id"]), (None, "root"))
        self.assertIsNone(self.coordinator.store.load()["tasks"][shipped["id"]].get("driven_by"))
        # A lead appointed for the ticket takes it back: adoption outranks the root.
        with self.coordinator.store.locked() as data:
            data["tasks"][task["id"]]["ticket"] = "TICKET-7"
        new_lead_task = self.coordinator.create_foreman_task(project["id"], ticket="TICKET-7")
        new_lead = self.coordinator.prepare_external_worker(new_lead_task["id"], [sys.executable, "-c", ""])
        self.assertEqual(self.coordinator.hand_root_work_to_lead(new_lead["id"], ticket="TICKET-7"), [task["id"]])
        self.assertEqual(self.coordinator.driver_resolution(task["id"])[1], "adopted")

    def test_a_reviewer_the_root_started_still_reports_to_the_reviewed_tasks_lead(self) -> None:
        project = self._project("reviewed")
        lead_task = self.coordinator.create_foreman_task(project["id"])
        lead = self.coordinator.prepare_external_worker(lead_task["id"], [sys.executable, "-c", ""])
        with mock.patch.dict("os.environ", {"HELM_WORKER_ID": lead["id"]}):
            for kind, text in (
                ("requirement", "goal: a unit; Done means: X; Out of scope: Y"),
                ("solution", "approach: one; verification: tests"),
            ):
                self.coordinator.propose_gate(lead_task["id"], kind, text)
                with mock.patch.dict("os.environ", {"HELM_WORKER_ID": ""}):
                    self.coordinator.decide_gate(lead_task["id"], kind, confirm=True, skip=False)
            task = self.coordinator.create_task(project["id"], "the lead's work")
        review = self.coordinator.create_task(
            project["id"], "review it", role="reviewer", reviews=task["id"]
        )
        self.assertIsNone(review["driven_by"])
        driver, how = self.coordinator.driver_resolution(review["id"])
        self.assertEqual((driver["id"], how), (lead["id"], "reviewed"))

    def test_root_ownership_survives_reviewer_and_creator_inheritance(self) -> None:
        """A reviewer of the root's work, or a task its worker created, is the
        root's too; read as "reviewed" or "creator" with no driver it would be
        orphaned to the watchdog."""
        project = self._project("inherit")
        task = self.coordinator.create_task(project["id"], "root-driven work")
        review = self.coordinator.create_task(
            project["id"], "review it", role="reviewer", reviews=task["id"]
        )
        self.assertEqual(self.coordinator.driver_resolution(review["id"]), (None, "root"))
        coder = self.coordinator.prepare_external_worker(task["id"], [sys.executable, "-c", ""])
        with mock.patch.dict("os.environ", {"HELM_WORKER_ID": coder["id"]}):
            child = self.coordinator.create_task(project["id"], "a look the worker asked for", read_only=True)
        self.assertEqual(self.coordinator.driver_resolution(child["id"]), (None, "root"))
        self.assertFalse(watchdog._driverless(self.coordinator, review["id"], self.coordinator.store.load()))

    def test_a_lead_named_for_a_ticket_takes_the_roots_work_on_it(self) -> None:
        """`helm lead --ticket` and `helm route --ticket` are the hand-off."""
        import argparse

        project = self._project("handoff")
        task = self.coordinator.create_task(project["id"], "root-driven work", ticket="TICKET-9")
        other = self.coordinator.create_task(project["id"], "unrelated root work")
        args = argparse.Namespace(
            project_id=project["id"], ticket="TICKET-9", brief=None, agent=None, model=None,
            effort=None, herdr=False, worker_command_text=[sys.executable, "-c", ""],
            new_lead=True,
        )
        with mock.patch.object(cli.Coordinator, "launch_worker", side_effect=lambda tid, cmd, wait=False: self.coordinator.prepare_external_worker(tid, cmd)):
            cli._cmd_foreman(cli._Context(self.coordinator, self.coordinator.store, None), args)
        lead = self.coordinator.driver_named(project["id"], "TICKET-9")
        self.assertIsNotNone(lead)
        self.assertEqual(self.coordinator.driver_resolution(task["id"])[1], "adopted")
        self.assertEqual(self.coordinator.driver_resolution(other["id"]), (None, "root"))

    def test_stopping_a_lead_hands_over_held_and_blocked_work_but_not_another_leads(self) -> None:
        project = self._project("states")
        lead_task = self.coordinator.create_foreman_task(project["id"])
        lead = self.coordinator.prepare_external_worker(lead_task["id"], [sys.executable, "-c", ""])
        rival_task = self.coordinator.create_foreman_task(project["id"])
        rival = self.coordinator.prepare_external_worker(rival_task["id"], [sys.executable, "-c", ""])
        with mock.patch.dict("os.environ", {"HELM_WORKER_ID": lead["id"]}):
            for kind, text in (
                ("requirement", "goal: a unit; Done means: X; Out of scope: Y"),
                ("solution", "approach: one; verification: tests"),
            ):
                self.coordinator.propose_gate(lead_task["id"], kind, text)
                with mock.patch.dict("os.environ", {"HELM_WORKER_ID": ""}):
                    self.coordinator.decide_gate(lead_task["id"], kind, confirm=True, skip=False)
            held = self.coordinator.create_task(project["id"], "held work")
            looks = [
                self.coordinator.create_task(project["id"], f"look {n}", read_only=True)
                for n in range(3)
            ]
        with self.coordinator.store.locked() as data:
            data["tasks"][held["id"]]["status"] = "approval-needed"
            for status, look in zip(("blocked", "approved", "pr-merged"), looks):
                data["tasks"][look["id"]]["status"] = status
            data["tasks"][looks[1]["id"]]["adopted_by"] = rival["id"]
        self.coordinator.stop_worker(lead["id"], reason="the root takes over")
        handed = set(self.coordinator.hand_lead_work_to_root(lead["id"]))
        self.assertEqual(handed, {held["id"], looks[0]["id"]})
        self.assertEqual(self.coordinator.driver_resolution(looks[1]["id"])[0]["id"], rival["id"])

    def test_helm_lead_for_a_ticket_with_its_lead_already_live_still_hands_off(self) -> None:
        import argparse

        project = self._project("relead")
        # Another ticket's lead is live first, so "the project's lead" is the wrong one.
        other_task = self.coordinator.create_foreman_task(project["id"], ticket="TICKET-4")
        self.coordinator.prepare_external_worker(other_task["id"], [sys.executable, "-c", ""])
        lead_task = self.coordinator.create_foreman_task(project["id"], ticket="TICKET-5")
        lead = self.coordinator.prepare_external_worker(lead_task["id"], [sys.executable, "-c", ""])
        mine = self.coordinator.create_task(project["id"], "the root's own", ticket="TICKET-5")
        args = argparse.Namespace(
            project_id=project["id"], ticket="TICKET-5", brief=None, agent=None, model=None,
            effort=None, herdr=False, worker_command_text=None, new_lead=False,
        )
        with mock.patch.object(type(self.coordinator), "worker_health", return_value=[]):
            self.assertEqual(cli._cmd_foreman(cli._Context(self.coordinator, self.coordinator.store, None), args), 0)
        driver, how = self.coordinator.driver_resolution(mine["id"])
        self.assertEqual((driver["id"], how), (lead["id"], "adopted"))

    def test_stopping_a_continued_lead_hands_over_what_its_earlier_session_adopted(self) -> None:
        """A continued lead keeps its task and changes its session id, so the
        adopter on the record is an earlier session of the same lead."""
        project = self._project("continued")
        lead_task = self.coordinator.create_foreman_task(project["id"])
        first = self.coordinator.prepare_external_worker(lead_task["id"], [sys.executable, "-c", ""])
        task = self.coordinator.create_task(project["id"], "root work the lead adopted")
        with self.coordinator.store.locked() as data:
            data["tasks"][task["id"]]["adopted_by"] = first["id"]
            data["workers"][first["id"]]["status"] = "completed"
            # The replacement session of the same lead task, already stood down.
            second = dict(data["workers"][first["id"]], id="w-second-session", status="stopped")
            data["workers"][second["id"]] = second
        self.assertEqual(self.coordinator.hand_lead_work_to_root("w-second-session"), [task["id"]])
        self.assertEqual(self.coordinator.driver_resolution(task["id"]), (None, "root"))

    def test_a_recovery_appointment_leaves_the_roots_work_alone(self) -> None:
        """Only an appointment the root asked for by name is a hand-off."""
        project = self._project("recovery")
        mine = self.coordinator.create_task(project["id"], "the root's own", ticket="TICKET-3")
        launch = lambda tid, cmd, wait=False: self.coordinator.prepare_external_worker(tid, cmd)
        with mock.patch.object(cli.Coordinator, "launch_worker", side_effect=launch):
            command = [sys.executable, "-c", ""]
            cli._start_foreman(
                self.coordinator, project["id"], herdr=False, command=command, ticket="TICKET-3"
            )
            self.assertEqual(self.coordinator.driver_resolution(mine["id"]), (None, "root"))
            cli._start_foreman(
                self.coordinator, project["id"], herdr=False, command=command,
                ticket="TICKET-3", hand_off=True,
            )
        self.assertEqual(self.coordinator.driver_resolution(mine["id"])[1], "adopted")

    def test_a_pr_event_on_root_driven_work_goes_to_the_commander_not_a_lead(self) -> None:
        project = self._project("watched")
        task = self.coordinator.create_task(project["id"], "root-driven work")
        event = {"task_id": task["id"], "actionable": True, "text": "CI failed"}
        with mock.patch.object(cli, "_start_foreman") as start:
            lead, how = cli._pr_event_lead(self.coordinator, event)
        self.assertIsNone(lead)
        self.assertEqual(how, "root")
        start.assert_not_called()
