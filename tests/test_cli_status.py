"""CLI surfaces: status, watch, board, action items and project colour."""

from __future__ import annotations

import contextlib
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
