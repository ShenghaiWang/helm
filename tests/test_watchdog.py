"""The watchdog reaches a human, and says it again while they have not answered."""

from __future__ import annotations

import os
from pathlib import Path
from unittest import mock

from helm import watchdog
from tests.support import HelmTestCase


class WatchdogTests(HelmTestCase):
    def test_the_notify_command_carries_the_title_headline_and_whole_list(self) -> None:
        target = Path(self.temp.name) / "carried.txt"
        command = f'printf "%s|%s|" "$HELM_TITLE" "$HELM_MESSAGE" > {target}; cat >> {target}'
        with mock.patch.object(watchdog, "_desktop_notify", return_value=False):
            carried = watchdog._notify("Helm", "the headline", text="line one\nline two", command=command)
        self.assertTrue(carried)
        self.assertEqual(target.read_text(), "Helm|the headline|line one\nline two")
        # The environment variable is the scheduler's way of passing it.
        with mock.patch.object(watchdog, "_desktop_notify", return_value=False), \
             mock.patch.dict(os.environ, {watchdog.NOTIFY_ENV: f"cat > {target}"}):
            self.assertTrue(watchdog._notify("Helm", "again", text="from env"))
        self.assertEqual(target.read_text(), "from env")

    def test_a_standing_list_is_said_again_after_the_reminder_interval(self) -> None:
        told: list[tuple[str, str]] = []
        text = "Commander, for your attention (1):\n  09-11 10:56   5m 🟦 media — approval-needed push"

        def notify(title, message, **kwargs):
            told.append((title, message))
            return True

        with mock.patch.dict(os.environ, {"TMPDIR": self.temp.name}), \
             mock.patch.object(watchdog, "pending_text", return_value=text), \
             mock.patch.object(watchdog, "_notify", side_effect=notify), \
             mock.patch("builtins.print"):
            watchdog.run(None, 20, once=True, remind_minutes=60)
            watchdog.run(None, 20, once=True, remind_minutes=60)          # unchanged, too soon: quiet
            self.assertEqual(len(told), 1)
            watchdog.run(None, 20, once=True, remind_minutes=0)           # reminders off: quiet
            self.assertEqual(len(told), 1)
            watchdog.run(None, 20, once=True, remind_minutes=0.0000001)   # long enough: said again
            self.assertEqual(len(told), 2)
            self.assertEqual(told[0][0], "Helm")
            self.assertEqual(told[1][0], "Helm, still waiting")
            with mock.patch.object(watchdog, "pending_text", return_value=""):
                watchdog.run(None, 20, once=True)                          # cleared: quiet, and forgotten
            self.assertEqual(len(told), 2)
            watchdog.run(None, 20, once=True, remind_minutes=60)          # back: news again
            self.assertEqual(len(told), 3)

    def test_the_scheduler_entries_carry_the_command_and_the_reminder(self) -> None:
        plist = watchdog._launchd_plist(
            Path("/root"), 20, Path("/root/state/watchdog.log"),
            notify_command='say "<needs a human>" & post', remind_minutes=45,
        )
        self.assertIn("<key>HELM_WATCHDOG_NOTIFY</key><string>say &quot;&lt;needs a human&gt;&quot; &amp; post</string>", plist)
        self.assertIn("<string>--remind-after</string><string>45</string>", plist)
        self.assertNotIn("EnvironmentVariables", watchdog._launchd_plist(Path("/root"), 20, Path("/l")))
        service, _timer = watchdog._systemd_units(Path("/root"), 20, notify_command="post it", remind_minutes=30)
        self.assertIn('Environment="HELM_WATCHDOG_NOTIFY=post it"', service)
        self.assertIn("--remind-after 30", service)

    def test_a_death_is_healed_only_after_it_reads_the_same_twice_a_minute_apart(self) -> None:
        import json
        import sys

        root = self.repo("healed")
        project = self.coordinator.register_project("Healed", str(root), project_id="healed")
        task = self.coordinator.create_task(project["id"], "work")
        worker = self.coordinator.prepare_external_worker(task["id"], [sys.executable, "-c", ""])
        died = {"worker_id": worker["id"], "project_id": project["id"], "verdict": "died"}
        healed: list[str] = []
        memory = Path(self.temp.name) / "dead.json"
        from helm import cli
        with mock.patch.object(type(self.coordinator), "worker_health", return_value=[died]), \
             mock.patch.object(cli, "_heal_dead_worker", side_effect=lambda c, e: healed.append(e["worker_id"]) or "healed"), \
             mock.patch.object(cli, "_ensure_foreman", return_value=None), \
             mock.patch("helm.watchdog.Coordinator", return_value=self.coordinator, create=True), \
             mock.patch("helm.core.Coordinator", return_value=self.coordinator):
            with mock.patch.dict("os.environ", {"HELM_STATE_DIR": str(self.state.directory)}):
                first = watchdog.heal_pass(None, memory)
                self.assertEqual(first, [])
                self.assertEqual(healed, [])
                self.assertIn(worker["id"], json.loads(memory.read_text()))
                # The same reading a minute later is evidence; act on it.
                seen = json.loads(memory.read_text())
                seen[worker["id"]] = seen[worker["id"]] - watchdog.HEAL_CONFIRM_SECONDS - 1
                memory.write_text(json.dumps(seen))
                second = watchdog.heal_pass(None, memory)
        self.assertEqual(second, ["healed"])
        self.assertEqual(healed, [worker["id"]])
        self.assertEqual(json.loads(memory.read_text()), {})

    def test_a_project_with_running_workers_and_no_foreman_is_re_driven(self) -> None:
        import sys

        root = self.repo("driverless")
        project = self.coordinator.register_project("Driverless", str(root), project_id="driverless")
        task = self.coordinator.create_task(project["id"], "work")
        worker = self.coordinator.prepare_external_worker(task["id"], [sys.executable, "-c", ""])
        self.assertEqual(self.state.load()["workers"][worker["id"]]["status"], "running")
        appointed: list[str] = []
        from helm import cli

        def appoint(coordinator, project_id, **kwargs):
            appointed.append(project_id)
            return {"worker": {"id": "w-new-foreman"}}

        memory = Path(self.temp.name) / "dead2.json"
        with mock.patch.object(type(self.coordinator), "worker_health", return_value=[]), \
             mock.patch.object(cli, "_ensure_foreman", side_effect=appoint), \
             mock.patch("helm.core.Coordinator", return_value=self.coordinator), \
             mock.patch.dict("os.environ", {"HELM_STATE_DIR": str(self.state.directory)}):
            reports = watchdog.heal_pass(None, memory)
        self.assertEqual(appointed, [project["id"]])
        self.assertIn("appointed w-new-foreman", reports[0])
        # A project that declined a foreman is left alone.
        with self.state.locked() as data:
            data["projects"][project["id"]]["foreman"] = False
        appointed.clear()
        with mock.patch.object(type(self.coordinator), "worker_health", return_value=[]), \
             mock.patch.object(cli, "_ensure_foreman", side_effect=appoint), \
             mock.patch("helm.core.Coordinator", return_value=self.coordinator), \
             mock.patch.dict("os.environ", {"HELM_STATE_DIR": str(self.state.directory)}):
            self.assertEqual(watchdog.heal_pass(None, memory), [])
        self.assertEqual(appointed, [])
