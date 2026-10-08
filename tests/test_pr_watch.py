"""The PR watch: an open PR's checks and reviews wake the lead that owns it.

Under turns a lead exists only inside a turn, so a red check or a reviewer's
thread on its PR reached nobody until somebody looked. These drive the real
pass -- `sync_open_pull_requests` and the CLI's delivery -- with the forge
replaced by fixed payloads, and check what reached which lead.
"""

from __future__ import annotations

import contextlib
import io
import os
import sys
import time
from pathlib import Path
from unittest import mock

from helm import cli
from helm.core import Coordinator
from helm.errors import HelmError
from helm.herdr import HerdrAdapter
from tests.support import FakeHerdr, HelmTestCase

URL = "https://github.com/example/widgets/pull/7"
AUTHOR = "author-account"
HEAD = "a" * 40
_COMMIT = (
    "from pathlib import Path; import subprocess; Path('c.txt').write_text('w'); "
    "subprocess.run(['git','add','c.txt'],check=True); subprocess.run(['git','commit','-qm','c'],check=True)"
)
_IDLE = [sys.executable, "-c", "import time; time.sleep(120)"]


def _check(name: str, conclusion: str = "SUCCESS", status: str = "COMPLETED") -> dict:
    return {
        "__typename": "CheckRun", "name": name, "status": status,
        "conclusion": conclusion if status == "COMPLETED" else "",
        "detailsUrl": f"https://ci.example.test/run/{name}",
    }


def _payload(*, state: str = "OPEN", checks=None, comments=(), reviews=(), decision="REVIEW_REQUIRED", **extra) -> dict:
    return {
        "url": URL, "state": state, "reviewDecision": decision, "mergeStateStatus": "BLOCKED",
        "headRefOid": HEAD, "author": {"login": AUTHOR}, "body": "",
        "statusCheckRollup": list(checks if checks is not None else [_check("build", status="IN_PROGRESS")]),
        "comments": list(comments), "reviews": list(reviews), **extra,
    }


def _thread(tid: str, author: str, body: str, *, path="src/app.py", line=12, resolved=False) -> dict:
    comment = {"id": f"{tid}-c1", "author": author, "body": body}
    return {"id": tid, "resolved": resolved, "path": path, "line": line, "first": comment, "last": comment}


class PullRequestWatchTests(HelmTestCase):
    def setUp(self) -> None:
        super().setUp()
        # A gh on PATH, so the pass does not stop at "not installed"; the
        # reads themselves are replaced, so it is never run.
        bin_dir = Path(self.temp.name) / "bin"
        bin_dir.mkdir()
        (bin_dir / "gh").write_text("#!/bin/sh\nexit 1\n")
        (bin_dir / "gh").chmod(0o755)
        path = mock.patch.dict(os.environ, {"PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}"})
        path.start()
        self.addCleanup(path.stop)
        herdr = FakeHerdr()
        adapter = mock.patch("helm.cli.HerdrAdapter", lambda c, *a, **k: HerdrAdapter(c, herdr))
        adapter.start()
        self.addCleanup(adapter.stop)
        self.started: list[dict] = []

    def _lead(self, project_id: str, ticket: str | None) -> dict:
        task = self.coordinator.create_foreman_task(project_id, ticket=ticket)
        worker = self.coordinator.launch_worker(task["id"], _IDLE, wait=False)
        self.addCleanup(self._stop, worker["id"])
        return worker

    def _stop(self, worker_id: str) -> None:
        with contextlib.suppress(HelmError, OSError):
            self.coordinator.stop_worker(worker_id, reason="test over")

    def _pr_task(self, name: str, *, ticket: str | None = "TICKET-7", lead: bool = True):
        root = self.repo(name)
        project = self.coordinator.register_project(
            name.title(), str(root), project_id=name, delivery_policy="pr"
        )
        lead_worker = self._lead(project["id"], ticket) if lead else None
        task = self.coordinator.create_task(project["id"], "ship the widget change", ticket=ticket)
        self.coordinator.launch_worker(task["id"], [sys.executable, "-c", _COMMIT])
        self.coordinator.record_pr_status(task["id"], state="open", url=URL)
        return project, task, lead_worker

    def _pass(self, payload: dict, threads=None, *, at: float) -> dict:
        with mock.patch.object(Coordinator, "read_pull_request", return_value=payload), \
                mock.patch.object(Coordinator, "read_review_threads", return_value=threads):
            return self.coordinator.sync_open_pull_requests(now_epoch=at)

    def _deliver(self, synced: dict) -> list[str]:
        with contextlib.redirect_stdout(io.StringIO()):
            return cli._deliver_pr_watch_events(self.coordinator, synced["events"], herdr=False)

    def _watch_messages(self, worker_id: str) -> list[str]:
        return [
            m["text"] for m in self.state.load()["messages"]
            if m.get("worker_id") == worker_id and m.get("kind") == "answer"
            and (m.get("payload") or {}).get("via") == "pr-watch"
        ]

    # -- starting -----------------------------------------------------------

    def test_watching_starts_when_the_pr_is_registered(self) -> None:
        _project, task, _lead = self._pr_task("starts")
        watch = self.state.load()["tasks"][task["id"]]["delivery"]["watch"]
        self.assertTrue(watch["since"])
        self.assertEqual(watch["url"], URL)
        # Just registered: nothing is due yet.
        self.assertEqual(self._pass(_payload(), at=time.time())["watched"], [])
        # One watch interval later it is read, with no command anyone ran.
        self.assertEqual(self._pass(_payload(), at=time.time() + 130)["watched"], [task["id"]])

    # -- what wakes the lead -----------------------------------------------

    def test_a_red_check_reaches_the_owning_lead_once(self) -> None:
        _project, task, lead = self._pr_task("redcheck")
        red = _payload(checks=[_check("build", "FAILURE"), _check("lint")])
        synced = self._pass(red, at=time.time() + 130)
        self.assertEqual(len(synced["events"]), 1)
        lines = self._deliver(synced)
        self.assertEqual(len(lines), 1, lines)
        messages = self._watch_messages(lead["id"])
        self.assertEqual(len(messages), 1)
        self.assertIn("CI failed: build [FAILURE] https://ci.example.test/run/build", messages[0])
        self.assertIn(URL, messages[0])
        self.assertIn("never force-push", messages[0])
        self.assertIn("approval-needed", messages[0])
        # The same state read again is not news, and nothing is sent twice.
        again = self._pass(red, at=time.time() + 260)
        self.assertEqual(again["events"], [])
        self._deliver(again)
        self.assertEqual(len(self._watch_messages(lead["id"])), 1)
        # Nor does it come back later as a queued retry.
        self.assertIsNone(self.state.load()["tasks"][task["id"]]["delivery"]["watch"].get("outbox"))

    def test_a_new_unresolved_thread_arrives_with_its_excerpt(self) -> None:
        _project, task, lead = self._pr_task("thread")
        self._deliver(self._pass(_payload(), [], at=time.time() + 130))
        threads = [
            _thread("T1", "reviewer", "Please rename this; the old name hides what it caches."),
            # The author's own note on the diff is the work, not news.
            _thread("T2", AUTHOR, "note to self"),
            _thread("T3", "reviewer", "already settled", resolved=True),
        ]
        synced = self._pass(_payload(), threads, at=time.time() + 260)
        self.assertEqual(len(synced["events"]), 1)
        self._deliver(synced)
        (message,) = self._watch_messages(lead["id"])
        self.assertIn(
            'New review thread by reviewer at src/app.py:12: "Please rename this; the old name hides what it caches."',
            message,
        )
        self.assertNotIn("note to self", message)
        self.assertNotIn("already settled", message)
        stored = self.state.load()["tasks"][task["id"]]["delivery"]["watch"]["snapshot"]
        self.assertEqual(set(stored["threads"]), {"T1", "T2"})

    def test_several_changes_in_one_pass_are_one_message(self) -> None:
        _project, _task, lead = self._pr_task("coalesce")
        payload = _payload(
            checks=[_check("build", "FAILURE")],
            decision="CHANGES_REQUESTED",
            comments=[
                {"id": "C1", "author": {"login": "reviewer"}, "body": "Can this be split?"},
                {"id": "C2", "author": {"login": "deploy[bot]"}, "body": "Preview ready"},
            ],
        )
        self._deliver(self._pass(payload, [_thread("T1", "reviewer", "why?")], at=time.time() + 130))
        (message,) = self._watch_messages(lead["id"])
        for expected in ("CI failed: build", "New review thread by reviewer", 'New comment by reviewer: "Can this be split?"',
                         "Review decision: CHANGES_REQUESTED"):
            self.assertIn(expected, message)
        self.assertNotIn("Preview ready", message)

    def test_green_after_red_is_reported_without_a_fix_instruction(self) -> None:
        _project, _task, lead = self._pr_task("green")
        self._deliver(self._pass(_payload(checks=[_check("build", "FAILURE")]), at=time.time() + 130))
        self._deliver(self._pass(_payload(checks=[_check("build")]), at=time.time() + 260))
        messages = self._watch_messages(lead["id"])
        self.assertEqual(len(messages), 2)
        self.assertIn("CI green: all 1 check(s) passed", messages[1])
        self.assertIn("Nothing here needs a fix", messages[1])

    def test_a_merge_is_still_recorded_as_before_and_the_lead_hears_of_it(self) -> None:
        _project, task, lead = self._pr_task("merged")
        merged = _payload(state="MERGED", checks=[_check("build")], mergeCommit={"oid": "b" * 40})
        synced = self._pass(merged, at=time.time() + 130)
        self.assertEqual(synced["merged"], [task["id"]])
        after = self.state.load()["tasks"][task["id"]]
        self.assertEqual(after["status"], "pr-merged")
        self.assertEqual(after["delivery"]["merge_commit"], "b" * 40)
        self._deliver(synced)
        (message,) = self._watch_messages(lead["id"])
        self.assertIn("Merged.", message)
        # A merged PR is no longer watched.
        self.assertEqual(self._pass(merged, at=time.time() + 400)["watched"], [])

    # -- which lead ---------------------------------------------------------

    def test_with_no_live_lead_one_is_appointed_for_the_ticket(self) -> None:
        project, task, _ = self._pr_task("nolead", lead=False)
        self.assertIsNone(self.coordinator.driver_named(project["id"], "TICKET-7"))
        appointed: list[dict] = []

        def start_foreman(coordinator, project_id, *, herdr=True, request=None, ticket=None, **_kw):
            lead_task = coordinator.create_foreman_task(project_id, request=request, ticket=ticket)
            worker = coordinator.launch_worker(lead_task["id"], _IDLE, wait=False)
            self.addCleanup(self._stop, worker["id"])
            appointed.append({"request": request, "ticket": ticket})
            return {"task": lead_task, "worker": worker}

        synced = self._pass(_payload(checks=[_check("build", "FAILURE")]), at=time.time() + 130)
        with mock.patch.object(cli, "_start_foreman", start_foreman):
            self._deliver(synced)
        self.assertEqual(len(appointed), 1)
        self.assertEqual(appointed[0]["ticket"], "TICKET-7")
        self.assertIn("CI failed: build", appointed[0]["request"])
        lead = self.coordinator.driver_named(project["id"], "TICKET-7")
        self.assertIsNotNone(lead)
        self.assertEqual(len(self._watch_messages(lead["id"])), 1)
        watch = self.state.load()["tasks"][task["id"]]["delivery"]["watch"]
        self.assertEqual(watch["delivered"][-1]["outcome"], "appointed")

    def test_an_event_never_reaches_another_projects_lead(self) -> None:
        _a, task_a, lead_a = self._pr_task("alpha")
        _b, task_b, lead_b = self._pr_task("beta")
        # Only alpha's PR changes.
        data = self.state.load()
        data["tasks"][task_b["id"]]["delivery"]["watch"]["since"] = "2999-01-01T00:00:00Z"
        self.state.save(data)
        synced = self._pass(_payload(checks=[_check("build", "FAILURE")]), at=time.time() + 130)
        self.assertEqual([e["task_id"] for e in synced["events"]], [task_a["id"]])
        self._deliver(synced)
        self.assertEqual(len(self._watch_messages(lead_a["id"])), 1)
        # Same ticket name in the other project, and still nothing reached it.
        self.assertEqual(self._watch_messages(lead_b["id"]), [])

    def test_a_lead_running_the_pass_queues_but_does_not_deliver(self) -> None:
        _project, task, lead = self._pr_task("leadcaller")
        synced = self._pass(_payload(checks=[_check("build", "FAILURE")]), at=time.time() + 130)
        with mock.patch.object(Coordinator, "caller_role", return_value="foreman"):
            self.assertEqual(self._deliver(synced), [])
        self.assertEqual(self._watch_messages(lead["id"]), [])
        # The root's next pass finds it queued, without reading the PR again.
        with mock.patch.object(Coordinator, "read_pull_request", side_effect=AssertionError("not due")):
            later = self.coordinator.sync_open_pull_requests(now_epoch=time.time() + 140)
        self.assertEqual([e["task_id"] for e in later["events"]], [task["id"]])
        self._deliver(later)
        self.assertEqual(len(self._watch_messages(lead["id"])), 1)

    # -- bounds and failures -----------------------------------------------

    def test_each_pr_is_read_at_most_once_per_interval(self) -> None:
        self._pr_task("interval")
        start = time.time()
        with mock.patch.object(Coordinator, "read_pull_request", return_value=_payload()) as read, \
                mock.patch.object(Coordinator, "read_review_threads", return_value=[]):
            self.coordinator.sync_open_pull_requests(now_epoch=start + 130)
            self.coordinator.sync_open_pull_requests(now_epoch=start + 140)
            self.coordinator.sync_open_pull_requests(now_epoch=start + 200)
            self.assertEqual(read.call_count, 1)
            self.coordinator.sync_open_pull_requests(now_epoch=start + 260)
            self.assertEqual(read.call_count, 2)

    def test_a_missing_gh_is_recorded_once_and_shows_in_pending(self) -> None:
        _project, task, _lead = self._pr_task("nogh")
        empty = Path(self.temp.name) / "empty"
        empty.mkdir()
        with mock.patch.dict(os.environ, {"PATH": str(empty)}):
            for offset in (130, 260, 400):
                outcome = self.coordinator.sync_open_pull_requests(now_epoch=time.time() + offset)
                self.assertEqual(outcome["skipped"][0]["task_id"], task["id"])
        watch = self.state.load()["tasks"][task["id"]]["delivery"]["watch"]
        self.assertEqual(watch["error_kind"], "missing")
        status = self.coordinator.project_status("nogh")
        said = [e for e in status.get("situation", []) if "PR watch cannot read" in e.get("text", "")]
        self.assertEqual(len(said), 1, "a missing gh is said once, not on every pass")
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            cli.main(["--state-dir", str(self.state.directory), "pending"])
        self.assertIn("PR watch cannot read", out.getvalue())

    def test_a_logged_out_gh_is_surfaced_and_an_offline_one_is_not(self) -> None:
        _project, task, _lead = self._pr_task("authgh")
        offline = HelmError("error connecting to api.github.com")
        with mock.patch.object(Coordinator, "read_pull_request", side_effect=offline):
            self.coordinator.sync_open_pull_requests(now_epoch=time.time() + 130)
        status = self.coordinator.project_status("authgh")
        self.assertFalse([e for e in status.get("situation", []) if "PR watch" in e.get("text", "")])
        logged_out = HelmError("To get started with GitHub CLI, please run:  gh auth login")
        with mock.patch.object(Coordinator, "read_pull_request", side_effect=logged_out):
            self.coordinator.sync_open_pull_requests(now_epoch=time.time() + 260)
            self.coordinator.sync_open_pull_requests(now_epoch=time.time() + 400)
        status = self.coordinator.project_status("authgh")
        said = [e for e in status.get("situation", []) if "PR watch cannot read" in e.get("text", "")]
        self.assertEqual(len(said), 1)
        self.assertIn("gh auth login", said[0]["text"])
        self.assertEqual(self.state.load()["tasks"][task["id"]]["delivery"]["watch"]["error_kind"], "unauthenticated")

    def test_a_pr_registered_before_the_watch_existed_starts_from_a_silent_baseline(self) -> None:
        _project, task, lead = self._pr_task("legacy")
        data = self.state.load()
        data["tasks"][task["id"]]["delivery"].pop("watch")
        self.state.save(data)
        red = _payload(checks=[_check("build", "FAILURE")])
        self.assertEqual(self._pass(red, at=time.time() + 130)["events"], [])
        redder = _payload(checks=[_check("build", "FAILURE"), _check("lint", "TIMED_OUT")])
        synced = self._pass(redder, at=time.time() + 260)
        self._deliver(synced)
        (message,) = self._watch_messages(lead["id"])
        self.assertIn("lint [TIMED_OUT]", message)
        self.assertNotIn("CI failed: build", message)

    def test_helm_pr_watch_once_runs_a_pass(self) -> None:
        _project, _task, lead = self._pr_task("command")
        data = self.state.load()
        for task in data["tasks"].values():
            if task.get("status") == "pr-open":
                task["delivery"]["watch"]["since"] = "2000-01-01T00:00:00Z"
        self.state.save(data)
        out = io.StringIO()
        with mock.patch.object(Coordinator, "read_pull_request",
                               return_value=_payload(checks=[_check("build", "FAILURE")])), \
                mock.patch.object(Coordinator, "read_review_threads", return_value=[]), \
                contextlib.redirect_stdout(out):
            code = cli.main(["--state-dir", str(self.state.directory), "pr", "watch", "--once"])
        self.assertEqual(code, 0, out.getvalue())
        self.assertIn("PR watch", out.getvalue())
        self.assertEqual(len(self._watch_messages(lead["id"])), 1)

    # -- what an independent review found wrong with the first cut ---------

    def _no_appointment(self, *_a, **_k):
        raise AssertionError("no lead may be appointed for this event")

    def _appointing(self, appointed: list):
        def start_foreman(coordinator, project_id, *, herdr=True, request=None, ticket=None, **_kw):
            lead_task = coordinator.create_foreman_task(project_id, request=request, ticket=ticket)
            worker = coordinator.launch_worker(lead_task["id"], _IDLE, wait=False)
            self.addCleanup(self._stop, worker["id"])
            appointed.append(ticket)
            return {"task": lead_task, "worker": worker}
        return start_foreman

    def test_a_quiet_event_with_no_live_lead_starts_nobody(self) -> None:
        _project, task, _ = self._pr_task("quietnolead", lead=False)
        synced = self._pass(_payload(checks=[_check("build")]), at=time.time() + 130)
        self.assertEqual(len(synced["events"]), 1)
        with mock.patch.object(cli, "_start_foreman", self._no_appointment):
            (line,) = self._deliver(synced)
        self.assertIn("none appointed", line)
        self.assertIsNone(self.state.load()["tasks"][task["id"]]["delivery"]["watch"].get("outbox"))

    def test_a_merged_pr_never_gets_a_lead_appointed_even_with_comments(self) -> None:
        _project, task, _ = self._pr_task("mergednolead", lead=False)
        merged = _payload(
            state="MERGED", checks=[_check("build")], mergeCommit={"oid": "c" * 40},
            comments=[{"id": "C9", "author": {"login": "reviewer"}, "body": "late nit"}],
        )
        synced = self._pass(merged, at=time.time() + 130)
        self.assertEqual(self.state.load()["tasks"][task["id"]]["status"], "pr-merged")
        self.assertTrue(synced["events"][0]["actionable"])
        with mock.patch.object(cli, "_start_foreman", self._no_appointment):
            self._deliver(synced)

    def _age_appointments(self, seconds: float) -> None:
        data = self.state.load()
        limit = data["integrations"]["pr_watch"]
        limit["appointments"] = [
            time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - seconds))
            for _ in limit["appointments"]
        ]
        self.state.save(data)

    def _queued_pass(self, start: float, offset: float) -> dict:
        with mock.patch.object(Coordinator, "read_pull_request", side_effect=AssertionError("not due")):
            return self.coordinator.sync_open_pull_requests(now_epoch=start + offset)

    def test_appointments_are_budgeted_per_root_across_passes(self) -> None:
        tasks = [self._pr_task(f"cap{n}", ticket=f"TICKET-{n}", lead=False)[1] for n in (1, 2, 3)]
        appointed: list[str] = []
        start = time.time()
        synced = self._pass(_payload(checks=[_check("build", "FAILURE")]), at=start + 130)
        self.assertEqual(len(synced["events"]), 3)
        with mock.patch.object(cli, "_start_foreman", self._appointing(appointed)):
            lines = self._deliver(synced)
        self.assertEqual(len(appointed), 2)
        self.assertEqual(sum("waiting for a lead" in line for line in lines), 1)
        queued = [
            t["id"] for t in tasks
            if self.state.load()["tasks"][t["id"]]["delivery"]["watch"].get("outbox")
        ]
        self.assertEqual(len(queued), 1)
        # Later passes inside the window -- `pending --changes` runs one every
        # few seconds -- appoint nobody, and do not repeat the waiting line.
        for offset in (140, 160, 180):
            later = self._queued_pass(start, offset)
            self.assertEqual([e["task_id"] for e in later["events"]], queued)
            with mock.patch.object(cli, "_start_foreman", self._no_appointment):
                self.assertEqual(self._deliver(later), [])
        # Once the window has rolled past, the one that waited gets its lead.
        self._age_appointments(601)
        with mock.patch.object(cli, "_start_foreman", self._appointing(appointed)):
            self._deliver(self._queued_pass(start, 200))
        self.assertEqual(sorted(appointed), ["TICKET-1", "TICKET-2", "TICKET-3"])

    def test_a_failed_appointment_spends_the_budget_too(self) -> None:
        for n in (1, 2, 3):
            self._pr_task(f"fail{n}", ticket=f"TICKET-{n}", lead=False)
        attempts: list[str] = []

        def failing(*_a, ticket=None, **_k):
            attempts.append(ticket)
            raise HelmError("the runtime would not start")

        start = time.time()
        synced = self._pass(_payload(checks=[_check("build", "FAILURE")]), at=start + 130)
        with mock.patch.object(cli, "_start_foreman", failing):
            self._deliver(synced)
            self.assertEqual(len(attempts), 2, "a failing launch is not retried for every event")
            for offset in (140, 200, 240):
                self._deliver(self._queued_pass(start, offset))
        self.assertEqual(len(attempts), 2)

    def test_a_rate_limited_threads_read_backs_off_and_stops_the_pass(self) -> None:
        self._pr_task("threadsone", ticket="TICKET-1")
        self._pr_task("threadstwo", ticket="TICKET-2")
        start = time.time()
        limited = mock.Mock(returncode=1, stdout="", stderr="GraphQL: API rate limit exceeded")
        with mock.patch.object(Coordinator, "read_pull_request", return_value=_payload()) as read, \
                mock.patch("helm.coordinator.pull_requests.subprocess.run", return_value=limited):
            outcome = self.coordinator.sync_open_pull_requests(now_epoch=start + 130)
        self.assertEqual(read.call_count, 1)
        self.assertTrue(outcome["rate_limited"])
        self.assertTrue(self.state.load()["integrations"]["pr_watch"]["backoff_until"])

    def test_a_thread_that_talks_about_rate_limits_is_not_one(self) -> None:
        import json as _json

        def node(tid: str, body: str) -> dict:
            comment = {"nodes": [{"id": f"{tid}-c", "author": {"login": "reviewer"}, "body": body}]}
            return {"id": tid, "isResolved": False, "path": "a.py", "line": 3, "first": comment, "last": comment}

        answer = {"data": {"repository": {"pullRequest": {"reviewThreads": {"nodes": [
            node("T1", "This loop will hit the API rate limit exceeded path"),
            node("T2", "Handle RATE_LIMITED from the client"),
        ]}}}}}
        ok = mock.Mock(returncode=0, stdout=_json.dumps(answer), stderr="")
        with mock.patch("helm.coordinator.pull_requests.subprocess.run", return_value=ok):
            threads = self.coordinator.read_review_threads(URL, cwd=Path("."))
        self.assertEqual([t["id"] for t in threads], ["T1", "T2"])
        self.assertNotIn("backoff_until", (self.state.load().get("integrations") or {}).get("pr_watch") or {})
        # A failed call whose OUTPUT quotes such a comment is not a limit
        # either: only gh's error stream on a failure is read for one.
        failed = mock.Mock(returncode=1, stdout=_json.dumps(answer), stderr="HTTP 502: Bad Gateway")
        with mock.patch("helm.coordinator.pull_requests.subprocess.run", return_value=failed):
            self.assertIsNone(self.coordinator.read_review_threads(URL, cwd=Path(".")))
        self.assertNotIn("backoff_until", (self.state.load().get("integrations") or {}).get("pr_watch") or {})

    def test_a_rate_limit_inside_a_graphql_answer_backs_off(self) -> None:
        answer = mock.Mock(returncode=0, stdout='{"errors":[{"type":"RATE_LIMITED","message":"slow down"}]}', stderr="")
        with mock.patch("helm.coordinator.pull_requests.subprocess.run", return_value=answer):
            self.assertIsNone(self.coordinator.read_review_threads(URL, cwd=Path(".")))
        self.assertTrue(self.state.load()["integrations"]["pr_watch"]["backoff_until"])

    def test_two_passes_at_once_read_once_and_deliver_once(self) -> None:
        _project, _task, lead = self._pr_task("double")
        at = time.time() + 130
        red = _payload(checks=[_check("build", "FAILURE")])
        with mock.patch.object(Coordinator, "read_pull_request", return_value=red) as read, \
                mock.patch.object(Coordinator, "read_review_threads", return_value=[]):
            first = self.coordinator.sync_open_pull_requests(now_epoch=at)
            second = self.coordinator.sync_open_pull_requests(now_epoch=at)
        self.assertEqual(read.call_count, 1, "the second pass found the PR already claimed")
        self.assertEqual(second["watched"], [])
        # Both passes try to deliver the same queued event; one gets it.
        self._deliver(first)
        self._deliver(first)
        self.assertEqual(len(self._watch_messages(lead["id"])), 1)

    def test_an_event_another_pass_is_delivering_is_left_alone(self) -> None:
        _project, task, lead = self._pr_task("claimed")
        synced = self._pass(_payload(checks=[_check("build", "FAILURE")]), at=time.time() + 130)
        self.assertIsNotNone(self.coordinator.claim_pr_watch_event(task["id"]))
        self.assertEqual(self._deliver(synced), [])
        self.assertEqual(self._watch_messages(lead["id"]), [])

    def test_delivering_an_event_removes_only_what_it_carried(self) -> None:
        _project, task, lead = self._pr_task("partial")
        self._pass(_payload(checks=[_check("build", "FAILURE")]), [], at=time.time() + 130)
        claimed = self.coordinator.claim_pr_watch_event(task["id"])
        # A thread arrives while the first event is being delivered.
        self._pass(
            _payload(checks=[_check("build", "FAILURE")]),
            [_thread("T1", "reviewer", "one more thing")],
            at=time.time() + 260,
        )
        self.coordinator.mark_pr_watch_event(
            task["id"], claimed["change_ids"], lead_id=lead["id"], outcome="typed"
        )
        left = self.coordinator.pr_watch_event(task["id"])
        self.assertEqual(len(left["changes"]), 1)
        self.assertIn("one more thing", left["changes"][0])
        self.assertNotIn("CI failed", left["text"])

    def test_a_watch_that_began_as_an_error_note_starts_from_a_silent_baseline(self) -> None:
        _project, task, _lead = self._pr_task("errnote")
        data = self.state.load()
        data["tasks"][task["id"]]["delivery"].pop("watch")
        self.state.save(data)
        offline = HelmError("error connecting to api.github.com")
        with mock.patch.object(Coordinator, "read_pull_request", side_effect=offline):
            self.coordinator.sync_open_pull_requests(now_epoch=time.time() + 130)
        self.coordinator.record_pr_status(task["id"], state="open", url=URL)
        self.assertFalse(self.state.load()["tasks"][task["id"]]["delivery"]["watch"].get("registered"))
        synced = self._pass(_payload(checks=[_check("build", "FAILURE")]), at=time.time() + 400)
        self.assertEqual(synced["events"], [])

    def test_owner_and_name_reach_gh_as_literal_strings(self) -> None:
        completed = mock.Mock(returncode=0, stdout="{}")
        with mock.patch("helm.coordinator.pull_requests.subprocess.run", return_value=completed) as run:
            self.coordinator.read_review_threads("https://github.com/@owner/@repo/pull/12", cwd=Path("."))
        command = run.call_args[0][0]
        self.assertEqual(command[command.index("owner=@owner") - 1], "-f")
        self.assertEqual(command[command.index("name=@repo") - 1], "-f")
        self.assertEqual(command[command.index("number=12") - 1], "-F")

    def test_a_url_that_is_not_one_never_reaches_gh(self) -> None:
        with mock.patch("helm.coordinator.pull_requests.subprocess.run") as run:
            for bad in ("--repo=evil", "-x", "not a url"):
                with self.assertRaises(HelmError):
                    self.coordinator.read_pull_request(bad, cwd=Path("."))
        run.assert_not_called()
        completed = mock.Mock(returncode=0, stdout="{}")
        with mock.patch("helm.coordinator.pull_requests.subprocess.run", return_value=completed) as run:
            self.coordinator.read_pull_request(URL, cwd=Path("."))
        self.assertEqual(run.call_args[0][0][-2:], ["--", URL])

    def test_a_rate_limit_stops_the_pass_and_backs_off(self) -> None:
        self._pr_task("limitone", ticket="TICKET-1")
        self._pr_task("limittwo", ticket="TICKET-2")
        start = time.time()
        limited = HelmError("GraphQL: API rate limit exceeded for user ID 1.")
        with mock.patch.object(Coordinator, "read_pull_request", side_effect=limited) as read:
            outcome = self.coordinator.sync_open_pull_requests(now_epoch=start + 130)
            self.assertEqual(read.call_count, 1, "one rate-limited read stops the pass")
            self.assertTrue(outcome["rate_limited"])
            self.assertEqual(len(outcome["skipped"]), 2)
            self.coordinator.sync_open_pull_requests(now_epoch=start + 400)
            self.assertEqual(read.call_count, 1, "nothing is read while backing off")
        with mock.patch.object(Coordinator, "read_pull_request", return_value=_payload()) as read, \
                mock.patch.object(Coordinator, "read_review_threads", return_value=[]):
            self.coordinator.sync_open_pull_requests(now_epoch=start + 1200)
            self.assertEqual(read.call_count, 2)
