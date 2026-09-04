"""Herdr workspace ownership, routing and space lifecycle."""

from __future__ import annotations

import contextlib
import io
import os
import json
import subprocess
import sys
from pathlib import Path
from unittest import mock

from helm import cli
from helm.core import (
    project_glyph,
    SafetyError,
    _git,
)
from helm.herdr import HerdrAdapter, HerdrNotFound, SubprocessHerdrClient

from tests.support import FakeHerdr, HelmTestCase, REPO_ROOT, SHIPPED_DOMAINS


class HerdrTests(HelmTestCase):
    def _finished_project(self, name: str) -> tuple[dict[str, Any], FakeHerdr, HerdrAdapter, dict[str, Any]]:
        root = self.repo(name)
        project = self.coordinator.register_project(name.title(), str(root), project_id=name)
        task = self.coordinator.create_task(project["id"], "one task")
        herdr = FakeHerdr()
        adapter = HerdrAdapter(self.coordinator, herdr)
        worker = adapter.launch_task(task["id"], [sys.executable, "-c", ""], wait=False)
        Path(worker["exit_file"]).write_text(json.dumps({"returncode": 0}) + "\n", encoding="utf-8")
        return project, herdr, adapter, worker

    def test_a_finished_worker_tab_closes_but_evidence_is_kept(self) -> None:
        root = self.repo("tabs")
        project = self.coordinator.register_project("Tabs", str(root), project_id="tabs")
        herdr = FakeHerdr()
        adapter = HerdrAdapter(self.coordinator, herdr)

        done = self.coordinator.create_task(project["id"], "finish cleanly")
        done_worker = adapter.launch_task(done["id"], [sys.executable, "-c", ""], wait=False)
        self.coordinator.record_worker_message(done_worker["id"], "result", "all good")
        self.coordinator.settle_reported_worker(done_worker["id"])

        broke = self.coordinator.create_task(project["id"], "hit a wall")
        broke_worker = adapter.launch_task(broke["id"], [sys.executable, "-c", ""], wait=False)
        self.coordinator.record_worker_message(broke_worker["id"], "blocker", "needs a credential")
        self.coordinator.settle_reported_worker(broke_worker["id"])

        released = adapter.release_finished_tabs()
        # The completed task's pane has nothing left to show; the blocked one
        # is the evidence needed to diagnose it and must survive.
        self.assertIn(done_worker["id"], released)
        self.assertNotIn(broke_worker["id"], released)
        state = self.coordinator.store.load()["integrations"]["herdr"]["workers"]
        self.assertNotIn(done_worker["id"], state)
        self.assertIn(broke_worker["id"], state)
        # Idempotent: nothing left to close on a second pass.
        self.assertEqual(adapter.release_finished_tabs(), [])

    def test_post_merge_release_closes_finished_worker_and_idle_foreman_tabs(self) -> None:
        root = self.repo("merged-tabs")
        project = self.coordinator.register_project("Merged", str(root), project_id="merged-tabs")
        herdr = FakeHerdr()
        adapter = HerdrAdapter(self.coordinator, herdr)

        task = self.coordinator.create_task(project["id"], "finish and merge")
        worker = adapter.launch_task(task["id"], [sys.executable, "-c", ""], wait=False)
        self.coordinator.record_worker_message(
            worker["id"], "result", "done", requested_status="completed"
        )
        foreman_task = self.coordinator.create_foreman_task(project["id"])
        foreman = adapter.launch_task(
            foreman_task["id"], [sys.executable, "-c", ""], wait=False
        )
        with self.coordinator.store.locked() as data:
            data["tasks"][task["id"]]["status"] = "merged"

        with contextlib.redirect_stdout(io.StringIO()), mock.patch.object(
            cli, "HerdrAdapter", return_value=adapter
        ):
            cli._release_finished_space(self.coordinator, {"project_id": project["id"]})

        self.assertIn(worker["id"], self.coordinator.store.load()["workers"])
        state = self.coordinator.store.load()["integrations"]["herdr"]
        self.assertNotIn(worker["id"], state["workers"])
        self.assertNotIn(foreman["id"], state["workers"])
        self.assertEqual(len(herdr.closed_tabs), 2)
        self.assertEqual(len(herdr.closed_workspaces), 1)
        self.assertTrue(Path(worker["exit_file"]).exists())
        self.assertTrue(Path(foreman["exit_file"]).exists())

    def test_a_space_is_not_held_for_evidence_that_no_longer_exists(self) -> None:
        root = self.repo("evidence")
        project = self.coordinator.register_project("Ev", str(root), project_id="evidence")
        herdr = FakeHerdr()
        adapter = HerdrAdapter(self.coordinator, herdr)
        task = self.coordinator.create_task(project["id"], "fail and leave evidence")
        worker = adapter.launch_task(task["id"], [sys.executable, "-c", ""], wait=False)
        self.coordinator.record_worker_message(worker["id"], "blocker", "needs a credential")
        self.coordinator.settle_reported_worker(worker["id"])

        # While the pane exists it is the diagnosis, so the space stays.
        self.assertFalse(adapter.close_project_space_if_finished(project["id"]))

        # Once the pane is gone the space holds an empty room. Keeping it
        # retains nothing and hides that the project is finished.
        with self.coordinator.store.locked() as data:
            adapter._herdr_state(data)["workers"].pop(worker["id"], None)
        self.assertTrue(adapter.close_project_space_if_finished(project["id"]))

    def test_empty_project_space_is_closed_by_finished_space_sweep(self) -> None:
        root = self.repo("emptyspace")
        project = self.coordinator.register_project(
            "Empty", str(root), project_id="emptyspace"
        )
        task = self.coordinator.create_task(project["id"], "already landed")
        herdr = FakeHerdr()
        adapter = HerdrAdapter(self.coordinator, herdr)
        adapter._ensure_project_workspace(project)
        with self.coordinator.store.locked() as data:
            data["tasks"][task["id"]]["status"] = "merged"
            data["integrations"]["herdr"]["workers"] = {}

        closed = adapter.close_finished_project_spaces()

        self.assertEqual(closed, [project["id"]])
        self.assertEqual(len(herdr.closed_workspaces), 1)
        self.assertNotIn(
            project["id"],
            self.coordinator.store.load()["integrations"]["herdr"]["projects"],
        )

    def test_herdr_persists_ids_and_keeps_projects_isolated(self) -> None:
        first = self.repo("herdr-first")
        second = self.repo("herdr-second")
        p1 = self.coordinator.register_project("First", str(first), project_id="first")
        p2 = self.coordinator.register_project("Second", str(second), project_id="second")
        t1 = self.coordinator.create_task(p1["id"], "first task")
        t2 = self.coordinator.create_task(p2["id"], "second task")
        herdr = FakeHerdr()
        adapter = HerdrAdapter(self.coordinator, herdr)

        w1 = adapter.launch_task(t1["id"], [sys.executable, "-c", ""], wait=False)
        w2 = adapter.launch_task(t2["id"], [sys.executable, "-c", ""], wait=False)
        state = self.state.load()["integrations"]["herdr"]
        # Exactly one workspace per project, and no extra coordinator space.
        self.assertEqual(len(herdr.workspaces), 2)
        self.assertIsNone(state.get("coordinator"))
        # The root tab cannot be closed, so it is named for the job it does.
        renamed = dict(herdr.renamed)
        self.assertEqual(renamed[state["projects"]["first"]["overview_tab_id"]], "Helm Reports · first")
        self.assertEqual(renamed[state["projects"]["second"]["overview_tab_id"]], "Helm Reports · second")
        self.assertNotEqual(state["projects"]["first"]["workspace_id"], state["projects"]["second"]["workspace_id"])
        self.assertEqual(state["workers"][w1["id"]]["workspace_id"], state["projects"]["first"]["workspace_id"])
        self.assertEqual(state["workers"][w2["id"]]["workspace_id"], state["projects"]["second"]["workspace_id"])
        self.assertEqual(herdr.tabs[0][2], w1["workspace"])
        self.assertEqual(herdr.tabs[1][2], w2["workspace"])
        # Labels are display only and a Herdr panel truncates them, so the
        # distinguishing part comes first and the colour rides as a glyph
        # rather than a hex value nobody can read at eight characters.
        self.assertIn("first", herdr.workspaces[0][1])
        self.assertIn(project_glyph(p1["color"]), herdr.workspaces[0][1])
        self.assertTrue(herdr.workspaces[0][1].endswith("first"))
        self.assertLess(len(herdr.workspaces[0][1]), 24)
        self.assertIn(w1["id"].replace("w-", "")[:4], herdr.tabs[0][1])
        self.assertLess(len(herdr.tabs[0][1]), 24)

        # A fresh adapter reuses recorded opaque IDs rather than matching a
        # user resource by label or focused workspace.
        HerdrAdapter(self.coordinator, herdr)._ensure_project_workspace(p1)
        self.assertEqual(len(herdr.workspaces), 2)

    def test_herdr_terminal_result_settles_open_interactive_worker(self) -> None:
        root = self.repo("herdr-result")
        project = self.coordinator.register_project("Result", str(root), project_id="herdr-result")
        task = self.coordinator.create_task(project["id"], "report through an open pane")
        herdr = FakeHerdr()
        adapter = HerdrAdapter(self.coordinator, herdr)
        worker = adapter.launch_task(task["id"], [sys.executable, "-c", ""], wait=False)
        # Pi-like agents can push the terminal protocol result and leave their
        # interactive session alive. No exit record is needed for review.
        Path(worker["log_file"]).write_text(
            json.dumps({"helm": 1, "type": "result", "text": "ready"}) + "\n",
            encoding="utf-8",
        )
        adapter.poll_worker(worker["id"])
        report = self.coordinator.inspect_task(task["id"])
        self.assertEqual(report["task"]["status"], "completed")
        self.assertEqual(report["workers"][0]["status"], "completed")
        self.assertIsNone(report["task"].get("approval"))
        with self.assertRaises(SafetyError):
            self.coordinator.merge_task(task["id"])
        self.assertEqual(herdr.closed_tabs, [])

    def test_herdr_routes_structured_messages_with_stable_project_identity(self) -> None:
        root = self.repo("herdr-messages")
        project = self.coordinator.register_project("Messages", str(root), project_id="messages")
        task = self.coordinator.create_task(project["id"], "report through Herdr")
        herdr = FakeHerdr()
        adapter = HerdrAdapter(self.coordinator, herdr)
        worker = adapter.launch_task(task["id"], [sys.executable, "-c", ""], wait=False)
        Path(worker["log_file"]).write_text(
            json.dumps({"helm": 1, "type": "blocker", "text": "needs review"}) + "\n",
            encoding="utf-8",
        )
        Path(worker["exit_file"]).write_text(json.dumps({"returncode": 0}) + "\n", encoding="utf-8")
        adapter.poll_worker(worker["id"])
        routed = "\n".join(command for _, command in herdr.runs if "printf" in command)
        self.assertIn("Messages", routed)
        self.assertIn("messages", routed)
        self.assertIn(project["color"], routed)
        self.assertIn("needs review", routed)
        self.assertEqual(self.coordinator.inspect_task(task["id"])["task"]["status"], "blocked")

    def test_reported_clean_finish_closes_the_project_space(self) -> None:
        project, herdr, adapter, worker = self._finished_project("finished")
        self.coordinator.record_worker_message(
            worker["id"], "result", "done", requested_status="completed"
        )
        adapter.poll_worker(worker["id"])

        # Completed is not resolved: the work still awaits review, and closing
        # here would take the session away mid-review.
        self.assertFalse(adapter.close_project_space_if_finished(project["id"]))

        # Cleaning the task resolves it, and the space is released.
        self.coordinator.cleanup_task(worker["task_id"])
        self.assertTrue(adapter.close_project_space_if_finished(project["id"]))
        workspace_id = herdr.workspaces[0][0]
        self.assertIn(workspace_id, herdr.closed_workspaces)
        self.assertIsNone(
            self.state.load()["integrations"]["herdr"]["projects"].get(project["id"])
        )

    def test_a_space_the_user_closed_settles_its_worker_instead_of_hanging(self) -> None:
        """A closed workspace is evidence the session is over, not a puzzle.

        Herdr answers `pane get` on a deleted pane with exit status 0 and the
        error in the body. Read as success that yields a dict with no status
        field, which liveness reported as "cannot tell" -- so a worker whose
        space the user closed stayed recorded as running with nothing able to
        correct it.
        """

        class ClosedSpaceHerdr(FakeHerdr):
            def pane_status(self, pane_id: str) -> dict[str, object]:
                raise HerdrNotFound("pane_not_found")

            def pane_run(self, pane_id: str, command: str) -> dict[str, object]:
                if self.runs and pane_id.startswith("pane"):
                    raise HerdrNotFound("pane_not_found")
                return super().pane_run(pane_id, command)

        root = self.repo("closedspace")
        project = self.coordinator.register_project(
            "Closed", str(root), project_id="closedspace"
        )
        task = self.coordinator.create_task(project["id"], "work")
        adapter = HerdrAdapter(self.coordinator, ClosedSpaceHerdr())
        worker = adapter.launch_task(task["id"], [sys.executable, "-c", ""], wait=False)

        polled = adapter.poll_worker(worker["id"])

        self.assertNotEqual(polled["status"], "running")
        # And the stale space record goes, so nothing keeps printing into a
        # room that no longer exists.
        state = self.state.load()["integrations"]["herdr"]
        self.assertIsNone(state["projects"].get(project["id"]))

    def test_releasing_a_finished_tab_also_records_the_exit(self) -> None:
        """Closing the pane is not the whole job.

        An interactive agent that reported a result keeps its session, so the
        runner never writes an exit record, and a worker without one counts as
        live forever -- which pins its worktree behind `helm task cleanup`.
        """
        root = self.repo("settled")
        project = self.coordinator.register_project(
            "Settled", str(root), project_id="settled"
        )
        task = self.coordinator.create_task(project["id"], "work")
        herdr = FakeHerdr()
        adapter = HerdrAdapter(self.coordinator, herdr)
        worker = adapter.launch_task(task["id"], [sys.executable, "-c", ""], wait=False)
        self.coordinator.record_worker_message(
            worker["id"], "result", "done", requested_status="completed"
        )
        self.assertFalse(Path(worker["exit_file"]).exists())

        released = adapter.release_finished_tabs()

        self.assertIn(worker["id"], released)
        self.assertEqual(len(herdr.closed_tabs), 1)
        # The exit record is what unpins the worktree for cleanup.
        self.assertTrue(Path(worker["exit_file"]).exists())
        self.coordinator.cleanup_task(task["id"])

    def test_a_settled_worker_with_no_pane_stops_counting_as_live(self) -> None:
        """Otherwise its directory can never be reclaimed.

        An interactive agent reports its result and keeps its session, so the
        runner writes no exit record, and a worker without one reads as live
        forever. Eighteen of twenty on one root were stranded that way, one
        holding 15 GB of a spike's derived data.
        """
        root = self.repo("paneless")
        project = self.coordinator.register_project(
            "Paneless", str(root), project_id="paneless"
        )
        task = self.coordinator.create_task(project["id"], "work")
        herdr = FakeHerdr()
        adapter = HerdrAdapter(self.coordinator, herdr)
        worker = adapter.launch_task(task["id"], [sys.executable, "-c", ""], wait=False)
        self.coordinator.record_worker_message(
            worker["id"], "result", "done", requested_status="completed"
        )
        # Settled, and the runner never wrote an exit record.
        self.assertFalse(Path(worker["exit_file"]).exists())
        # Its tab has already gone, so nothing is writing in its directory.
        with self.state.locked() as data:
            data["integrations"]["herdr"]["workers"].pop(worker["id"], None)

        adapter.release_finished_tabs()

        self.assertTrue(Path(worker["exit_file"]).exists())
        # Which is what lets the directory be reclaimed.
        worker_dir = Path(worker["config_file"]).parent
        self.coordinator.cleanup_task(task["id"])
        self.assertFalse(worker_dir.exists())

    def test_releasing_finished_tabs_keeps_a_failure_pane_to_be_read(self) -> None:
        """A failed task's pane is the evidence, so it is never swept."""
        root = self.repo("keptpane")
        project = self.coordinator.register_project(
            "Kept", str(root), project_id="keptpane"
        )
        task = self.coordinator.create_task(project["id"], "work")
        herdr = FakeHerdr()
        adapter = HerdrAdapter(self.coordinator, herdr)
        worker = adapter.launch_task(task["id"], [sys.executable, "-c", ""], wait=False)
        self.coordinator.record_worker_message(
            worker["id"], "failure", "it broke", requested_status="failed"
        )

        released = adapter.release_finished_tabs()

        self.assertEqual(released, [])
        self.assertEqual(herdr.closed_tabs, [])

    def test_a_resolved_failure_stops_pinning_the_space(self) -> None:
        project, herdr, adapter, worker = self._finished_project("resolved")
        self.coordinator.record_worker_message(
            worker["id"], "failure", "it broke", requested_status="failed"
        )
        adapter.poll_worker(worker["id"])
        self.assertFalse(adapter.close_project_space_if_finished(project["id"]))

        # Once the failure has been dealt with it must not pin the space
        # forever, or a project with any history never releases one again.
        self.coordinator.cleanup_task(worker["task_id"])
        self.assertTrue(adapter.close_project_space_if_finished(project["id"]))

    def test_a_failed_project_keeps_its_space_as_evidence(self) -> None:
        project, herdr, adapter, worker = self._finished_project("broken")
        self.coordinator.record_worker_message(
            worker["id"], "failure", "it broke", requested_status="failed"
        )
        adapter.poll_worker(worker["id"])

        # The pane holding the failure is the thing someone needs to read.
        self.assertFalse(adapter.close_project_space_if_finished(project["id"]))
        self.assertEqual(herdr.closed_workspaces, [])

    def test_a_space_is_kept_while_any_worker_is_still_running(self) -> None:
        root = self.repo("busy")
        project = self.coordinator.register_project("Busy", str(root), project_id="busy")
        done = self.coordinator.create_task(project["id"], "finished task")
        ongoing = self.coordinator.create_task(project["id"], "second task")
        herdr = FakeHerdr()
        adapter = HerdrAdapter(self.coordinator, herdr)
        finished = adapter.launch_task(done["id"], [sys.executable, "-c", ""], wait=False)
        Path(finished["exit_file"]).write_text(json.dumps({"returncode": 0}) + "\n", encoding="utf-8")
        self.coordinator.record_worker_message(
            finished["id"], "result", "done", requested_status="completed"
        )
        adapter.poll_worker(finished["id"])
        adapter.launch_task(ongoing["id"], [sys.executable, "-c", ""], wait=False)

        self.assertFalse(adapter.close_project_space_if_finished(project["id"]))
        self.assertEqual(herdr.closed_workspaces, [])

    def test_completed_work_holds_its_space_even_with_no_pane_left(self) -> None:
        """A closed tab is not evidence that a change was delivered.

        Releasing a cleanly finished worker's tab is the first thing that
        happens after a result, so reading "no pane" as "nothing to see" made
        the space close on exactly the work still awaiting a decision.
        """
        project, herdr, adapter, worker = self._finished_project("nopane")
        self.coordinator.record_worker_message(
            worker["id"], "result", "done", requested_status="completed"
        )
        adapter.release_finished_tabs()
        self.assertEqual(len(herdr.closed_tabs), 1)

        self.assertFalse(adapter.close_project_space_if_finished(project["id"]))
        self.assertEqual(herdr.closed_workspaces, [])

        # Only real delivery releases it.
        self.coordinator.cleanup_task(worker["task_id"])
        self.assertTrue(adapter.close_project_space_if_finished(project["id"]))

    def test_work_awaiting_a_human_holds_its_space_with_or_without_a_pane(self) -> None:
        project, herdr, adapter, worker = self._finished_project("awaiting")
        self.coordinator.record_worker_message(
            worker["id"],
            "approval-needed",
            "needs a publish decision",
            payload={"action": "publish"},
        )
        with self.coordinator.store.locked() as data:
            adapter._herdr_state(data)["workers"].pop(worker["id"], None)

        self.assertFalse(adapter.close_project_space_if_finished(project["id"]))
        self.assertEqual(herdr.closed_workspaces, [])

    def test_foreman_bookkeeping_alone_never_pins_a_settled_space(self) -> None:
        """A foreman produces no branch, so it has nothing to deliver.

        Holding a space open for its own task record would mean a project that
        finished everything it was asked to do never releases a space again.
        """
        root = self.repo("bookkeeping")
        project = self.coordinator.register_project(
            "Bookkeeping", str(root), project_id="bookkeeping"
        )
        herdr = FakeHerdr()
        adapter = HerdrAdapter(self.coordinator, herdr)
        work = self.coordinator.create_task(project["id"], "the actual change")
        worker = adapter.launch_task(work["id"], [sys.executable, "-c", ""], wait=False)
        self.coordinator.record_worker_message(
            worker["id"], "result", "done", requested_status="completed"
        )
        foreman_task = self.coordinator.create_foreman_task(project["id"])
        foreman = adapter.launch_task(
            foreman_task["id"], [sys.executable, "-c", ""], wait=False
        )
        self.coordinator.record_worker_message(foreman["id"], "result", "handed back")

        # The work is still undecided, so the space stays for it...
        self.assertFalse(adapter.close_project_space_if_finished(project["id"]))
        with self.coordinator.store.locked() as data:
            data["tasks"][work["id"]]["status"] = "merged"

        # ...and once it is delivered, the foreman's own record does not keep
        # the space open on its own.
        self.assertTrue(adapter.close_project_space_if_finished(project["id"]))

    def test_keep_spaces_env_disables_automatic_closing(self) -> None:
        project, herdr, adapter, worker = self._finished_project("kept")
        self.coordinator.record_worker_message(
            worker["id"], "result", "done", requested_status="completed"
        )
        adapter.poll_worker(worker["id"])
        self.coordinator.cleanup_task(worker["task_id"])
        with mock.patch.dict(os.environ, {"HELM_KEEP_SPACES": "1"}):
            self.assertFalse(adapter.close_project_space_if_finished(project["id"]))
        self.assertEqual(herdr.closed_workspaces, [])

    def test_pushed_message_reaches_the_project_pane_without_polling(self) -> None:
        root = self.repo("push")
        project = self.coordinator.register_project("Push", str(root), project_id="push")
        task = self.coordinator.create_task(project["id"], "push a status")
        herdr = FakeHerdr()
        adapter = HerdrAdapter(self.coordinator, herdr)
        worker = adapter.launch_task(task["id"], [sys.executable, "-c", ""], wait=False)
        before = len(herdr.runs)

        self.coordinator.record_worker_message(worker["id"], "status", "halfway through")
        # No poll_worker call: routing is driven by the push itself.
        self.assertTrue(adapter.route_worker_messages(worker["id"]))

        routed = "\n".join(command for _, command in herdr.runs[before:])
        self.assertIn("halfway through", routed)
        self.assertEqual(
            self.coordinator.poll_worker(worker["id"])["status"], "running"
        )

    def test_closed_workspace_record_is_replaced_not_reused(self) -> None:
        root = self.repo("herdr-stale")
        project = self.coordinator.register_project("Stale", str(root), project_id="stale")
        herdr = FakeHerdr()
        adapter = HerdrAdapter(self.coordinator, herdr)
        first = self.coordinator.create_task(project["id"], "first task")
        worker = adapter.launch_task(first["id"], [sys.executable, "-c", ""], wait=False)
        Path(worker["exit_file"]).write_text(json.dumps({"returncode": 0}) + "\n", encoding="utf-8")
        adapter.poll_worker(worker["id"])
        state = self.state.load()["integrations"]["herdr"]
        stale_project = state["projects"]["stale"]["workspace_id"]
        self.assertEqual(len(herdr.workspaces), 1)

        # The user closes the project's Helm workspace between tasks.
        herdr.missing.add(stale_project)
        second = self.coordinator.create_task(project["id"], "second task")
        adapter.launch_task(second["id"], [sys.executable, "-c", ""], wait=False)

        refreshed = self.state.load()["integrations"]["herdr"]
        self.assertEqual(len(herdr.workspaces), 2)
        self.assertNotEqual(refreshed["projects"]["stale"]["workspace_id"], stale_project)
        # Worker tabs that lived in the closed workspace are not kept as owned.
        self.assertTrue(
            all(
                record["workspace_id"] != stale_project
                for record in refreshed["workers"].values()
            )
        )

    def test_unreachable_herdr_never_duplicates_a_recorded_workspace(self) -> None:
        root = self.repo("herdr-transient")
        project = self.coordinator.register_project("Transient", str(root), project_id="transient")
        herdr = FakeHerdr()
        adapter = HerdrAdapter(self.coordinator, herdr)
        first = self.coordinator.create_task(project["id"], "first task")
        adapter.launch_task(first["id"], [sys.executable, "-c", ""], wait=False)
        recorded = self.state.load()["integrations"]["herdr"]["projects"]["transient"]["workspace_id"]

        # A transient failure is not evidence the workspace is gone.
        herdr.unreachable = True
        second = self.coordinator.create_task(project["id"], "second task")
        adapter.launch_task(second["id"], [sys.executable, "-c", ""], wait=False)
        herdr.unreachable = False

        self.assertEqual(len(herdr.workspaces), 1)
        self.assertEqual(
            self.state.load()["integrations"]["herdr"]["projects"]["transient"]["workspace_id"],
            recorded,
        )

    def test_subprocess_herdr_client_sends_the_documented_cli_argv(self) -> None:
        client = SubprocessHerdrClient()
        calls: list[list[str]] = []

        def fake_run(command, **kwargs):  # type: ignore[no-untyped-def]
            calls.append(list(command))
            return subprocess.CompletedProcess(command, 0, '{"result":{"type":"ok"}}', "")

        with mock.patch.dict(os.environ, {"HERDR_ENV": "1"}), mock.patch(
            "helm.herdr.shutil.which", return_value="/usr/bin/herdr"
        ), mock.patch("helm.herdr.subprocess.run", side_effect=fake_run):
            client.workspace_create("label")
            client.tab_create("w1", "worker", "/tmp/worktree")
            client.pane_run("w1:t1:p1", "printf hi")
            client.pane_status("w1:t1:p1")
            client.tab_close("w1:t1")
            client.workspace_close("w1")

        # Herdr already answers in JSON and rejects an explicit flag.
        for command in calls:
            self.assertNotIn("--json", command)
            # Helm never steals the user's focus, not even to its own space.
            self.assertNotIn("--focus", command)
            self.assertNotIn("focus", command[1:3])
        self.assertEqual(calls[0], ["herdr", "workspace", "create", "--label", "label", "--no-focus"])
        self.assertIn("--label", calls[1])
        self.assertNotIn("--name", calls[1])
        # Every space Helm creates is created unfocused.
        self.assertIn("--no-focus", calls[1])
        # `pane run` is variadic and has no focus flag.
        self.assertEqual(calls[2], ["herdr", "pane", "run", "w1:t1:p1", "printf hi"])
        self.assertEqual(calls[3], ["herdr", "pane", "get", "w1:t1:p1"])
        self.assertEqual(calls[4], ["herdr", "tab", "close", "w1:t1"])
        self.assertEqual(calls[5], ["herdr", "workspace", "close", "w1"])

    def test_herdr_unavailable_falls_back_to_core_launcher(self) -> None:
        root = self.repo("herdr-fallback")
        project = self.coordinator.register_project("Fallback", str(root), project_id="fallback")
        task = self.coordinator.create_task(project["id"], "use terminal fallback")
        herdr = FakeHerdr(available=False)
        worker = HerdrAdapter(self.coordinator, herdr).launch_task(
            task["id"],
            [sys.executable, "-c", "print('terminal path')"],
        )
        self.assertEqual(worker["execution"], "process")
        self.assertEqual(worker["status"], "completed")
        self.assertEqual(herdr.workspaces, [])

    def test_herdr_cleanup_refuses_unowned_resources(self) -> None:
        root = self.repo("herdr-cleanup")
        project = self.coordinator.register_project("Cleanup", str(root), project_id="cleanup")
        task = self.coordinator.create_task(project["id"], "cleanup only owned layout")
        herdr = FakeHerdr()
        adapter = HerdrAdapter(self.coordinator, herdr)
        worker = adapter.launch_task(task["id"], [sys.executable, "-c", ""], wait=False)
        Path(worker["exit_file"]).write_text(json.dumps({"returncode": 0}) + "\n", encoding="utf-8")
        adapter.poll_worker(worker["id"])
        with self.state.locked() as data:
            data["integrations"]["herdr"]["workers"]["user-resource"] = {
                "worker_id": "user-resource",
                "task_id": task["id"],
                "project_id": project["id"],
                "tab_id": "user-tab",
                "owned": False,
            }
        self.assertTrue(adapter.cleanup_task(task["id"]))
        self.assertNotIn("user-tab", herdr.closed_tabs)
        self.assertNotIn(worker["id"], self.state.load()["integrations"]["herdr"]["workers"])
        self.assertIn("user-resource", self.state.load()["integrations"]["herdr"]["workers"])
        adapter.cleanup_project(project["id"])
        self.assertNotIn("user-tab", herdr.closed_tabs)
        self.assertIn("user-resource", self.state.load()["integrations"]["herdr"]["workers"])

    def test_partial_herdr_setup_is_compensated_before_fallback(self) -> None:
        class PartialHerdr(FakeHerdr):
            def workspace_create(self, label: str) -> dict[str, object]:
                response = super().workspace_create(label)
                response["result"].pop("root_pane")
                return response

        root = self.repo("partial-herdr")
        project = self.coordinator.register_project("Partial", str(root), project_id="partial")
        task = self.coordinator.create_task(project["id"], "fallback")
        herdr = PartialHerdr()
        worker = HerdrAdapter(self.coordinator, herdr).launch_task(
            task["id"], [sys.executable, "-c", ""]
        )
        self.assertEqual(worker["execution"], "process")
        self.assertTrue(herdr.closed_tabs)
        self.assertTrue(herdr.closed_workspaces)

    def test_a_kept_pane_says_why_it_is_still_there(self) -> None:
        """A retained pane must not look like a working one.

        Helm keeps a blocked or failed task's pane because that pane is the
        diagnosis. Labelled identically to the live driver, the corpse and the
        driver are indistinguishable in the panel, and "which of these is
        running my project" becomes a question only the state file can answer.
        It was asked twice in one session before the label said so.
        """
        from helm.herdr import HerdrAdapter

        task = {"role": "foreman", "status": "blocked"}
        worker = {"id": "w-dead0001"}
        self.assertEqual(
            HerdrAdapter._worker_tab_label(task, worker), "foreman (blocked)"
        )
        self.assertEqual(
            HerdrAdapter._worker_tab_label(
                {"role": "foreman", "status": "failed"}, worker
            ),
            "foreman (failed)",
        )
        # The live one keeps the plain name, so the exception stays legible.
        self.assertEqual(
            HerdrAdapter._worker_tab_label(
                {"role": "foreman", "status": "running"}, worker
            ),
            "foreman",
        )

    def test_a_ticketed_task_tab_leads_with_the_ticket_then_its_purpose(self) -> None:
        """Ticket first, then what the agent is FOR.

        The body used to be a slug of the brief, which produced tabs named
        after whatever words the brief happened to open with -- and the ticket
        was already in front of it, so the slug mostly repeated it. What a tab
        list has to answer is which agent is which: who is writing code, who
        is checking it, who is only reading. The task records that already.
        """
        from helm.herdr import HerdrAdapter

        worker = {"id": "w-abcd1234"}
        self.assertEqual(
            HerdrAdapter._worker_tab_label(
                {"brief": "implement nextest adoption", "ticket": "TCK-123"}, worker
            ),
            "TCK-123 coder abcd",
        )
        self.assertEqual(
            HerdrAdapter._worker_tab_label(
                {"brief": "implement nextest adoption"}, worker
            ),
            "coder abcd",
        )

    def test_a_tab_says_whether_it_reviews_writes_or_only_reads(self) -> None:
        """reviewer/coder is the distinction that matters most -- they run on
        different models by design, and confusing them is how a review gets
        read as authorship. A read-only round is a `scout`: it can be left
        running without wondering whether it is mid-edit."""
        from helm.herdr import HerdrAdapter

        worker = {"id": "w-abcd1234"}
        self.assertEqual(
            HerdrAdapter._worker_tab_label(
                {"role": "reviewer", "ticket": "TCK-123", "brief": "review it"}, worker
            ),
            "TCK-123 reviewer abcd",
        )
        self.assertEqual(
            HerdrAdapter._worker_tab_label(
                {"role": "worker", "read_only": True, "ticket": "TCK-123", "brief": "look"},
                worker,
            ),
            "TCK-123 scout abcd",
        )

    def test_a_long_message_is_handed_over_as_a_file_not_typed_into_the_pane(self) -> None:
        """Pasting a long brief into a live TUI destroys the pane as evidence.

        The agent receives the text either way; what breaks is the pane. A
        couple of thousand characters arriving while the agent redraws its own
        interface interleaves the two streams character by character, and the
        commander watching it -- or anyone diagnosing the worker later -- is
        left with unreadable soup.
        """
        from helm.herdr import HerdrAdapter

        root = self.repo("long-message")
        project = self.coordinator.register_project(
            "LongMessage", str(root), project_id="long-message"
        )
        task = self.coordinator.create_task(project["id"], "receive a brief")
        herdr = FakeHerdr()
        adapter = HerdrAdapter(self.coordinator, herdr)
        worker = adapter.launch_task(
            task["id"], [sys.executable, "-c", ""], wait=False
        )

        brief = "PARAGRAPH. " * 200
        self.assertGreater(len(brief), HerdrAdapter.INLINE_MESSAGE_LIMIT)
        self.assertTrue(adapter.answer_worker(worker["id"], brief))

        sent = " ".join(text for _pane, text in herdr.sent_text)
        # The pane gets a headline and a pointer, never the body. A gist is a
        # subject line cut at MESSAGE_GIST_LIMIT, so asserting the body's own
        # words are absent would fail on any message whose opening words are
        # the ones repeated later; what actually protects the pane is that
        # what goes through it stays bounded however long the message is.
        self.assertLess(len(sent), HerdrAdapter.MESSAGE_GIST_LIMIT + 400)
        self.assertLess(len(sent), len(brief) // 4)
        self.assertIn("read it now", sent)

        inbox = self.coordinator.store.directory / "workers" / worker["id"] / "inbox"
        notes = list(inbox.glob("*.md"))
        self.assertEqual(len(notes), 1)
        self.assertIn("PARAGRAPH.", notes[0].read_text(encoding="utf-8"))

        # A short answer still goes straight in: making every reply a file
        # would put a read between the worker and a one-word direction.
        adapter.answer_worker(worker["id"], "use main")
        self.assertIn("use main", " ".join(t for _p, t in herdr.sent_text))

    def test_a_reviewer_the_retry_replaced_does_not_keep_its_pane(self) -> None:
        """An infrastructure death the system already answered is not evidence.

        A reviewer killed by the OOM killer leaves a failed task, and `failed`
        keeps its pane because a failure is normally what a human has to look
        at. But when the retry produced a real verdict on the same change, that
        diagnosis is done -- and on a machine where reviewers are killed on
        most reviews, holding every one of them open accumulates dead panes.
        The log and the evidence record outlive the tab regardless.
        """
        from helm.herdr import HerdrAdapter

        data = {
            "tasks": {
                "t-dead": {
                    "id": "t-dead", "role": "reviewer",
                    "status": "failed", "reviews": "t-author",
                },
                "t-live": {
                    "id": "t-live", "role": "reviewer",
                    "status": "completed", "reviews": "t-author",
                },
            }
        }
        self.assertTrue(
            HerdrAdapter._superseded_reviewer(data, data["tasks"]["t-dead"])
        )

        # Nothing supersedes an AUTHOR task that failed: that is exactly the
        # pane somebody still has to open.
        author = {"id": "t-x", "role": "worker", "status": "failed", "reviews": None}
        self.assertFalse(HerdrAdapter._superseded_reviewer(data, author))

        # And a reviewer whose retry has not yet reached a verdict keeps its
        # pane -- the failure is still the only account of what happened.
        alone = {
            "tasks": {
                "t-dead": {
                    "id": "t-dead", "role": "reviewer",
                    "status": "failed", "reviews": "t-author",
                },
                "t-running": {
                    "id": "t-running", "role": "reviewer",
                    "status": "running", "reviews": "t-author",
                },
            }
        }
        self.assertFalse(
            HerdrAdapter._superseded_reviewer(alone, alone["tasks"]["t-dead"])
        )


class PrecomputedReviewDiffTests(HelmTestCase):
    """A reviewer that never runs `git diff` cannot loop on `git diff`.

    Five consecutive reviewers on one project died the same way: read the diff,
    then re-run it while composing the verdict, and once that repeats it
    repeats forever. Every brief warned against it more sternly than the last,
    which is the tell that prose was the wrong instrument -- Helm's own brief
    told the reviewer to run git commands in the author's checkout, so Helm was
    steering it into the failure mode and then asking it not to go there.
    """

    def _task_with_change(self, name: str):
        root = self.repo(name)
        project = self.coordinator.register_project(
            name.title(), str(root), project_id=name
        )
        task = self.coordinator.create_task(project["id"], "make a change")
        # The worktree is materialised when a worker is prepared, not at task
        # creation -- and a review needs a branch that actually holds a commit.
        self.coordinator.prepare_external_worker(
            task["id"], [sys.executable, "-c", ""]
        )
        self.commit_on_task_branch(task, "a line the reviewer must see")
        return project, task

    def test_the_diff_is_written_down_and_the_reviewer_is_told_where(self) -> None:
        _project, task = self._task_with_change("precomputed")
        adapter = HerdrAdapter(self.coordinator, client=FakeHerdr())
        base = _git(Path(task["workspace"]), "rev-parse", task["base_branch"]).strip()

        fragment, path = adapter._precomputed_diff(task, base)

        self.assertTrue(path, "the reviewer needs a path, not an empty string")
        written = Path(path).read_text(encoding="utf-8")
        self.assertIn("a line the reviewer must see", written)
        self.assertIn(path, fragment)
        # The instruction is the point; a path alone still leaves `git diff`
        # as the obvious move.
        self.assertIn("DO NOT RUN `git diff`", fragment)
        self.assertIn("re-read the FILE", fragment)
        # It must not forbid the verification a review actually needs.
        self.assertIn("focused tests", fragment)

    def test_a_diff_helm_cannot_compute_does_not_block_the_review(self) -> None:
        """A missing diff must degrade to the old behaviour, not to no review.

        Failing closed here would turn one unreadable repository into a project
        that can never be reviewed again -- strictly worse than a reviewer that
        has to run git itself.
        """
        _project, task = self._task_with_change("degrade")
        adapter = HerdrAdapter(self.coordinator, client=FakeHerdr())

        fragment, path = adapter._precomputed_diff(task, "not-a-real-commit")
        self.assertEqual((fragment, path), ("", ""))

        missing = dict(task, workspace=None)
        self.assertEqual(adapter._precomputed_diff(missing, "HEAD"), ("", ""))

    def test_an_empty_diff_hands_over_nothing_rather_than_an_empty_file(self) -> None:
        """An empty patch would read as 'nothing changed', which is a lie."""
        root = self.repo("nochange")
        project = self.coordinator.register_project(
            "Nochange", str(root), project_id="nochange"
        )
        task = self.coordinator.create_task(project["id"], "change nothing")
        self.coordinator.prepare_external_worker(
            task["id"], [sys.executable, "-c", ""]
        )
        adapter = HerdrAdapter(self.coordinator, client=FakeHerdr())
        base = _git(Path(task["workspace"]), "rev-parse", task["base_branch"]).strip()
        self.assertEqual(adapter._precomputed_diff(task, base), ("", ""))


class AnswerDeliveryIsConfirmedTests(HelmTestCase):
    """Delivery must be observed, not assumed.

    `send_text` fills an input buffer and `Enter` submits it, and neither
    raising was taken as proof. But an agent that was mid-execution treats the
    arriving text as an interruption and parks at its own "what should I do
    instead?" prompt, where the Enter lands on the interrupt dialog rather than
    on the message. The message then sits in the buffer while every record says
    delivered -- which is exactly how a foreman sat stalled with Helm's message
    visible on screen and nothing in Helm noticing. The commander noticed.
    """

    def _worker_in_a_pane(self, name: str):
        root = self.repo(name)
        project = self.coordinator.register_project(
            name.title(), str(root), project_id=name
        )
        task = self.coordinator.create_task(project["id"], "do the thing")
        herdr = FakeHerdr()
        adapter = HerdrAdapter(self.coordinator, herdr)
        worker = adapter.launch_task(task["id"], [sys.executable, "-c", ""], wait=False)
        adapter.QUIET_WAIT_SECONDS = 0.1
        adapter.ANSWER_SETTLE_SECONDS = 0.0
        adapter.SUBMIT_CONFIRM_SECONDS = 0.4
        # This suite is about the terminal-driving path, so the fake stands in
        # for a client that types into a real pane.
        herdr.drives_a_terminal = True
        return adapter, herdr, worker

    def _log(self, worker_id: str) -> Path:
        return self.coordinator.store.directory / "workers" / worker_id / "output.log"

    def test_an_agent_that_acts_on_the_message_counts_as_delivered(self) -> None:
        adapter, herdr, worker = self._worker_in_a_pane("acts")
        log = self._log(worker["id"])
        log.parent.mkdir(parents=True, exist_ok=True)
        log.write_text("idle\n", encoding="utf-8")

        original = herdr.pane_send_keys

        def acting(pane_id: str, keys: str):
            result = original(pane_id, keys)
            if keys == "Enter":
                # Taking the message means doing something, and doing something
                # writes to the pane. That growth is the evidence.
                with log.open("a", encoding="utf-8") as handle:
                    handle.write("reading the file now\n")
            return result

        herdr.pane_send_keys = acting
        self.assertTrue(adapter.answer_worker(worker["id"], "go on then"))

    def test_a_pane_that_never_moves_is_reported_as_not_delivered(self) -> None:
        """The parked-at-a-prompt case. Silence must not read as success."""
        adapter, herdr, worker = self._worker_in_a_pane("parked")
        log = self._log(worker["id"])
        log.parent.mkdir(parents=True, exist_ok=True)
        log.write_text("interrupted, waiting for a human\n", encoding="utf-8")

        self.assertFalse(
            adapter.answer_worker(worker["id"], "go on then"),
            "an unmoving pane must not be reported as delivered",
        )
        # And it must have tried more than once: an interrupt prompt swallows
        # the first Enter as its own answer.
        enters = [k for _pane, k in getattr(herdr, "sent_keys", []) if k == "Enter"]
        self.assertGreaterEqual(len(enters), 2, "one Enter is not a delivery attempt")


class RootTabNameTests(HelmTestCase):
    """The root's own tab carries the product's name, so anyone opening a Helm
    root in Herdr sees the same thing instead of a bare number."""

    def _run(self, env: dict) -> list:
        import os
        from unittest import mock
        from helm import cli

        renamed: list = []

        class FakeClient:
            def available(self) -> bool:
                return True

            def tab_rename(self, tab_id: str, label: str) -> dict:
                renamed.append((tab_id, label))
                return {}

        with mock.patch.dict(os.environ, env, clear=False), \
             mock.patch.object(cli, "SubprocessHerdrClient", FakeClient):
            cli.main(["--state-dir", str(self.state.directory), "status"])
        return renamed

    def test_the_root_names_the_tab_it_is_running_in(self) -> None:
        import os
        renamed = self._run({
            "HERDR_ENV": "1",
            "HERDR_TAB_ID": "w1:t1",
            **({"HELM_WORKER_ID": ""} if os.environ.get("HELM_WORKER_ID") else {}),
        })
        self.assertEqual(renamed, [("w1:t1", cli_root_tab_name())])

    def test_a_worker_never_renames_its_own_tab(self) -> None:
        """A worker runs `helm` commands inside its own pane. Without this
        guard the first report it sent would replace the ticket-prefixed label
        a human scans the tab list for."""
        renamed = self._run({
            "HERDR_ENV": "1",
            "HERDR_TAB_ID": "w1:t9",
            "HELM_WORKER_ID": "w-someworker",
        })
        self.assertEqual(renamed, [])

    def test_naming_happens_once_per_tab(self) -> None:
        import os
        env = {
            "HERDR_ENV": "1",
            "HERDR_TAB_ID": "w1:t1",
            **({"HELM_WORKER_ID": ""} if os.environ.get("HELM_WORKER_ID") else {}),
        }
        self._run(env)
        self.assertEqual(self._run(env), [])


def cli_root_tab_name() -> str:
    from helm import cli
    return cli.ROOT_TAB_NAME


class BusyAgentGetsAPointerNotAPasteTests(HelmTestCase):
    """What garbles a pane is two streams writing to it at once.

    Length was the only trigger for the file handoff, which reads the damage
    backwards: a short message pasted into an agent mid-redraw interleaves
    character-by-character exactly as badly as a long one, and the session
    reads the arriving keystrokes as an interrupt. Observed on a real round --
    the agent's own "Now I'll write the tests first" came out shredded through
    Helm's message, and the turn was cut.
    """

    def _worker_in_a_pane(self, name: str):
        root = self.repo(name)
        project = self.coordinator.register_project(
            name.title(), str(root), project_id=name
        )
        task = self.coordinator.create_task(project["id"], "do the thing")
        herdr = FakeHerdr()
        adapter = HerdrAdapter(self.coordinator, herdr)
        worker = adapter.launch_task(task["id"], [sys.executable, "-c", ""], wait=False)
        adapter.QUIET_WAIT_SECONDS = 0.3
        adapter.QUIET_STILL_SECONDS = 0.1
        adapter.ANSWER_SETTLE_SECONDS = 0.0
        adapter.SUBMIT_CONFIRM_SECONDS = 0.2
        herdr.drives_a_terminal = True
        return adapter, herdr, worker

    def _log(self, worker_id: str) -> Path:
        return self.coordinator.store.directory / "workers" / worker_id / "output.log"

    def _sent_text(self, herdr) -> str:
        return "\n".join(text for _pane, text in getattr(herdr, "sent_text", []))

    def test_a_quiet_agent_still_gets_a_short_message_typed_in(self) -> None:
        adapter, herdr, worker = self._worker_in_a_pane("quiet")
        log = self._log(worker["id"])
        log.parent.mkdir(parents=True, exist_ok=True)
        log.write_text("idle\n", encoding="utf-8")
        adapter.answer_worker(worker["id"], "carry on")
        self.assertIn("carry on", self._sent_text(herdr))

    def test_an_agent_that_never_stops_writing_gets_a_pointer_instead(self) -> None:
        adapter, herdr, worker = self._worker_in_a_pane("busy")
        log = self._log(worker["id"])
        log.parent.mkdir(parents=True, exist_ok=True)
        log.write_text("working\n", encoding="utf-8")
        original_stat = Path.stat
        grew = {"n": 0}

        def never_still(self_path, *args, **kwargs):
            result = original_stat(self_path, *args, **kwargs)
            if str(self_path) == str(log):
                # Keep the observed size moving so the wait never finds a gap.
                grew["n"] += 1
                return type(
                    "Grown", (), {"st_size": result.st_size + grew["n"], "st_mtime": result.st_mtime}
                )()
            return result

        # A title line AND a body, so "pointer instead of paste" stays
        # meaningfully testable. The old message was "carry on" -- eight
        # characters, where the body IS the first line, so once a busy agent
        # started getting a one-line gist the assertion tripped on a degenerate
        # case rather than on the behaviour it guards.
        brief = "RESUME THE ROUND.\n\nEvery detail that belongs in the file and not the pane."
        with mock.patch.object(Path, "stat", never_still):
            adapter.answer_worker(worker["id"], brief)
        sent = self._sent_text(herdr)
        self.assertNotIn(
            "Every detail that belongs in the file",
            sent,
            "a busy agent must not have the message BODY pasted into it",
        )
        self.assertIn("read it now", sent)
        # The subject line is the one thing that does go through, so a commander
        # watching the pane can see what arrived without opening the file.
        self.assertIn("RESUME THE ROUND.", sent)

    def test_the_quiet_wait_says_whether_it_actually_found_a_gap(self) -> None:
        adapter, _herdr, worker = self._worker_in_a_pane("gap")
        log = self._log(worker["id"])
        log.parent.mkdir(parents=True, exist_ok=True)
        log.write_text("idle\n", encoding="utf-8")
        self.assertTrue(adapter._wait_for_quiet(worker["id"]))


class HandedOverMessageSaysWhatItIsAboutTests(HelmTestCase):
    """The pane must show the subject, not just that a message exists.

    A handed-over message used to arrive as a bare "Helm has a message for
    you, written to <path>". The commander watching a foreman then saw that
    something arrived and never what it asked for, so the pane stopped being
    evidence of WHY the foreman did what it did next -- and the pane is the
    one surface a human reads directly. The file is still the message; the
    line in the pane is its subject.
    """

    def _worker(self, name: str):
        root = self.repo(name)
        project = self.coordinator.register_project(name, str(root), project_id=name)
        task = self.coordinator.create_task(project["id"], "receive a brief")
        herdr = FakeHerdr()
        adapter = HerdrAdapter(self.coordinator, herdr)
        worker = adapter.launch_task(task["id"], [sys.executable, "-c", ""], wait=False)
        return adapter, herdr, worker

    def test_the_pane_line_carries_the_first_line_of_the_message(self) -> None:
        adapter, herdr, worker = self._worker("gist-shown")
        brief = (
            "STOP THE REVIEW LOOP: no round ten.\n"
            + "Detail that nobody needs in the pane. " * 40
        )
        self.assertGreater(len(brief), HerdrAdapter.INLINE_MESSAGE_LIMIT)
        self.assertTrue(adapter.answer_worker(worker["id"], brief))

        sent = " ".join(text for _pane, text in herdr.sent_text)
        self.assertIn("STOP THE REVIEW LOOP: no round ten.", sent)
        # Still a handover, not a paste: the body does not go through the pane.
        self.assertNotIn("Detail that nobody needs in the pane. Detail", sent)
        self.assertIn("read it now", sent)

    def test_a_busy_agent_still_gets_the_gist(self) -> None:
        """The case the guard removed, and the only one that happens in practice.

        A handover fires when the message is long OR when the agent never went
        quiet. The gist was computed only for a quiet one, on the grounds that a
        subject line is still text -- but the fallback pointer is ~180 characters
        into that same pane, so nothing was being kept out of it. A foreman is
        almost never quiet, so the commander watching a live pane always saw
        "Helm has a message for you" and had to open a file to learn what about.
        """
        adapter, herdr, worker = self._worker("gist-busy")
        # Short enough to paste inline; the handover is triggered by the agent
        # never falling quiet, which is what makes this the busy path.
        brief = "STAGE 1 IS READ-ONLY: start it now.\n\nDetail nobody needs in the pane."
        self.assertLess(len(brief), HerdrAdapter.INLINE_MESSAGE_LIMIT)
        with mock.patch.object(HerdrAdapter, "_wait_for_quiet", return_value=False):
            self.assertTrue(adapter.answer_worker(worker["id"], brief))
        sent = " ".join(text for _pane, text in herdr.sent_text)
        self.assertIn("STAGE 1 IS READ-ONLY: start it now.", sent)
        self.assertNotIn("Helm has a message for you.", sent)
        self.assertIn("read it now", sent)

    def test_a_long_opening_line_is_cut_into_a_gist_rather_than_withheld(self) -> None:
        # This used to assert the opposite: a message that is one long
        # paragraph has no title line, so no gist was claimed, on the grounds
        # that truncating prose puts body text through the pane. That guard
        # was removed on purpose, because most coordinator messages open with
        # one long sentence and it was withholding the subject line from
        # almost every handover -- the commander watched "Helm has an
        # instruction for you" with no clue what about. So the rule now is a
        # headline cut at a word boundary, and what still protects the pane is
        # the bound, not the silence.
        adapter, herdr, worker = self._worker("no-title")
        brief = "PARAGRAPH. " * 200
        self.assertTrue(adapter.answer_worker(worker["id"], brief))

        sent = " ".join(text for _pane, text in herdr.sent_text)
        self.assertIn("Helm instructs you:", sent)
        self.assertIn("read it now", sent)
        # Bounded however long the message is, and cut at a word boundary.
        self.assertLess(len(sent), HerdrAdapter.MESSAGE_GIST_LIMIT + 400)
        self.assertLess(len(sent), len(brief) // 4)
        self.assertIn("…", sent)

    def test_a_message_with_no_text_at_all_claims_no_gist(self) -> None:
        # A fabricated subject is both noise and a small lie about what the
        # message says, so when there is genuinely nothing to title -- a
        # message that is only whitespace -- none is invented.
        adapter, herdr, worker = self._worker("no-text")
        brief = "   \n\n \t \n" + " " * 500
        self.assertTrue(adapter.answer_worker(worker["id"], brief))

        sent = " ".join(text for _pane, text in herdr.sent_text)
        self.assertIn("Helm has an instruction for you.", sent)
        self.assertIn("read it now", sent)


class TabsNameTheWorkWhenThereIsNoTicketTests(HelmTestCase):
    """"coder 7ded" beside "coder 8e99" identifies neither.

    A ticket already goes first when a project has one. A project that
    numbers milestones instead has the same kind of handle sitting in the
    brief's first line, so use it rather than leaving two hashes.
    """

    def _label(self, task):
        from helm.herdr import HerdrAdapter
        return HerdrAdapter._worker_tab_label(task, {"id": "w-7ded1234"})

    def test_a_milestone_marker_replaces_the_bare_hash(self) -> None:
        label = self._label({"brief": "M4 · Edit + share — the correctness core"})
        self.assertTrue(label.startswith("M4 "), label)

    def test_a_round_marker_is_taken_too(self) -> None:
        label = self._label({"brief": "M3 FIX ROUND 11. Round 10's fixes were not..."})
        self.assertTrue(label.startswith("M3 "), label)

    def test_a_ticket_still_wins(self) -> None:
        label = self._label({"brief": "M4 something", "ticket": "ACME-46"})
        self.assertTrue(label.startswith("ACME-46 "), label)

    def test_a_brief_with_no_marker_is_unchanged(self) -> None:
        label = self._label({"brief": "Investigate the download stall"})
        self.assertEqual(label, "coder 7ded")

    def test_prose_after_the_first_line_never_matches(self) -> None:
        """Fail-closed: a later mention of round 3 is not this round's name."""
        label = self._label({"brief": "Fix the auditor\nas we did in round 3"})
        self.assertEqual(label, "coder 7ded")


class ReviewerTabsNameTheWorkTheyCheckTests(HelmTestCase):
    """A reviewer's own brief says nothing about which milestone it checks.

    It opens with "Run every git command in ...", so the marker lives one hop
    away on the authored task. Without that hop the panel showed "M4 coder"
    and "M2 coder" beside a bare "reviewer" -- the one label a human most
    needs placed, since a review of the wrong milestone reads exactly like a
    review of the right one.
    """

    def _label(self, reviewer_task, data):
        from helm.herdr import HerdrAdapter
        return HerdrAdapter._worker_tab_label(
            reviewer_task, {"id": "w-ab12cdef"}, data
        )

    def test_the_reviewed_milestone_names_the_tab(self) -> None:
        data = {"tasks": {"t-auth": {"brief": "M4 · Edit + share"}}}
        label = self._label(
            {"role": "reviewer", "reviews": "t-auth", "brief": "Run every git command in /x"},
            data,
        )
        self.assertEqual(label, "M4 reviewer ab12")

    def test_the_reviewed_ticket_wins_over_a_marker(self) -> None:
        data = {"tasks": {"t-auth": {"brief": "M4 x", "ticket": "ACME-46"}}}
        label = self._label(
            {"role": "reviewer", "reviews": "t-auth", "brief": "Run every git command in /x"},
            data,
        )
        self.assertEqual(label, "ACME-46 reviewer ab12")

    def test_a_reviewed_task_with_no_marker_leaves_the_tab_plain(self) -> None:
        data = {"tasks": {"t-auth": {"brief": "Investigate the stall"}}}
        label = self._label(
            {"role": "reviewer", "reviews": "t-auth", "brief": "Run every git command in /x"},
            data,
        )
        self.assertEqual(label, "reviewer ab12")

    def test_a_dangling_reviews_link_is_harmless(self) -> None:
        """Fail-closed: a missing reviewed task must not raise."""
        label = self._label(
            {"role": "reviewer", "reviews": "t-gone", "brief": "Run every git command in /x"},
            {"tasks": {}},
        )
        self.assertEqual(label, "reviewer ab12")
