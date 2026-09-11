"""The backstop for when the reporting chain does not fire.

Reporting is a push, and a push can be missed. A worker killed by the OS
reports nothing at all; a foreman that dies mid-round reports nothing either;
and the coordinator that would relay any of it only exists inside a
conversation turn, so nothing reaches the commander while they are away.

This runs outside all of that. It reconciles worker state, and when something
genuinely needs a human it says so through a channel that reaches one.

Two properties it must keep, because both are how notifiers die:

- **Quiet when nothing is wrong.** `helm pending` prints nothing on a healthy
  root, and this stays silent with it.
- **Quiet when nothing has CHANGED.** A backlog nobody has cleared must not
  nag on every interval. The fingerprint ignores digits, because the pending
  list carries elapsed times ("quiet for 1137s") that differ on every run and
  would otherwise make an unchanged list look like news every time.

Platform integration is generated, never assumed: `install` writes a launchd
agent on macOS or a systemd user timer on Linux, both pointing at this
module's own `run`. Nothing machine-specific is tracked in the repository, so
a fresh clone installs its own.
"""

from __future__ import annotations

import hashlib
import os
import platform
import shutil
import subprocess
import sys
import time
from pathlib import Path

LABEL = "com.helm.watchdog"
DEFAULT_INTERVAL = 20


def _fingerprint(text: str) -> str:
    """Identity of a pending list, ignoring how long things have been waiting."""
    stripped = "".join(character for character in text if not character.isdigit())
    return hashlib.sha256(stripped.encode("utf-8")).hexdigest()


def _headline(text: str) -> str:
    """The first line that says something, not the line that counts things.

    This used to be `text.splitlines()[0]`, which is always the header --
    "HELM NEEDS A HUMAN (4):". Every notification Helm has ever sent said a
    number and nothing else: no project, no subject, no verb. Two of them in a
    row are indistinguishable, so a reader learns within a day that opening one
    tells them nothing, and stops looking. The chain then delivers perfectly
    and informs nobody, which is worse than not delivering, because the log
    shows a notification was sent.

    The banner has room for roughly one line, so it gets the oldest waiting
    item -- the one that has gone longest without an answer -- and the header's
    count is appended so the reader knows whether there are others behind it.
    """
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    if not lines:
        return "something needs a human"
    header = lines[0]
    items = [
        line
        for line in lines[1:]
        if not line.startswith("(") and "helm status for detail" not in line
    ]
    if not items:
        return header
    count = ""
    if "(" in header and ")" in header:
        count = header[header.index("(") : header.index(")") + 1]
    first = items[0]
    return f"{first} {count}".strip() if count else first


def _notify(title: str, message: str) -> bool:
    """Best-effort desktop notification. Never fatal: the text also goes to stdout."""
    if shutil.which("osascript"):
        # AppleScript string literals, not Python ones: `repr` produces single
        # quotes, which osascript rejects outright, and the pending headline
        # routinely contains quotes, colons and parentheses.
        def _as_literal(value: str) -> str:
            escaped = value.replace("\\", "\\\\").replace('"', '\\"')
            return f'"{escaped}"'

        script = (
            f"display notification {_as_literal(message)} "
            f"with title {_as_literal(title)}"
        )
        with _quiet():
            done = subprocess.run(
                ["osascript", "-e", script], timeout=10, check=False
            )
            # What this can and cannot promise. A non-zero exit means the
            # notifier itself failed and is worth reporting. A zero exit means
            # only that osascript accepted the script: macOS posts the banner
            # as the scripting host, and if that host has no notification
            # permission the banner is dropped in silence and the exit is
            # still 0. So True here means "handed off without error", never
            # "the human saw it" -- and the text goes to stdout regardless,
            # which is the copy that can actually be checked afterwards.
            return done.returncode == 0
    if shutil.which("notify-send"):
        with _quiet():
            done = subprocess.run(
                ["notify-send", title, message], timeout=10, check=False
            )
            return done.returncode == 0
    return False


class _quiet:
    """Swallow anything a notifier does wrong. It is the least important step here."""

    def __enter__(self) -> None:
        return None

    def __exit__(self, *exc: object) -> bool:
        return True


def pending_text(root: Path | None) -> str:
    """Run `helm pending` in-process and capture it.

    NOT --heal. Healing was wired in here for one hour and executed healthy
    workers: the liveness probe returns false death for a just-launched
    worker and for the runner-in-pane shape, and a heal acting on that
    verdict killed each new launch within its first poll interval -- the
    exit record said "provably gone" about a process that was starting up.
    Until the probe itself is fixed to require pid-grade evidence plus a
    startup grace period, the watchdog only watches.
    """
    from . import cli

    import io
    import contextlib as _contextlib

    argv = ["--root", str(root)] if root else []
    buffer = io.StringIO()
    with _contextlib.redirect_stdout(buffer):
        cli.main([*argv, "pending"])
    return buffer.getvalue().strip()


def run(root: Path | None, interval: int, once: bool = False) -> int:
    """Check now, then every `interval` seconds until stopped.

    The interval is a POLL, not a report cadence: it decides how quickly
    something reaches the human, and a human waiting fifteen minutes to learn a
    publish failed is not being told promptly. Cheap by construction -- the
    check reads state already on disk and prints nothing unless the set of
    waiting items changed -- so a short interval costs almost nothing and buys
    the difference between "surfaced" and "surfaced in time".
    """
    state = Path(os.environ.get("TMPDIR", "/tmp")) / "helm-watchdog.last"
    while True:
        try:
            text = pending_text(root)
        except Exception as exc:  # noqa: BLE001 - a watchdog that dies is worse
            print(f"helm watchdog: check failed: {exc}", file=sys.stderr, flush=True)
            text = ""
        if not text:
            with _quiet():
                state.write_text("", encoding="utf-8")
        else:
            current = _fingerprint(text)
            previous = ""
            with _quiet():
                previous = state.read_text(encoding="utf-8").strip()
            if current != previous:
                with _quiet():
                    state.write_text(current, encoding="utf-8")
                _notify("Helm", _headline(text))
                print(text, flush=True)
        if once:
            return 0
        time.sleep(max(5, interval))


def _launchd_plist(root: Path, interval: int, log: Path) -> str:
    executable = sys.executable
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>{LABEL}</string>
  <key>ProgramArguments</key>
  <array>
    <string>{executable}</string>
    <string>-m</string><string>helm</string>
    <string>--root</string><string>{root}</string>
    <string>watchdog</string><string>run</string>
    <string>--interval</string><string>{interval}</string>
  </array>
  <key>WorkingDirectory</key><string>{root}</string>
  <key>KeepAlive</key><true/>
  <key>RunAtLoad</key><true/>
  <key>StandardOutPath</key><string>{log}</string>
  <key>StandardErrorPath</key><string>{log}</string>
</dict>
</plist>
"""


def _systemd_units(root: Path, interval: int) -> tuple[str, str]:
    executable = sys.executable
    service = f"""[Unit]
Description=Helm watchdog: surface what needs a human

[Service]
Type=simple
WorkingDirectory={root}
ExecStart={executable} -m helm --root {root} watchdog run --interval {interval}
Restart=always
"""
    timer = f"""[Unit]
Description=Helm watchdog timer

[Timer]
OnBootSec=5min
OnUnitActiveSec={interval}s

[Install]
WantedBy=timers.target
"""
    return service, timer


def restart() -> int:
    """Restart the running watchdog so it picks up the code on disk.

    The daemon imports Helm once at startup and keeps that copy for its whole
    life, so editing `helm/watchdog.py` changes nothing about what a human
    actually receives until this runs. Nothing about the outside says so: git
    says fixed, the tests say fixed, and the process quietly delivers the
    previous build.

    Separate from `install` deliberately. Install rewrites the scheduler entry
    -- interval, paths, log location -- which is the wrong tool for "I changed
    the code": it would silently reset a hand-edited interval, and a command
    whose name says "install" is not where anyone looks after fixing a bug.
    """
    system = platform.system()
    if system == "Darwin":
        target = Path.home() / "Library" / "LaunchAgents" / f"{LABEL}.plist"
        if not target.exists():
            print("No watchdog is installed; run helm watchdog install first.")
            return 1
        # `kickstart -k` kills the running instance and starts it again under
        # the same entry. `launchctl load` on an already-loaded agent is a
        # no-op, which is exactly the silent nothing this command exists to
        # avoid.
        result = subprocess.run(
            ["launchctl", "kickstart", "-k", f"gui/{os.getuid()}/{LABEL}"],
            check=False, timeout=20, capture_output=True, text=True,
        )
        if result.returncode != 0:
            print("Could not restart the watchdog:")
            print("  " + (result.stderr or result.stdout or "").strip())
            return 1
        print("Restarted the Helm watchdog; it is now running the code on disk.")
        return 0
    if system == "Linux":
        result = subprocess.run(
            ["systemctl", "--user", "restart", "helm-watchdog.service"],
            check=False, timeout=20, capture_output=True, text=True,
        )
        if result.returncode != 0:
            print("Could not restart the watchdog:")
            print("  " + (result.stderr or result.stdout or "").strip())
            return 1
        print("Restarted the Helm watchdog; it is now running the code on disk.")
        return 0
    print(f"No scheduler integration for {system}; restart the watchdog by hand.")
    return 1


def install(root: Path, interval: int) -> int:
    """Generate and load the platform's own scheduler entry."""
    system = platform.system()
    if system == "Darwin":
        target = Path.home() / "Library" / "LaunchAgents" / f"{LABEL}.plist"
        log = root / "state" / "watchdog.log"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(_launchd_plist(root, interval, log), encoding="utf-8")
        with _quiet():
            subprocess.run(["launchctl", "unload", str(target)], check=False, timeout=20)
        result = subprocess.run(
            ["launchctl", "load", str(target)], check=False, timeout=20
        )
        print(f"Installed the Helm watchdog: {target}")
        print(f"  Polls every {interval}s against {root} and notifies within that,")
        print("  staying silent unless something needs a human AND the list changed.")
        if result.returncode != 0:
            print("  launchctl load reported a problem; run it by hand to see why.")
            return 1
        return 0
    if system == "Linux":
        unit_dir = Path.home() / ".config" / "systemd" / "user"
        unit_dir.mkdir(parents=True, exist_ok=True)
        service, _timer = _systemd_units(root, interval)
        # A continuously-polling service needs no timer: a timer would restart
        # it on a cadence, which is the very latency this is removing.
        (unit_dir / "helm-watchdog.service").write_text(service, encoding="utf-8")
        with _quiet():
            subprocess.run(["systemctl", "--user", "daemon-reload"], check=False, timeout=20)
            subprocess.run(
                ["systemctl", "--user", "enable", "--now", "helm-watchdog.service"],
                check=False,
                timeout=20,
            )
        print(f"Installed the Helm watchdog: {unit_dir}/helm-watchdog.service")
        print(f"  Runs every {interval}s against {root}.")
        return 0
    # Windows, BSD, a container without an init -- say so rather than pretending.
    print(f"No scheduler integration for {system}.")
    print("  Run it yourself instead, however this machine starts long-lived jobs:")
    print(f"    {sys.executable} -m helm --root {root} watchdog run --interval {interval}")
    return 1


def uninstall(root: Path) -> int:
    system = platform.system()
    if system == "Darwin":
        target = Path.home() / "Library" / "LaunchAgents" / f"{LABEL}.plist"
        with _quiet():
            subprocess.run(["launchctl", "unload", str(target)], check=False, timeout=20)
        if target.exists():
            target.unlink()
            print(f"Removed {target}")
            return 0
        print("No watchdog was installed.")
        return 0
    if system == "Linux":
        unit_dir = Path.home() / ".config" / "systemd" / "user"
        with _quiet():
            subprocess.run(
                ["systemctl", "--user", "disable", "--now", "helm-watchdog.service"],
                check=False,
                timeout=20,
            )
        removed = False
        for name in ("helm-watchdog.timer", "helm-watchdog.service"):
            path = unit_dir / name
            if path.exists():
                path.unlink()
                removed = True
        print("Removed the watchdog units." if removed else "No watchdog was installed.")
        return 0
    print(f"Nothing to remove: no scheduler integration for {system}.")
    return 0
