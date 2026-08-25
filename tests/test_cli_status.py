"""CLI surfaces: status, watch, board, action items and project colour."""

from __future__ import annotations

import contextlib
import datetime as _dt
import io
import os
import json
import shutil
import signal
import subprocess
import sys
from pathlib import Path
from unittest import mock

from helm import cli
from helm.core import (
    project_glyph,
    _COLOR_PALETTE,
    Coordinator,
    StateStore,
)
from helm.herdr import HerdrAdapter

from tests.support import FakeHerdr, HelmTestCase, REPO_ROOT, SHIPPED_DOMAINS


class CliStatusTests(HelmTestCase):
    def test_project_color_is_stable_across_store_instances(self) -> None:
        root = self.repo("colors")
        first = self.coordinator.register_project("Colors", str(root), project_id="colors")
        second = Coordinator(StateStore(self.state.directory)).list_projects()[0]
        self.assertEqual(first["color"], second["color"])
        self.assertIn("#", first["color"])

    def test_an_unanswered_question_is_surfaced_as_needing_attention(self) -> None:
        root = self.repo("asking")
        project = self.coordinator.register_project("Ask", str(root), project_id="asking")
        task = self.coordinator.create_task(project["id"], "ask and wait")
        worker = self.coordinator.launch_worker(
            task["id"], [sys.executable, "-c", ""], wait=False
        )
        self.coordinator.record_worker_message(worker["id"], "status", "working")
        # Reporting normally, so every other signal says healthy. Asking is
        # what makes it blocked, and that has to be visible on its own.
        self.coordinator.record_worker_message(worker["id"], "question", "which option?")
        health = {e["worker_id"]: e for e in self.coordinator.worker_health()}
        self.assertEqual(health[worker["id"]]["verdict"], "awaiting-answer")
        self.assertIn("waiting", health[worker["id"]]["detail"])

        self.coordinator.record_worker_message(worker["id"], "answer", "the second one")
        answered = {e["worker_id"]: e for e in self.coordinator.worker_health()}
        self.assertNotEqual(answered[worker["id"]]["verdict"], "awaiting-answer")

        # Asking again after being answered blocks it again.
        self.coordinator.record_worker_message(worker["id"], "question", "and now?")
        again = {e["worker_id"]: e for e in self.coordinator.worker_health()}
        self.assertEqual(again[worker["id"]]["verdict"], "awaiting-answer")

    def test_needs_you_marks_an_orphaned_escalation_as_unverified(self) -> None:
        """A diagnostic entry -- its worker or task record is missing -- must
        read differently from an ordinary, verified escalation: printing it
        the same way would claim a liveness check that never happened.
        """
        root = self.repo("orphan-cli")
        project = self.coordinator.register_project("OrphanCli", str(root), project_id="orphan-cli")
        task = self.coordinator.create_task(project["id"], "ask and vanish")
        worker = self.coordinator.launch_worker(
            task["id"], [sys.executable, "-c", ""], wait=False
        )
        self.coordinator.record_worker_message(worker["id"], "question", "which option?")
        with self.coordinator.store.locked() as data:
            del data["workers"][worker["id"]]

        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            cli._print_status(self.coordinator, None)
        printed = output.getvalue()
        self.assertIn("Needs you (1)", printed)
        self.assertIn("UNVERIFIED", printed)
        self.assertIn(worker["id"], printed)
        # The line stays concise -- one row, no dumped history or invented
        # project/task facts beyond what the message itself already recorded.
        self.assertEqual(
            sum(1 for line in printed.splitlines() if worker["id"] in line), 1
        )

    def test_all_palette_colours_map_to_unique_nonempty_glyphs(self) -> None:
        # Several projects report into one session and a line without its
        # project is ambiguous, so the glyph is the separator. The palette held
        # eight colours that collapsed to five squares -- three blue, two
        # orange -- so two projects could differ in colour and still print the
        # same glyph, which defeats the whole point of having one.
        for color in _COLOR_PALETTE:
            self.assertTrue(project_glyph(color))
        self.assertEqual(
            len({project_glyph(color) for color in _COLOR_PALETTE}),
            len(_COLOR_PALETTE),
            "every palette colour must map to its own glyph",
        )

    def test_fourteen_registered_projects_get_unique_glyphs_and_the_fifteenth_reuses_stably(
        self,
    ) -> None:
        # The product requirement is a fixed number -- at least 14 concurrently
        # registered projects get a distinct glyph before any project has to
        # reuse one -- so this proves it against that literal number through
        # the public registration path, not against whatever `_COLOR_PALETTE`
        # happens to hold today.
        glyphs = {}
        for index in range(14):
            name = f"glyph-{index}"
            project = self.coordinator.register_project(
                name, str(self.repo(name)), project_id=name
            )
            glyphs[name] = project_glyph(project["color"])
        self.assertEqual(len(glyphs), 14)
        self.assertEqual(
            len(set(glyphs.values())), 14, f"glyph collision among first 14: {glyphs}"
        )

        # Reload before registering the 15th: the allocator must see the same
        # claimed colours a fresh process would load from disk, not whatever a
        # single in-memory Coordinator happens to still be holding.
        reloaded = Coordinator(StateStore(self.state.directory))
        fifteenth = reloaded.register_project(
            "glyph-14", str(self.repo("glyph-14")), project_id="glyph-14"
        )
        fifteenth_glyph = project_glyph(fifteenth["color"])
        self.assertIn(
            fifteenth_glyph,
            set(glyphs.values()),
            "the 15th project must reuse one of the 14 glyphs already in use",
        )

        # Reload again and confirm the reused glyph did not drift -- the
        # colour a human already learned to recognise for this project keeps
        # meaning this project.
        reloaded_again = Coordinator(StateStore(self.state.directory))
        stored = next(
            p for p in reloaded_again.list_projects() if p["id"] == "glyph-14"
        )
        self.assertEqual(stored["color"], fifteenth["color"])
        self.assertEqual(project_glyph(stored["color"]), fifteenth_glyph)

    def test_legacy_palette_colours_keep_their_exact_square(self) -> None:
        # These seven colours and their squares predate the wider palette.
        # Existing project records on disk store only the hex colour, so if
        # the mapping from one of these colours to its glyph ever shifted,
        # every already-registered project using it would silently change
        # glyph underneath a human who has learned to recognise it.
        legacy_squares = {
            "#2563eb": "\N{LARGE BLUE SQUARE}",
            "#7c3aed": "\N{LARGE PURPLE SQUARE}",
            "#c2410c": "\N{LARGE ORANGE SQUARE}",
            "#4d7c0f": "\N{LARGE GREEN SQUARE}",
            "#be123c": "\N{LARGE RED SQUARE}",
            "#eab308": "\N{LARGE YELLOW SQUARE}",
            "#92400e": "\N{LARGE BROWN SQUARE}",
        }
        for color, glyph in legacy_squares.items():
            self.assertEqual(project_glyph(color), glyph)

    def test_uppercase_palette_hex_and_circle_glyph_survive_established_consumers(
        self,
    ) -> None:
        from helm.herdr import _paint_command

        # A palette lookup must not be case-sensitive: a colour is free-form
        # user/config input in practice, and the exact-match lookup added to
        # widen the palette keys off the lower-cased hex digits.
        lower_color = "#3b82f6"
        upper_color = "#3B82F6"
        circle_glyph = project_glyph(lower_color)
        self.assertTrue(circle_glyph)
        self.assertEqual(project_glyph(upper_color), circle_glyph)
        # It is really a distinct glyph from the legacy square of the same
        # hue, not a coincidental match through the generic hue fallback.
        self.assertNotEqual(circle_glyph, project_glyph("#2563eb"))

        # A glyph is a character, so unlike an escape sequence it must survive
        # the same established output paths a square glyph always has. Drive
        # every consumer with the uppercase form specifically, since that is
        # the case a bare hue-bucket lookup would fail to normalise.
        command = _paint_command(upper_color, "status: harvest done")
        self.assertIn(circle_glyph, command)
        self.assertNotIn("\x1b", command)

        label = cli._project_label({"id": "media", "name": "Media", "color": upper_color})
        self.assertIn(circle_glyph, label)

        project = self.coordinator.register_project(
            "circle-project", str(self.repo("circle-project")), project_id="circle-project",
            color=upper_color,
        )
        task = self.coordinator.create_task(project["id"], "the work", no_domain=True)
        herdr = FakeHerdr()
        adapter = HerdrAdapter(self.coordinator, herdr)
        adapter.launch_task(task["id"], [sys.executable, "-c", ""], wait=False)
        self.assertIn(circle_glyph, herdr.workspaces[0][1])

    def test_custom_colour_keeps_generic_hue_fallback(self) -> None:
        # A colour outside the built-in palette -- set explicitly, or hashed
        # for a project registered before the palette grew -- must keep
        # degrading through the hue bucket, not require a palette entry.
        project = self.coordinator.register_project(
            "custom-colour", str(self.repo("custom-colour")), project_id="custom-colour",
            color="#0369a1",
        )
        self.assertEqual(project["color"], "#0369a1")
        self.assertEqual(project_glyph(project["color"]), "\N{LARGE BLUE SQUARE}")
        self.assertEqual(project_glyph("nonsense"), "")

    def test_project_glyphs_are_stable_across_a_state_reload(self) -> None:
        names = [f"stable-{index}" for index in range(16)]
        colors = {}
        for name in names:
            project = self.coordinator.register_project(
                name, str(self.repo(name)), project_id=name
            )
            colors[name] = project["color"]

        reloaded = Coordinator(StateStore(self.state.directory))
        by_id = {p["id"]: p for p in reloaded.list_projects()}
        for name in names:
            self.assertEqual(by_id[name]["color"], colors[name])
            self.assertEqual(
                project_glyph(by_id[name]["color"]), project_glyph(colors[name])
            )

    def test_watch_surfaces_new_project_updates_once(self) -> None:
        root = self.repo("surface")
        project = self.coordinator.register_project("Surface", str(root), project_id="surface")
        self.coordinator.record_situation(
            project["id"], "Foreman report: task t-example [completed] ready for merge decision"
        )

        first = self.coordinator.project_updates_for_watch()
        self.assertEqual(len(first), 1)
        self.assertEqual(first[0]["project_id"], project["id"])
        self.assertIn("ready for merge decision", first[0]["text"])
        self.assertEqual(self.coordinator.project_updates_for_watch(), [])

        self.coordinator.record_situation(project["id"], "Review loop: reviewer approved")
        second = self.coordinator.project_updates_for_watch(mark_seen=False)
        self.assertEqual(len(second), 1)
        # mark_seen=False is useful for previews and must not consume it.
        self.assertEqual(len(self.coordinator.project_updates_for_watch()), 1)

    def test_follow_up_summary_becomes_project_action_item(self) -> None:
        root = self.repo("followup")
        project = self.coordinator.register_project("Follow", str(root), project_id="followup")
        task = self.coordinator.create_task(project["id"], "change the code")

        self.coordinator.record_task_progress_summary(
            task["id"],
            "rotation watcher follow-up needed if it must stay warm across every token refresh",
            source="Review loop",
        )

        status = self.coordinator.project_status(project["id"])
        self.assertEqual(len(status["action_items"]), 1)
        self.assertIn("follow-up needed", status["action_items"][0]["text"])
        updates = self.coordinator.project_updates_for_watch()
        self.assertTrue(any("ACTION REQUIRED" in update["text"] for update in updates))
        self.assertEqual(self.coordinator.project_updates_for_watch(), [])

    def test_a_failed_task_asks_the_commander_for_a_decision(self) -> None:
        """A failure has to reach somebody without being phrased just so.

        A blocker is listed by `open_escalations`; a failure was listed
        nowhere. It raised no action item, no situation line and no
        escalation, and appeared only under a heading inside `helm project
        status` that nothing points at -- so a task that died read exactly
        like a task nobody had started. And deriving the follow-up from marker
        words in the worker's prose is a denylist: the failure nobody worded
        conveniently is the one that disappears.
        """
        root = self.repo("failing")
        project = self.coordinator.register_project(
            "Failing", str(root), project_id="failing"
        )
        task = self.coordinator.create_task(project["id"], "do the thing")
        worker = self.coordinator.launch_worker(
            task["id"], [sys.executable, "-c", ""], wait=False
        )
        # Deliberately plain prose: no "follow-up", no "action required", no
        # payload flag -- nothing for a keyword match to catch.
        self.coordinator.record_worker_message(
            worker["id"], "failure", "the build broke and I cannot fix it"
        )

        items = self.coordinator.open_action_items()
        self.assertTrue(
            any("the build broke" in item["text"] for item in items),
            f"a failed task raised no action item: {items}",
        )
        situation = self.coordinator.project_status(project["id"])["situation"]
        self.assertTrue(any("[failed]" in entry["text"] for entry in situation))
        # And it is owed to the commander, not filed as routine progress.
        surfaced = self.coordinator.project_updates_for_watch()
        self.assertTrue(any("the build broke" in u["text"] for u in surfaced))

    def test_a_session_that_dies_without_reporting_still_raises_the_decision(self) -> None:
        """The larger half of failures never reported anything at all.

        A worker that sends a `failure` goes through the event path. A worker
        whose session simply ends -- killed, non-zero exit, lost -- is settled
        by observation through an internal message that path never sees, so it
        raised nothing: no item, no escalation. Both routes reach `failed`, so
        both must reach the commander, and the decision is derived rather than
        recorded at failure time so neither route can be forgotten.
        """
        root = self.repo("dying")
        project = self.coordinator.register_project(
            "Dying", str(root), project_id="dying"
        )
        task = self.coordinator.create_task(project["id"], "a task whose session dies")
        self.coordinator.launch_worker(
            task["id"], [sys.executable, "-c", "import sys; sys.exit(3)"], wait=True
        )
        self.assertEqual(
            self.coordinator.store.load()["tasks"][task["id"]]["status"], "failed"
        )

        items = self.coordinator.open_action_items()
        self.assertTrue(
            any(task["id"] in item["text"] for item in items),
            f"a session that died without reporting raised nothing: {items}",
        )
        # Derived, so asking twice does not accumulate duplicates.
        self.assertEqual(
            len(self.coordinator.open_action_items()), len(items)
        )

    def test_a_replaced_foremans_blocker_stops_being_owed(self) -> None:
        """Appointing a replacement IS the answer to a foreman's escalation.

        The blocked record stays as evidence, but presenting it as still
        needing a human turns the attention list into a list of things
        already dealt with — and a reader who skims a stale list misses the
        entries that are real.
        """
        import sys
        root = self.repo("supersededforeman")
        project = self.coordinator.register_project(
            "Superseded", str(root), project_id="supersededforeman"
        )
        first = self.coordinator.create_task(
            project["id"], "drive the project", role="foreman"
        )
        worker = self.coordinator.launch_worker(
            first["id"], [sys.executable, "-c", ""], wait=False
        )
        self.coordinator.record_worker_message(
            worker["id"], "blocker", "cannot proceed without a decision"
        )
        owed = [
            u for u in self.coordinator.project_updates_for_watch()
            if "cannot proceed" in u["text"]
        ]
        self.assertTrue(owed, "a foreman blocker should be owed while it stands")

        # The replacement is the answer.
        self.coordinator.create_task(
            project["id"], "drive the project", role="foreman"
        )

        still_owed = [
            u for u in self.coordinator.project_updates_for_watch()
            if "cannot proceed" in u["text"]
        ]
        self.assertEqual(still_owed, [])

    def test_an_owed_report_survives_a_read_that_relayed_nothing(self) -> None:
        """Surfaced meant "something read it", which is the wrong property.

        The reader is an agent, and an agent's output reaches a commander only
        if the agent passes it on. A worker result was permanently consumed by
        a read that piped it through a filter and dropped it, leaving the
        record claiming the commander had been told. Two commands draining one
        queue made it worse: whichever ran first won and the other showed an
        empty section.

        So an owed report returns until it is acknowledged. Routine lines keep
        show-once, because repeating those is the noise that teaches a reader
        to skip the section entirely.
        """
        root = self.repo("owed-ack")
        project = self.coordinator.register_project(
            "Owed", str(root), project_id="owed-ack"
        )
        self.coordinator.record_situation(
            project["id"], "Worker result: the change is finished", surface=True
        )
        self.coordinator.record_situation(project["id"], "routine progress push")

        first = self.coordinator.project_updates_for_watch()
        self.assertTrue(any("the change is finished" in u["text"] for u in first))
        self.assertTrue(any("routine progress" in u["text"] for u in first))

        second = self.coordinator.project_updates_for_watch()
        self.assertTrue(
            any("the change is finished" in u["text"] for u in second),
            "an owed report vanished after a read that relayed nothing",
        )
        self.assertFalse(any("routine progress" in u["text"] for u in second))

        acknowledged = self.coordinator.acknowledge_updates(project["id"])
        self.assertEqual(len(acknowledged), 1)
        # Who relayed it is recorded, so "an agent read it" and "the commander
        # was told" stop being the same claim.
        self.assertTrue(acknowledged[0]["acknowledged_by"])
        self.assertEqual(self.coordinator.project_updates_for_watch(), [])

    def test_a_confirmation_gate_is_not_filed_among_the_follow_ups(self) -> None:
        """A gate holding a foreman still outranks a note about later.

        Delivery and cleanup decisions trail finished work and can wait. A
        proposed requirement or solution gate cannot: the foreman is stopped
        until it is answered. Sorted into the same list they read alike, and a
        long list is one the reader skips -- which is how a gate blocking live
        work went unanswered while its project looked merely quiet.
        """
        root = self.repo("gated")
        project = self.coordinator.register_project("Gated", str(root), project_id="gated")
        # A delivery decision, so the two kinds are present together.
        done = self.coordinator.create_task(project["id"], "write the change")
        worker = self.coordinator.launch_worker(
            done["id"], [sys.executable, "-c", ""], wait=False
        )
        self.coordinator.record_worker_message(worker["id"], "result", "done")
        foreman = self.coordinator.create_task(
            project["id"], "drive this project", role="foreman"
        )
        self.coordinator.propose_gate(
            foreman["id"], "requirement", "ship the thing, excluding the other thing"
        )

        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            cli._print_status(self.coordinator, None)
        printed = output.getvalue()

        self.assertIn("Waiting on your decision (1):", printed)
        gates = printed.split("Waiting on your decision (1):")[1].split("\n\n")[0]
        self.assertIn(foreman["id"], gates)
        self.assertNotIn("Delivery decision needed", gates)
        # And it is gone from the follow-ups rather than printed in both.
        follow_ups = printed.split("Decisions and follow-ups")[1].split("\n\n")[0]
        self.assertNotIn(foreman["id"], follow_ups)

    def test_status_and_watch_put_the_pending_decision_in_front_of_a_reader(self) -> None:
        root = self.repo("surfaced")
        project = self.coordinator.register_project(
            "Surfaced", str(root), project_id="surfaced"
        )
        task = self.coordinator.create_task(project["id"], "write the change")
        worker = self.coordinator.launch_worker(
            task["id"], [sys.executable, "-c", ""], wait=False
        )
        self.coordinator.record_worker_message(worker["id"], "result", "done")

        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            cli._print_status(self.coordinator, None)
        printed = output.getvalue()
        self.assertIn("Decisions and follow-ups (1)", printed)
        self.assertIn("Delivery decision needed", printed)
        self.assertIn(task["id"], printed)

        # A gate keeps showing until somebody answers it. Surfacing it once and
        # then hiding it is how finished work stops being anybody's problem.
        first = self.coordinator.project_updates_for_watch()
        self.assertTrue(any("DECISION REQUIRED" in u["text"] for u in first))
        second = self.coordinator.project_updates_for_watch()
        self.assertTrue(any("DECISION REQUIRED" in u["text"] for u in second))

        Path(worker["exit_file"]).write_text(
            json.dumps({"returncode": 0}) + "\n", encoding="utf-8"
        )
        self.coordinator.cleanup_task(task["id"])
        # Answering the gate clears the gate. The worker's own result is an
        # owed report and outlives it deliberately: it stops appearing when it
        # has been relayed, which is a separate act from cleaning up the task
        # it describes.
        self.coordinator.acknowledge_updates(project["id"])
        self.assertEqual(self.coordinator.project_updates_for_watch(), [])
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            cli._print_status(self.coordinator, None)
        self.assertNotIn("Decisions and follow-ups", output.getvalue())

    def test_project_action_command_records_commander_visible_item(self) -> None:
        helm_root = self._helm_root("action-root")
        project_root = self.repo("action-project")
        destination = helm_root / "projects" / "action"
        shutil.move(str(project_root), str(destination))
        coordinator = Coordinator(StateStore(helm_root / "state", helm_root=helm_root))
        project = coordinator.discover_project(helm_root, "action")

        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            self.assertEqual(
                cli.main([
                    "--root",
                    str(helm_root),
                    "project",
                    "action",
                    project["id"],
                    "decide whether to open a follow-up task",
                    "--source",
                    "review",
                ]),
                0,
            )
        self.assertIn("Recorded action", output.getvalue())

        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            self.assertEqual(cli.main(["--root", str(helm_root), "project", "status", project["id"]]), 0)
        text = output.getvalue()
        self.assertIn("action items:", text)
        self.assertIn("decide whether to open a follow-up task", text)

    def test_watch_surfaces_latest_project_updates_and_consumes_backlog(self) -> None:
        root = self.repo("surface-backlog")
        project = self.coordinator.register_project(
            "Surface Backlog", str(root), project_id="surface-backlog"
        )
        for index in range(5):
            self.coordinator.record_situation(project["id"], f"round {index} summary")

        first = self.coordinator.project_updates_for_watch(limit_per_project=2)
        self.assertEqual(len(first), 3)
        self.assertIn("3 older project update", first[0]["text"])
        self.assertEqual([entry["text"] for entry in first[1:]], ["round 3 summary", "round 4 summary"])
        self.assertEqual(self.coordinator.project_updates_for_watch(), [])

    def test_watch_prints_project_updates_from_quiet_foreman(self) -> None:
        helm_root = self._helm_root("watch-updates")
        project_root = self.repo("watch-project")
        destination = helm_root / "projects" / "surface"
        shutil.move(str(project_root), str(destination))
        coordinator = Coordinator(StateStore(helm_root / "state", helm_root=helm_root))
        project = coordinator.discover_project(helm_root, "surface")
        coordinator.record_situation(
            project["id"], "Foreman report: task t-example [completed] ready for merge decision"
        )

        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            self.assertEqual(cli.main(["--root", str(helm_root), "watch"]), 0)
        text = output.getvalue()
        self.assertIn("Project updates:", text)
        self.assertIn("ready for merge decision", text)

        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            self.assertEqual(cli.main(["--root", str(helm_root), "watch"]), 0)
        self.assertNotIn("ready for merge decision", output.getvalue())

    def test_status_surfaces_a_foreman_report_the_root_has_not_seen(self) -> None:
        """A push nobody read is indistinguishable from a project with nothing to say.

        `watch` already bridged the record into the root's session, but the
        root rarely runs it, so relaying a finished investigation depended on
        the coordinator remembering to look -- and failed silently when it
        did not. `status` is what the root actually runs, so it has to say
        what arrived since last time.
        """
        helm_root = self._helm_root("status-updates")
        destination = helm_root / "projects" / "surfaced"
        shutil.move(str(self.repo("status-project")), str(destination))
        coordinator = Coordinator(StateStore(helm_root / "state", helm_root=helm_root))
        project = coordinator.discover_project(helm_root, "surfaced")
        coordinator.record_situation(
            project["id"], "Foreman report: investigation COMPLETE, four decisions outstanding"
        )

        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            self.assertEqual(cli.main(["--root", str(helm_root), "status"]), 0)
        text = output.getvalue()
        self.assertIn("New since you last looked:", text)
        self.assertIn("four decisions outstanding", text)

        # Shown once. Repeating it forever trains the reader to skip the
        # section, which is the failure this exists to prevent.
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            self.assertEqual(cli.main(["--root", str(helm_root), "status"]), 0)
        self.assertNotIn("four decisions outstanding", output.getvalue())

    def test_every_terminal_report_survives_the_per_project_limit(self) -> None:
        """A result the commander never saw must not be marked seen.

        The limit exists so a long-quiet root gets the current state instead of
        a transcript, and for routine progress that is right. Applied to
        results it is not: a project that reported five outcomes in a row had
        the older ones counted into "marked surfaced" and printed nowhere, so
        the record was complete and the commander still heard nothing.
        """
        helm_root = self._helm_root("status-owed")
        destination = helm_root / "projects" / "owed"
        shutil.move(str(self.repo("owed-project")), str(destination))
        coordinator = Coordinator(StateStore(helm_root / "state", helm_root=helm_root))
        project = coordinator.discover_project(helm_root, "owed")
        for index in range(5):
            coordinator.record_situation(
                project["id"], f"Worker result: round {index} DRY", surface=True
            )
        coordinator.record_situation(project["id"], "routine progress push")

        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            self.assertEqual(cli.main(["--root", str(helm_root), "status"]), 0)
        text = output.getvalue()
        for index in range(5):
            self.assertIn(f"round {index} DRY", text)

    def test_status_does_not_echo_decisions_it_already_lists_in_full(self) -> None:
        """The new section must not bury the news under duplicates.

        Delivery and cleanup decisions are printed below in full, with their
        task ids. Echoing them here pushed the one genuinely new line off the
        top of the screen behind twelve copies of the same decision.
        """
        helm_root = self._helm_root("status-updates-dedup")
        destination = helm_root / "projects" / "deduped"
        shutil.move(str(self.repo("dedup-project")), str(destination))
        coordinator = Coordinator(StateStore(helm_root / "state", helm_root=helm_root))
        project = coordinator.discover_project(helm_root, "deduped")
        coordinator.record_situation(
            project["id"], "DECISION REQUIRED: Delivery decision needed: read this task's result"
        )
        coordinator.record_situation(project["id"], "Foreman report: the actual news")

        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            self.assertEqual(cli.main(["--root", str(helm_root), "status"]), 0)
        text = output.getvalue()
        surfaced = text.split("New since you last looked:")[1].split("\n\n")[0]
        self.assertIn("the actual news", surfaced)
        self.assertNotIn("DECISION REQUIRED", surfaced)

    def test_the_board_shows_what_a_task_produced_without_merging_it(self) -> None:
        root = self.repo("boarding")
        project = self.coordinator.register_project("Board", str(root), project_id="boarding")
        task = self.coordinator.create_task(project["id"], "make something visible")
        code = (
            "from pathlib import Path; import subprocess; "
            "Path('out.txt').write_text('made'); "
            "subprocess.run(['git','add','out.txt'],check=True); "
            "subprocess.run(['git','commit','-m','made it'],check=True)"
        )
        worker = self.coordinator.launch_worker(task["id"], [sys.executable, "-c", code])
        entries = {p["id"]: p for p in self.coordinator.board()}
        card = entries["boarding"]["tasks"][0]
        # The work is on a branch nobody has merged; the board shows it anyway.
        self.assertEqual(card["id"], task["id"])
        self.assertTrue(any("out.txt" in line for line in card["diffstat"]))
        self.assertIn(card["tone"], {"amber", "green", "red", "grey"})
        self.assertTrue(entries["boarding"]["glyph"])
        # Rendering escapes task text rather than injecting it into the page.
        html = cli._board_html(self.coordinator.board(), "now")
        self.assertIn("Helm board", html)
        self.assertNotIn("<script", html)

    def test_project_status_outlives_the_pane_and_does_not_grow(self) -> None:
        root = self.repo("statusrec")
        project = self.coordinator.register_project("St", str(root), project_id="statusrec")
        task = self.coordinator.create_task(project["id"], "fail with a reason")
        worker = self.coordinator.launch_worker(
            task["id"], [sys.executable, "-c", ""], wait=False
        )
        Path(worker["log_file"]).write_text(
            "starting\nAPI Error: Connection closed mid-response\n", encoding="utf-8"
        )
        self.coordinator.record_worker_message(worker["id"], "blocker", "needs a credential")
        self.coordinator.settle_reported_worker(worker["id"])

        status = self.coordinator.project_status(project["id"])
        kept = status["evidence"][0]
        # The diagnosis survives without the pane: signature, tail and the
        # worker's own words are all on disk.
        self.assertEqual(kept["task_id"], task["id"])
        self.assertTrue(any("API Error" in line for line in kept["signatures"]))
        self.assertTrue(any("credential" in m for m in kept["messages"]))
        self.assertEqual(kept["branch"], task["branch"])

        # The situation log is the only growing part, so it is the only capped
        # part: beyond the limit, older entries roll into history the status
        # view does not load.
        for index in range(self.coordinator.SITUATION_KEPT + 4):
            self.coordinator.record_situation(project["id"], f"decision {index}")
        grown = self.coordinator.project_status(project["id"])
        self.assertEqual(len(grown["situation"]), self.coordinator.SITUATION_KEPT)
        self.assertEqual(grown["history_entries"], 4)
        self.assertTrue(grown["situation"][-1]["text"].endswith("15"))

    def test_each_project_carries_a_colour_glyph_everywhere(self) -> None:
        from helm.core import project_glyph
        from helm.herdr import _paint_command

        # Distinct palette colours map to distinct squares.
        self.assertEqual(project_glyph("#0369a1"), "\U0001F7E6")
        self.assertEqual(project_glyph("#4d7c0f"), "\U0001F7E9")
        self.assertEqual(project_glyph("#7c3aed"), "\U0001F7EA")
        self.assertEqual(project_glyph("#be123c"), "\U0001F7E5")
        self.assertNotEqual(project_glyph("#0369a1"), project_glyph("#b45309"))
        # A malformed colour degrades to no glyph rather than raising.
        self.assertEqual(project_glyph("nonsense"), "")

        # A glyph is a character, so unlike an escape sequence it survives the
        # trip into a pane.
        command = _paint_command("#0369a1", "status: harvest done")
        self.assertIn("\U0001F7E6", command)
        self.assertNotIn("\x1b", command)

        # And it survives piped output, where the background tint is dropped.
        label = cli._project_label({"id": "media", "name": "Media", "color": "#0369a1"})
        self.assertIn("\U0001F7E6", label)
        self.assertEqual(cli._project_paint({"color": "#0369a1"}, label, stream=io.StringIO()), label)

    def test_each_project_reports_in_its_own_colour(self) -> None:
        first = {"id": "first", "name": "First", "color": "#0369a1"}
        second = {"id": "second", "name": "Second", "color": "#b45309"}

        class FakeTerminal(io.StringIO):
            def isatty(self) -> bool:
                return True

        terminal = FakeTerminal()
        # Asserting that colour *happens* means asking for it. A session that
        # sets NO_COLOR -- as an agent harness reasonably does -- would
        # otherwise fail this test for doing exactly what the last assertions
        # here require. So the opt-outs `_color_enabled` honours are cleared
        # for the half that needs colour, and only for that half.
        with mock.patch.dict(os.environ):
            os.environ.pop("NO_COLOR", None)
            os.environ.pop("HELM_NO_COLOR", None)
            painted_first = cli._project_paint(first, "status: harvest done", stream=terminal)
            painted_second = cli._project_paint(second, "status: harvest done", stream=terminal)
            # Same text, different projects, visibly different lines.
            self.assertNotEqual(painted_first, painted_second)
            self.assertIn("48;2;3;105;161", painted_first)
            self.assertIn("48;2;180;83;9", painted_second)
            self.assertTrue(painted_first.endswith("\033[0m"))
            # The label still carries identity, so colour is never the only channel.
            self.assertIn("harvest done", painted_first)

            # A light project colour gets dark text rather than unreadable white.
            light = cli._project_paint(
                {"id": "l", "name": "L", "color": "#f5f5f5"}, "x", stream=terminal
            )
            self.assertIn(";30m", light)

            # And piped output is never corrupted, opt-out or not.
            self.assertEqual(
                cli._project_paint(first, "plain", stream=io.StringIO()), "plain"
            )

        # Never defy an opt-out, whichever of the two the caller set.
        for variable in ("NO_COLOR", "HELM_NO_COLOR"):
            with mock.patch.dict(os.environ, {variable: "1"}):
                self.assertEqual(
                    cli._project_paint(first, "plain", stream=terminal), "plain"
                )

    def test_the_watchdog_stays_quiet_until_the_pending_list_changes(self) -> None:
        """A notifier that repeats itself is one the reader learns to ignore.

        The pending list carries elapsed times -- "quiet for 1137s" -- which
        differ on every run, so hashing it raw made an unchanged backlog look
        like fresh news every interval. The fingerprint therefore tracks WHICH
        items are waiting, not how long they have been waiting.
        """
        from helm import watchdog

        first = "HELM NEEDS A HUMAN (2):\n  a w-1 [quiet]: no message for 30s"
        later = "HELM NEEDS A HUMAN (2):\n  a w-1 [quiet]: no message for 9000s"
        self.assertEqual(watchdog._fingerprint(first), watchdog._fingerprint(later))

        changed = "HELM NEEDS A HUMAN (3):\n  a w-1 [quiet]: no message for 30s\n  b GATE"
        self.assertNotEqual(watchdog._fingerprint(first), watchdog._fingerprint(changed))

    def test_the_watchdog_says_so_rather_than_pretending_on_an_unknown_platform(
        self,
    ) -> None:
        """A fresh clone on Windows must not be told a scheduler was installed."""
        from unittest import mock

        from helm import watchdog

        output = io.StringIO()
        with mock.patch("helm.watchdog.platform.system", return_value="Plan9"):
            with contextlib.redirect_stdout(output):
                code = watchdog.install(Path(self.temp.name), 900)
        printed = output.getvalue()
        self.assertEqual(code, 1)
        self.assertIn("No scheduler integration", printed)
        # And it hands over the command to run by hand instead.
        self.assertIn("watchdog run", printed)

    def test_pending_changes_prints_only_what_is_new(self) -> None:
        """An unattended watch must not re-print what its reader already saw.

        Re-printing the whole list on every poll buries the one new line under
        things already read, and trains the reader to skip it — the same
        failure the list itself exists to fix. And the comparison ignores
        digits, because a line carries elapsed time ("quiet for 7481s") that
        differs on every poll: without that, every health line reads as new,
        forever.
        """
        helm_root = self._helm_root("changes-root")
        destination = helm_root / "projects" / "changes"
        shutil.move(str(self.repo("changes")), str(destination))
        coordinator = Coordinator(StateStore(helm_root / "state", helm_root=helm_root))
        project = coordinator.discover_project(helm_root, "changes")
        coordinator.record_situation(
            project["id"], "Worker result: the first thing", surface=True
        )

        first = io.StringIO()
        with contextlib.redirect_stdout(first):
            cli.main(["--root", str(helm_root), "pending", "--changes"])
        self.assertIn("the first thing", first.getvalue())

        # Nothing changed: silent.
        second = io.StringIO()
        with contextlib.redirect_stdout(second):
            cli.main(["--root", str(helm_root), "pending", "--changes"])
        self.assertEqual(second.getvalue().strip(), "")

        # Something new: only that.
        coordinator.record_situation(
            project["id"], "Worker result: the second thing", surface=True
        )
        third = io.StringIO()
        with contextlib.redirect_stdout(third):
            cli.main(["--root", str(helm_root), "pending", "--changes"])
        printed = third.getvalue()
        self.assertIn("the second thing", printed)
        self.assertNotIn("the first thing", printed)


    def test_a_growing_counter_does_not_reannounce_an_unchanged_item(self) -> None:
        """994s -> 1016s is one character longer, and that used to leak through.

        The identity was built from the DISPLAY line, which is truncated. One
        more digit pushed the cut one character earlier, the tails differed,
        and an item nobody had touched announced itself again -- on every poll
        that crossed a digit boundary. Comparing the full text fixes it.
        """
        helm_root = self._helm_root("counter-root")
        destination = helm_root / "projects" / "counter"
        shutil.move(str(self.repo("counter")), str(destination))
        coordinator = Coordinator(StateStore(helm_root / "state", helm_root=helm_root))
        project = coordinator.discover_project(helm_root, "counter")

        # The REAL detail, and its length is the point: at 117 characters the
        # 80-character display cut lands inside it, so one more digit shifts
        # the tail from "so it" to "so i". A shorter string never truncates
        # and the test would pass without exercising anything.
        detail = (
            "no protocol message and no terminal output for {}s; the session "
            "is alive, so it is slow, wedged or looping, not gone"
        )
        health = [{
            "worker_id": "w-slow", "task_id": "t-1", "role": "worker",
            "project_id": project["id"], "agent_id": None, "execution": "herdr",
            "verdict": "stalled", "detail": detail.format(994),
            "output_idle_seconds": 994.0, "reported_idle_seconds": 994.0,
            "nudged_at": None,
        }]

        def run() -> str:
            buffer = io.StringIO()
            with contextlib.redirect_stdout(buffer):
                cli.main(["--root", str(helm_root), "pending", "--changes"])
            return buffer.getvalue()

        with mock.patch.object(Coordinator, "worker_health", return_value=health):
            first = run()
        self.assertIn("w-slow", first)

        health[0]["detail"] = detail.format(1016)  # one digit wider
        health[0]["output_idle_seconds"] = 1016.0
        with mock.patch.object(Coordinator, "worker_health", return_value=health):
            second = run()
        self.assertEqual(second.strip(), "", "a wider counter is not news")
        self.assertGreater(len(detail.format(994)), 80, "must exceed the display cut")


    def test_pending_survives_every_kind_of_item_it_can_list(self) -> None:
        """The one command that must never fail silently.

        `pending` builds its list from four separate append sites -- an
        escalation, a blocking gate, an owed report and a health verdict. They
        were changed to carry a third element (the untruncated identity that
        --changes compares on) and two were missed, so the command crashed on
        unpack for anyone whose root had an escalation or an open gate. It
        reported nothing at all for as long as that was true, which is the
        worst possible failure for a notifier: silence that looks like calm.
        """
        helm_root = self._helm_root("every-kind-root")
        destination = helm_root / "projects" / "everykind"
        shutil.move(str(self.repo("everykind")), str(destination))
        coordinator = Coordinator(StateStore(helm_root / "state", helm_root=helm_root))
        project = coordinator.discover_project(helm_root, "everykind")
        task = coordinator.create_task(project["id"], "do a thing")
        worker = coordinator.launch_worker(task["id"], [sys.executable, "-c", ""], wait=False)

        coordinator.record_worker_message(worker["id"], "blocker", "cannot reach the tracker")
        coordinator.record_situation(project["id"], "Worker result: it finished", surface=True)

        for argv in (["pending"], ["pending", "--changes"]):
            buffer = io.StringIO()
            with contextlib.redirect_stdout(buffer):
                exit_code = cli.main(["--root", str(helm_root), *argv])
            self.assertEqual(exit_code, 0, f"{argv} must not crash")
            self.assertIn("cannot reach the tracker", buffer.getvalue())


    def test_two_workers_do_not_share_one_identity(self) -> None:
        """Stripping every digit dissolved the worker id and swallowed news.

        w-1a2b3c4d5e6f and w-12ab3c4d5e6f both reduced to "w-edab", so once one
        had been reported the other never was. A repeat is noise; a swallowed
        item is the silence this command exists to prevent, so this error runs
        the dangerous way and gets its own test.
        """
        helm_root = self._helm_root("collide-root")
        destination = helm_root / "projects" / "collide"
        shutil.move(str(self.repo("collide")), str(destination))
        coordinator = Coordinator(StateStore(helm_root / "state", helm_root=helm_root))
        project = coordinator.discover_project(helm_root, "collide")

        def entry(worker_id: str, idle: int) -> dict:
            return {
                "worker_id": worker_id, "task_id": "t-1", "role": "worker",
                "project_id": project["id"], "agent_id": None, "execution": "herdr",
                "verdict": "stalled",
                "detail": f"no protocol message and no terminal output for {idle}s",
                "output_idle_seconds": float(idle),
                "reported_idle_seconds": float(idle), "nudged_at": None,
            }

        def run(health: list) -> str:
            buffer = io.StringIO()
            with mock.patch.object(Coordinator, "worker_health", return_value=health):
                with contextlib.redirect_stdout(buffer):
                    cli.main(["--root", str(helm_root), "pending", "--changes"])
            return buffer.getvalue()

        first = run([entry("w-1a2b3c4d5e6f", 400)])
        self.assertIn("w-1a2b3c4d5e6f", first)

        # A DIFFERENT worker, whose id collides once every digit is stripped.
        second = run([entry("w-1a2b3c4d5e6f", 460), entry("w-12ab3c4d5e6f", 400)])
        self.assertIn("w-12ab3c4d5e6f", second, "the second worker must not be swallowed")
        self.assertNotIn("w-1a2b3c4d5e6f", second, "the first is unchanged; only its clock moved")

    def test_every_surfaced_line_says_how_long_it_has_been_waiting(self) -> None:
        """A blocker from six days ago and one from this minute looked identical.

        Without an age every item reads as equally urgent, which is the same
        failure as an attention list full of healthy workers wearing different
        clothes. Relative rather than absolute, because the question being
        asked is "has this been ignored", not "what time was it".
        """
        helm_root = self._helm_root("aged-root")
        destination = helm_root / "projects" / "aged"
        shutil.move(str(self.repo("aged")), str(destination))
        coordinator = Coordinator(StateStore(helm_root / "state", helm_root=helm_root))
        project = coordinator.discover_project(helm_root, "aged")
        task = coordinator.create_task(project["id"], "do a thing")
        worker = coordinator.launch_worker(task["id"], [sys.executable, "-c", ""], wait=False)
        coordinator.record_worker_message(worker["id"], "blocker", "cannot reach the tracker")

        # Age that escalation by six days.
        data = coordinator.store.load()
        stale = (
            _dt.datetime.now(_dt.timezone.utc) - _dt.timedelta(days=6)
        ).isoformat().replace("+00:00", "Z")
        for message in data["messages"]:
            if message.get("worker_id") == worker["id"]:
                message["created_at"] = stale
        coordinator.store.save(data)

        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            cli.main(["--root", str(helm_root), "pending"])
        printed = buffer.getvalue()
        self.assertIn("cannot reach the tracker", printed)
        self.assertIn("6d", printed, "the reader must be able to see it has been ignored")
        # And WHEN it arrived, which an age alone cannot give back: a reader
        # correlates a clock time against a deploy or their own memory.
        arrived = (
            _dt.datetime.now(_dt.timezone.utc) - _dt.timedelta(days=6)
        ).astimezone()
        self.assertIn(f"{arrived:%m-%d %H:%M}", printed)

    def test_the_age_is_not_part_of_the_changes_identity(self) -> None:
        """An age ticks on every poll; keying on it re-announces everything."""
        helm_root = self._helm_root("ageid-root")
        destination = helm_root / "projects" / "ageid"
        shutil.move(str(self.repo("ageid")), str(destination))
        coordinator = Coordinator(StateStore(helm_root / "state", helm_root=helm_root))
        project = coordinator.discover_project(helm_root, "ageid")
        coordinator.record_situation(project["id"], "Worker result: a thing happened", surface=True)

        def run() -> str:
            buffer = io.StringIO()
            with contextlib.redirect_stdout(buffer):
                cli.main(["--root", str(helm_root), "pending", "--changes"])
            return buffer.getvalue()

        self.assertIn("a thing happened", run())
        # Nothing changed but the clock. It must stay silent.
        self.assertEqual(run().strip(), "")


class ASupersededForemanBlockerLeavesTheAttentionListTests(HelmTestCase):
    """The summons drops when a successor is appointed; the record stays.

    `project_updates_for_watch` already knew this. `open_escalations` -- the
    path `helm pending` actually reads for blockers -- did not, and a blocker
    is live there for as long as its task is `blocked`. A foreman that
    escalates ends `blocked` and there is no route out of that state for a
    task nobody will continue, so those entries never left. Six of them, aged
    7 to 18 hours, were still summoning a human the morning after the work
    they described had been finished and merged.
    """

    def _blocked_foreman(self, project: dict, note: str) -> dict:
        task = self.coordinator.create_task(
            project["id"], "drive the project", role="foreman"
        )
        # A LIVE process, not `-c ""`. An immediately-exiting worker races
        # its own blocker: Helm can settle it before the message is recorded,
        # and the escalation then never exists -- which made this suite fail
        # in both directions depending on who won.
        worker = self.coordinator.launch_worker(
            task["id"], [sys.executable, "-c", "import time; time.sleep(30)"], wait=False
        )
        self.coordinator.record_worker_message(worker["id"], "blocker", note)
        return task

    def _project(self, name: str) -> dict:
        root = self.repo(name)
        return self.coordinator.register_project(
            name.title(), str(root), project_id=name
        )

    def _open(self, note: str) -> list:
        return [e for e in self.coordinator.open_escalations() if note in e["text"]]

    def test_a_foreman_blocker_stands_while_nobody_has_replaced_it(self) -> None:
        project = self._project("alone")
        self._blocked_foreman(project, "needs a decision alpha")
        self.assertTrue(self._open("needs a decision alpha"))

    def test_a_live_successor_answers_it(self) -> None:
        project = self._project("livesucc")
        self._blocked_foreman(project, "needs a decision beta")
        self.coordinator.create_task(project["id"], "drive it", role="foreman")
        self.assertEqual(self._open("needs a decision beta"), [])

    def test_a_successor_THAT_HAS_SINCE_FINISHED_also_answers_it(self) -> None:
        """The case that was broken, and the ordinary one.

        A foreman takes over, does the work, reports and stands down. Its task
        is then `completed`, so a rule that looked only for a LIVE successor
        found none and every blocker behind it started summoning again.
        """
        project = self._project("finishedsucc")
        self._blocked_foreman(project, "needs a decision gamma")
        successor = self.coordinator.create_task(
            project["id"], "drive it", role="foreman"
        )
        with self.coordinator.store.locked() as data:
            entry = data["tasks"][successor["id"]]
            entry["status"] = "completed"
            # Stamped a second later on purpose. `created_at` has one-second
            # resolution and a fixture creates both tasks inside the same
            # second, which no real appointment does -- leaving it tied would
            # be testing the clock, not the rule.
            entry["created_at"] = "2099-01-01T00:00:00Z"
        self.assertEqual(self._open("needs a decision gamma"), [])

    def test_a_WORKER_blocker_is_never_superseded_by_a_foreman(self) -> None:
        """The guard that matters. A worker's blocker is nobody else's ask.

        Foremen come and go on a project constantly; if their arrival retired
        worker escalations, the list would quietly empty itself of exactly the
        items it exists for.
        """
        project = self._project("workerblock")
        task = self.coordinator.create_task(project["id"], "do the work")
        worker = self.coordinator.launch_worker(
            task["id"], [sys.executable, "-c", "import time; time.sleep(30)"], wait=False
        )
        self.coordinator.record_worker_message(
            worker["id"], "blocker", "needs a credential delta"
        )
        self.coordinator.create_task(project["id"], "drive it", role="foreman")
        self.coordinator.create_task(project["id"], "drive it again", role="foreman")
        self.assertTrue(
            self._open("needs a credential delta"),
            "a worker's blocker must survive any number of foremen",
        )

    def test_another_projects_foreman_does_not_answer_it(self) -> None:
        project = self._project("mine")
        other = self._project("theirs")
        self._blocked_foreman(project, "needs a decision epsilon")
        self.coordinator.create_task(other["id"], "drive it", role="foreman")
        self.assertTrue(
            self._open("needs a decision epsilon"),
            "supersession must not cross a project boundary",
        )


class BusyIsNotAnAttentionItemTests(HelmTestCase):
    """A worker whose log is moving is not something a human can act on.

    `working`, `driving` and `quiet` all mean the same underlying fact: the
    worker's own log IS moving, so it is demonstrably alive and busy, and the
    only thing absent is a protocol push. In one session those verdicts fired
    eight times against workers that were mid-compile or writing to disk that
    very second, and each one cost a verification round to dismiss. A signal
    that is usually wrong teaches the reader to skim it the one time it is
    right, which is the same failure as an attention list full of healthy
    workers. `stalled` -- gone dark, no output at all -- is the verdict that
    means something, and it must survive this filter.
    """

    def _status_with_verdict(self, name: str, verdict: str):
        root = self.repo(name)
        project = self.coordinator.register_project(name, str(root), project_id=name)
        entry = {
            "worker_id": "w-fake",
            "project_id": project["id"],
            "verdict": verdict,
            "detail": "constructed for this test",
        }
        with mock.patch.object(self.coordinator, "worker_health", return_value=[entry]):
            status = self.coordinator.project_status(project["id"])
        return [h["verdict"] for h in status["needs_attention"]]

    def test_a_busy_worker_never_reaches_the_attention_list(self) -> None:
        for verdict in ("working", "driving", "quiet"):
            with self.subTest(verdict=verdict):
                self.assertEqual(
                    self._status_with_verdict(f"busy-{verdict}", verdict),
                    [],
                    f"{verdict!r} means the log is moving; a human cannot act on it",
                )

    def test_a_worker_that_has_gone_dark_still_reaches_it(self) -> None:
        # The other half, and the one that matters: quieting the benign
        # verdicts must not quiet the verdict that means a worker may be gone.
        self.assertEqual(
            self._status_with_verdict("dark", "stalled"),
            ["stalled"],
            "stalled means no output at all and must survive the filter",
        )


class OneDefinitionOfHealthyTests(HelmTestCase):
    """The healthy-verdict set is defined once, not copied.

    It had drifted into four literal copies -- one in core, three in the CLI.
    Removing `quiet` from the core copy left the other three still listing it
    as needing attention, so the fix changed nothing on the surface the
    commander actually reads. A duplicated policy is not a style problem: it
    is a rule that can be true in one place and false in another.
    """

    def test_no_source_file_carries_a_second_copy_of_the_set(self) -> None:
        from helm.core import HEALTHY_WORKER_VERDICTS

        marker = '"healthy", "settled", "reported"'
        for name in ("core.py", "cli.py", "herdr.py"):
            text = (REPO_ROOT / "helm" / name).read_text(encoding="utf-8")
            self.assertLessEqual(
                text.count(marker),
                1 if name == "core.py" else 0,
                f"{name} spells the healthy set out again; import "
                f"HEALTHY_WORKER_VERDICTS instead",
            )
        self.assertIn("quiet", HEALTHY_WORKER_VERDICTS)
        self.assertNotIn("stalled", HEALTHY_WORKER_VERDICTS)
