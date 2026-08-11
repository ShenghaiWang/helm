"""`helm route`: root Helm's non-blocking hand-off to a project's foreman.

Root Helm's whole job for one commander input is to identify the project,
make sure its foreman is live, hand the request to it, and come straight
back -- never sit blocked on that foreman's own work, and never let a busy
project's foreman delay routing for another project. Every test here drives
the real `route` CLI command, not a hand-mirrored sequence of core calls, so
a bug in the handler itself would actually be caught.
"""

from __future__ import annotations

import contextlib
import io
import os
import shlex
import shutil
import subprocess
import sys
import time
from pathlib import Path
from unittest import mock

from helm import cli
from helm.core import Coordinator, HelmError, StateStore
from helm.herdr import HerdrAdapter

from tests.support import FakeHerdr, HelmTestCase


class RouteCommandTests(HelmTestCase):
    def _project(self, name: str) -> dict:
        root = self.repo(name)
        return self.coordinator.register_project(name.title(), str(root), project_id=name)

    def _project_root(self, helm_root, name: str) -> tuple[Coordinator, dict]:
        shutil.move(str(self.repo(name)), str(helm_root / "projects" / name))
        coordinator = Coordinator(StateStore(helm_root / "state", helm_root=helm_root))
        project = coordinator.discover_project(helm_root, name)
        return coordinator, project

    def _route(self, helm_root, *args, herdr: FakeHerdr | None = None) -> tuple[int, str]:
        """Invoke the real CLI `route` command, optionally with a fake Herdr wired in."""
        output = io.StringIO()
        patches = []
        if herdr is not None:
            patches.append(
                mock.patch(
                    "helm.cli.HerdrAdapter",
                    lambda coordinator, *a, **k: HerdrAdapter(coordinator, herdr),
                )
            )
        with contextlib.ExitStack() as stack:
            for patch in patches:
                stack.enter_context(patch)
            with contextlib.redirect_stdout(output):
                code = cli.main(["--root", str(helm_root), "route", *args])
        return code, output.getvalue()

    # -- a project with no foreman yet -----------------------------------

    def test_route_appoints_a_missing_foreman_and_never_claims_delivery(self) -> None:
        """A just-spawned agent has no pane ready to receive text.

        Sending into it would be a race against its own startup, so the
        handler must not attempt it and must not print "delivered" -- the
        request survives because it was recorded, not because anything was
        sent anywhere.
        """
        helm_root = self._helm_root("route-root")
        coordinator, project = self._project_root(helm_root, "route-target")
        self.assertIsNone(coordinator.foreman_for(project["id"]))

        command = shlex.join([sys.executable, "-c", ""])
        code, output = self._route(
            helm_root, project["id"], "please pick up the next ticket", "--command", command
        )

        self.assertEqual(code, 0)
        self.assertIn("routed to", output)
        self.assertNotIn("[delivered]", output)
        self.assertIn("recorded", output)
        self.assertIn("starting", output)

        foreman = coordinator.foreman_for(project["id"])
        self.assertIsNotNone(foreman)
        messages = coordinator.store.load()["messages"]
        self.assertIn(
            "please pick up the next ticket",
            "".join(m.get("text", "") for m in messages if m.get("worker_id") == foreman["id"]),
        )

    def test_a_new_foremans_request_survives_and_is_visible_on_its_own_status_read(self) -> None:
        """The durable half of the "record-first" guarantee, proven end to end.

        Root never sent anything into a pane for a brand-new foreman, so the
        only way this request can reach it is by reading it back -- exactly
        what a foreman does at brief/status time via `foreman_brief` /
        `helm project status`. If it is not there, "recorded" was a lie.
        """
        helm_root = self._helm_root("route-status-root")
        coordinator, project = self._project_root(helm_root, "route-status")
        command = shlex.join([sys.executable, "-c", ""])

        code, _ = self._route(
            helm_root, project["id"], "clean up the flaky test", "--command", command
        )
        self.assertEqual(code, 0)

        foreman = coordinator.foreman_for(project["id"])
        self.assertIsNotNone(foreman)
        messages = coordinator.store.load()["messages"]
        recorded = [
            m for m in messages
            if m.get("worker_id") == foreman["id"] and m.get("kind") == "answer"
        ]
        self.assertTrue(recorded)
        self.assertIn("clean up the flaky test", recorded[-1]["text"])

        # The half this test used to assert only in its docstring. Recording
        # the request somewhere in the store is not the guarantee -- reaching
        # the document the foreman actually reads is. A foreman appointed by
        # this very call has its brief composed before the message exists, so
        # `route` writes the request into that brief directly; anything that
        # breaks that path leaves the foreman coming up to a quiet project and
        # standing down on top of an unanswered request.
        foreman_task = coordinator.store.load()["tasks"][foreman["task_id"]]
        self.assertIn("clean up the flaky test", foreman_task["brief"])
        self.assertIn("REQUESTS ROUTED TO YOU", foreman_task["brief"])

    def test_a_routed_request_reaches_a_live_foremans_status_read(self) -> None:
        """The other half: a foreman that was already live when the request landed.

        Its brief was composed long before, so the brief cannot carry this.
        The request has to surface in the record it is told to re-read --
        `helm project status` -- until it has actually acted on it.
        """
        helm_root = self._helm_root("route-live-root")
        coordinator, project = self._project_root(helm_root, "route-live")
        command = shlex.join([sys.executable, "-c", ""])
        self._route(helm_root, project["id"], "first request", "--command", command)
        foreman = coordinator.foreman_for(project["id"])
        self.assertIsNotNone(foreman)

        coordinator.record_worker_message(foreman["id"], "status", "picked up the first one")
        self.assertEqual(coordinator.pending_foreman_requests(project["id"]), [])

        coordinator.record_worker_message(foreman["id"], "answer", "now investigate TICKET-9")
        pending = coordinator.pending_foreman_requests(project["id"])
        self.assertEqual(len(pending), 1)
        self.assertIn("TICKET-9", pending[0]["text"])
        self.assertIn(
            "TICKET-9",
            "".join(
                e["text"] for e in coordinator.project_status(project["id"])["pending_requests"]
            ),
        )

        # Acting on it is what clears it -- the foreman's own next push.
        coordinator.record_worker_message(foreman["id"], "status", "on it")
        self.assertEqual(coordinator.pending_foreman_requests(project["id"]), [])

    def test_route_passes_through_agent_and_model_when_appointing_a_foreman(self) -> None:
        """`route` must offer the same fit/restriction controls `helm foreman` does.

        A commander naming a runtime or model for one project's driver would
        otherwise have that choice silently dropped by the routing path,
        which is a worse outcome than the command refusing to run at all.
        """
        helm_root = self._helm_root("route-agent-root")
        coordinator, project = self._project_root(helm_root, "route-agent")
        command = shlex.join([sys.executable, "-c", ""])

        code, output = self._route(
            helm_root, project["id"], "start work",
            "--command", command, "--agent", "codex", "--model", "gpt-test",
        )
        self.assertEqual(code, 0)

        foreman = coordinator.foreman_for(project["id"])
        self.assertIsNotNone(foreman)
        task = coordinator.store.load()["tasks"][foreman["task_id"]]
        self.assertEqual(task.get("agent"), "codex")
        self.assertEqual(task.get("model"), "gpt-test")

    def test_route_with_no_herdr_starts_a_plain_process_foreman(self) -> None:
        helm_root = self._helm_root("route-no-herdr-root")
        coordinator, project = self._project_root(helm_root, "route-plain")
        command = shlex.join([sys.executable, "-c", ""])

        code, output = self._route(
            helm_root, project["id"], "start work", "--command", command, "--no-herdr",
        )
        self.assertEqual(code, 0)
        foreman = coordinator.foreman_for(project["id"])
        self.assertIsNotNone(foreman)
        self.assertEqual(foreman.get("execution"), "process")

    # -- a project that has declined a foreman ----------------------------

    def test_route_gives_a_truthful_decline_error_for_foreman_false(self) -> None:
        helm_root = self._helm_root("route-decline-root")
        coordinator, project = self._project_root(helm_root, "route-decline")
        with coordinator.store.locked() as data:
            data["projects"][project["id"]]["foreman"] = False

        code, output = self._route(helm_root, project["id"], "do something")
        self.assertEqual(code, 2)

    def test_route_decline_error_names_the_reason_not_a_generic_startup_failure(self) -> None:
        """The decline path must not read as "we tried and failed to start one"."""
        helm_root = self._helm_root("route-decline-text-root")
        coordinator, project = self._project_root(helm_root, "route-decline-text")
        with coordinator.store.locked() as data:
            data["projects"][project["id"]]["foreman"] = False

        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            code = cli.main(["--root", str(helm_root), "route", project["id"], "do something"])
        self.assertEqual(code, 2)
        self.assertIn("declined a foreman", stderr.getvalue())
        self.assertNotIn("see the message above", stderr.getvalue())

    # -- an unknown project ------------------------------------------------

    def test_route_to_an_unknown_project_reports_that_truthfully(self) -> None:
        helm_root = self._helm_root("route-unknown-root")
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            code = cli.main(["--root", str(helm_root), "route", "no-such-project", "do it"])
        self.assertEqual(code, 2)
        self.assertIn("unknown project", stderr.getvalue())

    # -- isolation across projects -----------------------------------------

    def test_route_never_touches_a_different_project(self) -> None:
        helm_root = self._helm_root("route-isolation-root")
        coordinator, busy = self._project_root(helm_root, "route-busy")
        _, quiet = self._project_root(helm_root, "route-quiet")

        # Give the busy project a foreman that looks like it is mid-task --
        # nothing about routing to the quiet project should read or touch it.
        busy_foreman_task = coordinator.create_foreman_task(busy["id"])
        coordinator.prepare_external_worker(
            busy_foreman_task["id"], [sys.executable, "-c", ""], execution="external"
        )
        self.assertIsNotNone(coordinator.foreman_for(busy["id"]))
        self.assertIsNone(coordinator.foreman_for(quiet["id"]))

        command = shlex.join([sys.executable, "-c", ""])
        code, output = self._route(
            helm_root, quiet["id"], "start on the quiet project", "--command", command
        )
        self.assertEqual(code, 0)
        self.assertIn(quiet["id"], output)
        self.assertNotIn(busy["id"], output)

        # The busy project's foreman is exactly as it was -- routing to
        # another project appointed nobody new for it and sent it nothing.
        still_busy_foreman = coordinator.foreman_for(busy["id"])
        self.assertIsNotNone(still_busy_foreman)
        messages = coordinator.store.load()["messages"]
        self.assertNotIn(
            "start on the quiet project",
            "".join(
                m.get("text", "") for m in messages
                if m.get("worker_id") == still_busy_foreman["id"]
            ),
        )
        self.assertIsNotNone(coordinator.foreman_for(quiet["id"]))

    # -- a live, reachable foreman ------------------------------------------

    def test_route_delivers_into_a_live_reachable_foremans_own_session(self) -> None:
        """A reachable foreman gets both: recorded durably, then delivered live."""
        helm_root = self._helm_root("route-live-root")
        coordinator, project = self._project_root(helm_root, "route-live")
        foreman_task = coordinator.create_foreman_task(project["id"])
        herdr = FakeHerdr()
        adapter = HerdrAdapter(coordinator, herdr)
        worker = adapter.launch_task(foreman_task["id"], [sys.executable, "-c", ""], wait=False)

        code, output = self._route(
            helm_root, project["id"], "next: fix the flaky test", herdr=herdr,
        )

        self.assertEqual(code, 0)
        self.assertIn("[delivered]", output)
        self.assertIn(
            "next: fix the flaky test",
            [text for _pane, text in herdr.sent_text],
        )
        messages = coordinator.store.load()["messages"]
        recorded = [
            m for m in messages
            if m.get("worker_id") == worker["id"] and m.get("kind") == "answer"
        ]
        self.assertTrue(recorded)
        self.assertEqual(recorded[-1]["text"], "next: fix the flaky test")

    def test_an_outbound_answer_does_not_refresh_the_workers_liveness_signal(self) -> None:
        """`last_reported_at` is the worker's OWN liveness signal, not Helm's outbox.

        `route` (and `helm worker answer`) deliver an outbound message onto
        the worker's task -- that a message was sent says nothing about
        whether the worker is alive to receive it, let alone that it did.
        Refreshing `last_reported_at` for it let a dead pane that had just
        been sent a request read as freshly healthy by anything reading
        message recency, which is the freshness-contamination bug this
        checks directly against the coordinator's own record.
        """
        project = self._project("route-noop-refresh")
        task = self.coordinator.create_task(project["id"], "an ordinary task")
        worker = self.coordinator.prepare_external_worker(
            task["id"], [sys.executable, "-c", ""], execution="external"
        )
        before = self.coordinator.store.load()["workers"][worker["id"]].get("last_reported_at")

        self.coordinator.record_worker_message(worker["id"], "answer", "an outbound reply")

        after = self.coordinator.store.load()["workers"][worker["id"]].get("last_reported_at")
        self.assertEqual(before, after)

    def test_a_worker_reported_status_still_refreshes_liveness(self) -> None:
        """The fix must not silently break the ordinary, worker-originated case."""
        project = self._project("route-refresh-still-works")
        task = self.coordinator.create_task(project["id"], "an ordinary task")
        worker = self.coordinator.prepare_external_worker(
            task["id"], [sys.executable, "-c", ""], execution="external"
        )
        before = self.coordinator.store.load()["workers"][worker["id"]].get("last_reported_at")

        self.coordinator.record_worker_message(
            worker["id"], "status", "working on it", payload={"summary": True}
        )

        after = self.coordinator.store.load()["workers"][worker["id"]].get("last_reported_at")
        self.assertNotEqual(before, after)
        self.assertTrue(after)

        # A terminal "result" push must refresh it too -- the fix narrows
        # the exception to `answer` alone, not to every non-"status" kind.
        # (Timestamps are second-resolution, so two calls in the same
        # second can legitimately produce the same value; what matters is
        # that it was actively set again, not merely left untouched.)
        with mock.patch("helm.core.now", return_value="2099-01-01T00:00:00Z"):
            self.coordinator.record_worker_message(worker["id"], "result", "done")
        final = self.coordinator.store.load()["workers"][worker["id"]].get("last_reported_at")
        self.assertEqual(final, "2099-01-01T00:00:00Z")

    # -- reachability, not health-verdict, decides delivery -------------------

    def _live_herdr_foreman(self, helm_root, name: str, herdr: FakeHerdr):
        coordinator, project = self._project_root(helm_root, name)
        foreman_task = coordinator.create_foreman_task(project["id"])
        adapter = HerdrAdapter(coordinator, herdr)
        worker = adapter.launch_task(foreman_task["id"], [sys.executable, "-c", ""], wait=False)
        return coordinator, project, worker

    def test_route_delivers_to_a_foreman_that_is_busy_driving_its_project(self) -> None:
        """A foreman deep in its own loop is exactly the reachable, healthy case.

        Its liveness verdict from `worker_health` would not be a plain
        "healthy" here (it has an open hold on its own task, mid-driving),
        but its Herdr pane is real and the provider confirms it -- so it
        must still receive the text. Reachability, not a health verdict, is
        the question `route` answers.
        """
        helm_root = self._helm_root("route-driving-root")
        herdr = FakeHerdr()
        coordinator, project, worker = self._live_herdr_foreman(helm_root, "route-driving", herdr)
        # Give it an open hold, as a foreman mid-drive waiting on its own
        # worker's approval request would have -- a state `worker_health`
        # reports as something other than a bare "healthy" verdict.
        coordinator.record_worker_message(
            worker["id"], "approval-needed", "need to push a branch",
            payload={"action": "push"},
        )

        code, output = self._route(helm_root, project["id"], "keep going", herdr=herdr)

        self.assertEqual(code, 0)
        self.assertIn("[delivered]", output)
        self.assertIn("keep going", [text for _pane, text in herdr.sent_text])

    def test_route_delivers_to_a_foreman_that_has_been_quiet_a_long_time(self) -> None:
        """Silence is not the same as unreachable. A pane can sit idle and correct.

        `worker_health`'s "stalled"/"quiet" verdicts exist to flag silence
        for a human to look at -- they say nothing about whether text sent
        into the pane would actually arrive, which is the only question
        `route` needs answered here.
        """
        helm_root = self._helm_root("route-quiet-root")
        herdr = FakeHerdr()
        coordinator, project, worker = self._live_herdr_foreman(helm_root, "route-quiet", herdr)
        long_ago = "2000-01-01T00:00:00+00:00"
        with coordinator.store.locked() as data:
            data["workers"][worker["id"]]["last_reported_at"] = long_ago
        log_file = Path(worker["log_file"])
        os.utime(log_file, (time.time() - 100_000, time.time() - 100_000))

        code, output = self._route(helm_root, project["id"], "still there?", herdr=herdr)

        self.assertEqual(code, 0)
        self.assertIn("[delivered]", output)
        self.assertIn("still there?", [text for _pane, text in herdr.sent_text])

    def test_route_does_not_let_a_fresh_message_fake_reachability_for_a_dead_pane(self) -> None:
        """Message freshness must never stand in for actual pane reachability.

        This is the exact bug the fix closes: recording `route`'s own
        request bumps `last_reported_at`, and an even fresher push just
        before that would make a genuinely dead pane look perfectly
        current by every message-based signal. `session_reachable` asks
        the provider directly and must not be fooled by either.
        """
        helm_root = self._helm_root("route-contaminated-root")

        class DeadPaneHerdr(FakeHerdr):
            def pane_status(self, pane_id: str) -> dict[str, object]:
                return {"result": {"pane": {"status": "missing"}}}

        herdr = DeadPaneHerdr()
        coordinator, project, worker = self._live_herdr_foreman(
            helm_root, "route-contaminated", herdr
        )
        # A status push landed on this worker a moment ago -- as fresh as a
        # message can be -- immediately before its pane actually died.
        coordinator.record_worker_message(
            worker["id"], "status", "still going", payload={"summary": True}
        )

        code, output = self._route(helm_root, project["id"], "keep going", herdr=herdr)

        self.assertEqual(code, 0)
        self.assertNotIn("[delivered]", output)
        self.assertIn("no reachable session", output)
        self.assertNotIn("keep going", [text for _pane, text in herdr.sent_text])

    def test_route_refuses_to_claim_delivery_to_a_dead_herdr_pane(self) -> None:
        """The provider itself says the pane is gone. Route must not pretend otherwise.

        One foreman per project is preserved -- this does not stop or
        replace the dead one, only refuses to lie about reaching it, and
        says how to replace it by hand.
        """
        helm_root = self._helm_root("route-dead-root")

        class DeadPaneHerdr(FakeHerdr):
            def pane_status(self, pane_id: str) -> dict[str, object]:
                return {"result": {"pane": {"status": "missing"}}}

        herdr = DeadPaneHerdr()
        coordinator, project, worker = self._live_herdr_foreman(helm_root, "route-dead", herdr)

        code, output = self._route(helm_root, project["id"], "keep going", herdr=herdr)

        self.assertEqual(code, 0)
        self.assertNotIn("[delivered]", output)
        self.assertIn("no reachable session", output)
        # Not silently replaced: `session_reachable`'s own reconciliation is
        # what settles the dead pane's worker record (the strongest evidence
        # available that the session is over), and route only reports it --
        # it never starts a second foreman on its own. The CLI still says
        # how to hand the project a new one.
        settled = coordinator.store.load()["workers"][worker["id"]]
        self.assertNotEqual(settled["status"], "running")
        self.assertIn(f"helm foreman {project['id']}", output)

    def test_a_dead_panes_request_is_recorded_before_reconciliation_can_lose_it(self) -> None:
        """The exact bug this round fixes: the request must not be lost.

        Recording happens first, while the worker is still "running". Only
        then is reachability checked -- and `session_reachable`'s own
        reconciliation of the dead pane settles the worker to "failed"
        *after* the record already landed. The request is durably visible
        in the project's own messages and status record regardless, and the
        CLI's "recorded" claim is actually true rather than aspirational.
        """
        helm_root = self._helm_root("route-dead-durable-root")

        class DeadPaneHerdr(FakeHerdr):
            def pane_status(self, pane_id: str) -> dict[str, object]:
                return {"result": {"pane": {"status": "missing"}}}

        herdr = DeadPaneHerdr()
        coordinator, project, worker = self._live_herdr_foreman(
            helm_root, "route-dead-durable", herdr
        )

        code, output = self._route(
            helm_root, project["id"], "the exact request text", herdr=herdr,
        )

        self.assertEqual(code, 0)
        self.assertIn("recorded", output)
        self.assertNotIn("[delivered]", output)
        # Durably in the message log, on the foreman's own worker/task --
        # not silently dropped because reconciliation later settled it.
        messages = coordinator.store.load()["messages"]
        recorded = [
            m for m in messages
            if m.get("worker_id") == worker["id"] and m.get("kind") == "answer"
        ]
        self.assertTrue(recorded)
        self.assertEqual(recorded[-1]["text"], "the exact request text")
        # It was recorded onto the task while the worker was still running,
        # and reconciliation only settled the worker afterward -- the
        # record itself was never at risk from the ordering.
        self.assertEqual(recorded[-1]["task_id"], worker["task_id"])
        settled = coordinator.store.load()["workers"][worker["id"]]
        self.assertNotEqual(settled["status"], "running")

    def test_route_never_claims_delivery_for_a_plain_process_foreman(self) -> None:
        """A plain-process foreman has no Herdr pane at all -- no input channel Helm owns.

        `session_reachable` returns False for any worker whose execution is
        not `herdr`, which is exactly right here: there is nothing to send
        text into, live or otherwise, so this must always read as
        "recorded only", never "delivered".
        """
        helm_root = self._helm_root("route-process-root")
        coordinator, project = self._project_root(helm_root, "route-process")
        foreman_task = coordinator.create_foreman_task(project["id"])
        coordinator.launch_worker(foreman_task["id"], [sys.executable, "-c", ""], wait=False)

        code, output = self._route(helm_root, project["id"], "keep going")

        self.assertEqual(code, 0)
        self.assertNotIn("[delivered]", output)
        self.assertIn("no reachable session", output)

    def test_route_reports_the_send_itself_failing_distinctly(self) -> None:
        """Reachable but the send call itself raised: still truthful, still not "delivered"."""
        helm_root = self._helm_root("route-sendfail-root")
        herdr = FakeHerdr()
        coordinator, project, worker = self._live_herdr_foreman(helm_root, "route-sendfail", herdr)

        def failing_answer_worker(self, worker_id, text):
            raise HelmError("pane refused the send")

        with mock.patch.object(HerdrAdapter, "answer_worker", failing_answer_worker):
            code, output = self._route(helm_root, project["id"], "keep going", herdr=herdr)

        self.assertEqual(code, 0)
        self.assertNotIn("[delivered]", output)
        self.assertIn("the send itself failed", output)

    # -- excluded agent / restricted model refusals leave no orphan -----------

    def _fake_agent_cli(self, *names: str) -> Path:
        bin_dir = Path(self.temp.name) / f"route-bin-{'-'.join(names)}"
        bin_dir.mkdir(exist_ok=True)
        for name in names:
            executable = bin_dir / name
            executable.write_text("#!/bin/sh\nexit 0\n")
            executable.chmod(0o755)
        return bin_dir

    def test_route_refuses_an_excluded_agent_without_crashing_or_leaking_a_worktree(self) -> None:
        """A policy refusal must surface as HelmError, not a rollback TypeError.

        A foreman task has no branch (`WORKTREELESS_ROLES`); rollback used
        to pass that `None` straight into `git branch -D` and crash instead
        of reporting the refusal. Fixed at the rollback's source, so this
        proves the whole path: a clean refusal, and no branch or worktree
        left behind for a launch that never happened.
        """
        helm_root = self._helm_root("route-excluded-root")
        coordinator, project = self._project_root(helm_root, "route-excluded")
        bin_dir = self._fake_agent_cli("codex")
        env = {
            "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
            "HELM_EXCLUDE_AGENTS": "codex",
        }
        before_branches = subprocess.run(
            ["git", "-C", str(Path(coordinator.get_project(project['id'])['root'])),
             "branch", "--list"],
            check=True, text=True, stdout=subprocess.PIPE,
        ).stdout

        stderr = io.StringIO()
        with mock.patch.dict(os.environ, env), contextlib.redirect_stderr(stderr):
            code = cli.main([
                "--root", str(helm_root), "route", project["id"], "start work",
                "--agent", "codex",
            ])

        self.assertEqual(code, 2)
        self.assertIn("excluded", stderr.getvalue())
        self.assertIsNone(coordinator.foreman_for(project["id"]))
        after_branches = subprocess.run(
            ["git", "-C", str(Path(coordinator.get_project(project['id'])['root'])),
             "branch", "--list"],
            check=True, text=True, stdout=subprocess.PIPE,
        ).stdout
        self.assertEqual(before_branches, after_branches)
        # No leftover worker directory either -- rollback's own cleanup.
        workers_root = coordinator.store.directory / "workers"
        if workers_root.is_dir():
            self.assertEqual(list(workers_root.iterdir()), [])

    def test_route_refuses_a_restricted_model_on_the_wrong_runtime_without_crashing(self) -> None:
        helm_root = self._helm_root("route-restricted-root")
        self.write_preferences(helm_root, model={"runtimes": {"claude": ["claude"]}})
        coordinator, project = self._project_root(helm_root, "route-restricted")
        bin_dir = self._fake_agent_cli("codex")
        env = {
            "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
        }

        stderr = io.StringIO()
        with mock.patch.dict(os.environ, env), contextlib.redirect_stderr(stderr):
            code = cli.main([
                "--root", str(helm_root), "route", project["id"], "start work",
                "--agent", "codex", "--model", "claude-opus-5",
            ])

        self.assertEqual(code, 2)
        self.assertIn("restrict", stderr.getvalue())
        self.assertIsNone(coordinator.foreman_for(project["id"]))

    # -- authority boundary ---------------------------------------------------

    def test_a_worker_cannot_route_on_its_own_behalf(self) -> None:
        """Routing is root Helm's own job -- an agent may not authorize it for itself."""
        helm_root = self._helm_root("route-authority-root")
        coordinator, project = self._project_root(helm_root, "route-authority")
        task = coordinator.create_task(project["id"], "an ordinary task")
        worker = coordinator.prepare_external_worker(
            task["id"], [sys.executable, "-c", ""], execution="external"
        )

        with mock.patch.dict(os.environ, {"HELM_WORKER_ID": worker["id"]}):
            stderr = io.StringIO()
            with contextlib.redirect_stderr(stderr):
                code = cli.main([
                    "--root", str(helm_root),
                    "route", project["id"], "do something",
                ])
        self.assertEqual(code, 2)
        self.assertIn("held at the Helm root", stderr.getvalue())

    # -- non-blocking behavior -------------------------------------------------

    def test_route_never_waits_on_a_worker_launch(self) -> None:
        """`route` must spawn with `wait=False`, never a synchronous run-to-completion.

        A `wait=True` launch would make routing to one project's foreman
        take as long as that foreman's whole session -- exactly the
        cross-project blocking this command exists to prevent. Patching
        both launch paths to fail loudly on `wait=True` catches a
        regression that a timing-based test could miss or flake on.
        """
        helm_root = self._helm_root("route-nonblocking-root")
        coordinator, project = self._project_root(helm_root, "route-nonblocking")
        command = shlex.join([sys.executable, "-c", ""])

        real_launch_worker = Coordinator.launch_worker
        real_launch_task = HerdrAdapter.launch_task

        def guarded_launch_worker(self, task_id, cmd=None, *, wait=True, **kwargs):
            assert wait is False, "route must never launch a foreman with wait=True"
            return real_launch_worker(self, task_id, cmd, wait=wait, **kwargs)

        def guarded_launch_task(self, task_id, cmd=None, *, wait=True, **kwargs):
            assert wait is False, "route must never launch a foreman with wait=True"
            return real_launch_task(self, task_id, cmd, wait=wait, **kwargs)

        with mock.patch.object(Coordinator, "launch_worker", guarded_launch_worker), \
             mock.patch.object(HerdrAdapter, "launch_task", guarded_launch_task):
            code, output = self._route(
                helm_root, project["id"], "start work", "--command", command,
            )
        self.assertEqual(code, 0)
        self.assertIsNotNone(coordinator.foreman_for(project["id"]))
