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
        text = "HELM NEEDS A HUMAN (1):\n  09-11 10:56   5m 🟦 media — approval-needed push"

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
