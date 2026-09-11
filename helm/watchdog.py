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
#: A list that has stood unchanged this long is said again. A desktop banner
#: is gone in seconds, and an approval that waited seven hours had been
#: announced exactly once, at the moment nobody was looking. 0 disables it.
DEFAULT_REMIND_MINUTES = 60
#: A command the commander names to carry the notification somewhere a
#: banner does not reach -- a chat message, a phone. Run with `sh -c`, the
#: title and headline in HELM_TITLE and HELM_MESSAGE, and the whole pending
#: list on stdin. Set by `install --notify-command`, which writes it into the
#: scheduler entry as this environment variable.
NOTIFY_ENV = "HELM_WATCHDOG_NOTIFY"
#: A death is acted on only when it reads the same on two checks this far
#: apart. The one time healing ran on a single reading it killed each new
#: launch inside its first poll: the probe returns false death for a worker
#: still starting and for the runner-in-pane shape. Two readings a minute
#: apart, on top of the health check's own startup grace and pid-grade rule,
#: is the difference between evidence and a glitch.
HEAL_CONFIRM_SECONDS = 60


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


def _notify(title: str, message: str, *, text: str = "", command: str | None = None) -> bool:
    """Best-effort notification: the desktop, plus the commander's own command
    when one is configured. Never fatal: the text also goes to stdout."""
    carried = False
    hook = command if command is not None else os.environ.get(NOTIFY_ENV, "").strip()
    if hook:
        with _quiet():
            done = subprocess.run(
                ["sh", "-c", hook], input=text or message, text=True, timeout=60, check=False,
                env={**os.environ, "HELM_TITLE": title, "HELM_MESSAGE": message},
            )
            carried = done.returncode == 0
    return _desktop_notify(title, message) or carried


def _desktop_notify(title: str, message: str) -> bool:
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


def sync_pull_requests(root: Path | None) -> dict[str, object]:
    """Read every open PR that is due a look, and record what the forge says.

    Bounded inside the coordinator to one read per task per interval, and
    quiet about a remote it cannot reach, so calling it on every poll costs
    nothing on an offline laptop and catches a merge within minutes on a
    connected one.
    """
    from .core import Coordinator
    from .state import StateStore

    store = StateStore(root / "state", helm_root=root) if root else StateStore()
    coordinator = Coordinator(store)
    synced = coordinator.sync_open_pull_requests()
    # The same pass sheds what a standing cleanup grant covers, and archives
    # the records that then hold nothing -- housekeeping nobody has to run.
    swept = coordinator.sweep_residue_under_grants()
    if swept["cleaned"]:
        coordinator.archive_tasks([entry["task_id"] for entry in swept["cleaned"]])
    synced["cleaned"] = [entry["task_id"] for entry in swept["cleaned"]]
    return synced


def heal_pass(root: Path | None, memory: Path) -> list[str]:
    """Settle workers that are provably dead, and re-drive a project left without a driver.

    Only the `died` verdict is acted on -- the process is gone, or the
    provider says the pane is and the worker has been silent past the
    threshold -- and only once it has read that way on two checks at least
    `HEAL_CONFIRM_SECONDS` apart; `memory` holds the first sighting. A dead
    foreman is replaced by `_heal_dead_worker` itself. A project whose
    foreman is gone while a worker of it is still running is re-driven, so a
    worker's next question is answered rather than recorded into nothing.
    Stalled or erroring workers are reported, never touched.
    """
    import json

    from . import cli
    from .core import Coordinator
    from .state import StateStore

    store = StateStore(root / "state", helm_root=root) if root else StateStore()
    coordinator = Coordinator(store)
    seen: dict[str, float] = {}
    with _quiet():
        seen = {k: float(v) for k, v in json.loads(memory.read_text(encoding="utf-8")).items()}
    current: dict[str, float] = {}
    reports: list[str] = []
    moment = time.time()
    for entry in coordinator.worker_health(liveness=cli._liveness_probe(coordinator)):
        if entry.get("verdict") != "died" or not entry.get("worker_id"):
            continue
        first = seen.get(entry["worker_id"], moment)
        if moment - first < HEAL_CONFIRM_SECONDS:
            current[entry["worker_id"]] = first
            continue
        healed = cli._heal_dead_worker(coordinator, entry)
        if healed:
            reports.append(healed)
        else:
            current[entry["worker_id"]] = first
    with _quiet():
        memory.write_text(json.dumps(current), encoding="utf-8")
    data = store.load()
    for project in data.get("projects", {}).values():
        project_id = project["id"]
        driving = coordinator.foreman_for(project_id, data=data)
        if driving is not None:
            continue
        running = [
            worker for worker in data.get("workers", {}).values()
            if worker.get("project_id") == project_id and worker.get("status") == "running"
            and (data.get("tasks", {}).get(worker.get("task_id")) or {}).get("role") != "foreman"
        ]
        if not running:
            continue
        with _quiet():
            if not coordinator.project_wants_foreman(project_id):
                continue
            appointed = cli._ensure_foreman(coordinator, project_id)
            if appointed:
                reports.append(
                    f"helm watchdog: {project_id} had {len(running)} running worker(s) and no "
                    f"foreman; appointed {appointed['worker']['id']}"
                )
    return reports


def run(
    root: Path | None,
    interval: int,
    once: bool = False,
    *,
    notify_command: str | None = None,
    remind_minutes: float = DEFAULT_REMIND_MINUTES,
    heal: bool = True,
) -> int:
    """Check now, then every `interval` seconds until stopped.

    The interval is a POLL, not a report cadence: it decides how quickly
    something reaches the human, and a human waiting fifteen minutes to learn a
    publish failed is not being told promptly. Cheap by construction -- the
    check reads state already on disk and prints nothing unless the set of
    waiting items changed -- so a short interval costs almost nothing and buys
    the difference between "surfaced" and "surfaced in time".

    A list that changes is announced; a list that stands unchanged is said
    again after `remind_minutes`, once per that interval, as a reminder
    rather than as news.
    """
    state = Path(os.environ.get("TMPDIR", "/tmp")) / "helm-watchdog.last"
    memory = Path(os.environ.get("TMPDIR", "/tmp")) / "helm-watchdog.dead"
    while True:
        if heal:
            try:
                for line in heal_pass(root, memory):
                    print(line, flush=True)
            except Exception as exc:  # noqa: BLE001 - healing is a courtesy, never the reason to die
                print(f"helm watchdog: heal skipped: {exc}", file=sys.stderr, flush=True)
        try:
            synced = sync_pull_requests(root)
            for task_id in synced.get("merged", []):
                print(f"helm watchdog: pull request merged for task {task_id}", flush=True)
        except Exception as exc:  # noqa: BLE001 - the sync is a courtesy, never the reason to die
            print(f"helm watchdog: PR sync skipped: {exc}", file=sys.stderr, flush=True)
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
            previous, last_told = "", 0.0
            with _quiet():
                recorded = state.read_text(encoding="utf-8").split()
                previous = recorded[0] if recorded else ""
                last_told = float(recorded[1]) if len(recorded) > 1 else 0.0
            changed = current != previous
            standing = (
                not changed and remind_minutes > 0
                and time.time() - last_told >= remind_minutes * 60
            )
            if changed or standing:
                with _quiet():
                    state.write_text(f"{current} {time.time():.3f}", encoding="utf-8")
                title = "Helm" if changed else "Helm, still waiting"
                _notify(title, _headline(text), text=text, command=notify_command)
                print(text, flush=True)
        if once:
            return 0
        time.sleep(max(5, interval))


def _xml_escape(value: str) -> str:
    return (
        value.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")
    )


def _launchd_plist(
    root: Path, interval: int, log: Path, *, notify_command: str = "",
    remind_minutes: float = DEFAULT_REMIND_MINUTES, heal: bool = True,
) -> str:
    executable = sys.executable
    heal_flag = "" if heal else "    <string>--no-heal</string>\n"
    environment = (
        f"  <key>EnvironmentVariables</key>\n  <dict>\n    <key>{NOTIFY_ENV}</key>"
        f"<string>{_xml_escape(notify_command)}</string>\n  </dict>\n"
        if notify_command else ""
    )
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
    <string>--remind-after</string><string>{remind_minutes:g}</string>
{heal_flag}  </array>
{environment}  <key>WorkingDirectory</key><string>{root}</string>
  <key>KeepAlive</key><true/>
  <key>RunAtLoad</key><true/>
  <key>StandardOutPath</key><string>{log}</string>
  <key>StandardErrorPath</key><string>{log}</string>
</dict>
</plist>
"""


def _systemd_units(
    root: Path, interval: int, *, notify_command: str = "",
    remind_minutes: float = DEFAULT_REMIND_MINUTES, heal: bool = True,
) -> tuple[str, str]:
    executable = sys.executable
    heal_flag = "" if heal else " --no-heal"
    environment = (
        f'Environment="{NOTIFY_ENV}={notify_command.replace(chr(34), chr(92) + chr(34))}"\n'
        if notify_command else ""
    )
    service = f"""[Unit]
Description=Helm watchdog: surface what needs a human

[Service]
Type=simple
WorkingDirectory={root}
{environment}ExecStart={executable} -m helm --root {root} watchdog run --interval {interval} --remind-after {remind_minutes:g}{heal_flag}
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


def install(
    root: Path, interval: int, *, notify_command: str = "",
    remind_minutes: float = DEFAULT_REMIND_MINUTES, heal: bool = True,
) -> int:
    """Generate and load the platform's own scheduler entry."""
    system = platform.system()
    if system == "Darwin":
        target = Path.home() / "Library" / "LaunchAgents" / f"{LABEL}.plist"
        log = root / "state" / "watchdog.log"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(
            _launchd_plist(
                root, interval, log, notify_command=notify_command, remind_minutes=remind_minutes, heal=heal,
            ),
            encoding="utf-8",
        )
        with _quiet():
            subprocess.run(["launchctl", "unload", str(target)], check=False, timeout=20)
        result = subprocess.run(
            ["launchctl", "load", str(target)], check=False, timeout=20
        )
        print(f"Installed the Helm watchdog: {target}")
        print(f"  Polls every {interval}s against {root} and notifies within that,")
        print("  staying silent unless something needs a human AND the list changed,")
        print(f"  then saying it again every {remind_minutes:g} minutes while it still waits.")
        if notify_command:
            print(f"  Each notification also runs your command: {notify_command}")
        print(
            "  A worker that reads as dead on two checks a minute apart is stopped, a dead foreman "
            "replaced, and a project with running workers and no driver re-driven."
            if heal else "  Healing is off (--no-heal): deaths are reported, never acted on."
        )
        if result.returncode != 0:
            print("  launchctl load reported a problem; run it by hand to see why.")
            return 1
        return 0
    if system == "Linux":
        unit_dir = Path.home() / ".config" / "systemd" / "user"
        unit_dir.mkdir(parents=True, exist_ok=True)
        service, _timer = _systemd_units(
            root, interval, notify_command=notify_command, remind_minutes=remind_minutes, heal=heal,
        )
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
