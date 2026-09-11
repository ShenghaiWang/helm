"""Whether a worker is alive, silent, stuck, or done.

A mixin over `CoordinatorBase`, moved out of `core` unchanged. It resolves
every cross-call through `self` at runtime and imports nothing from
`helm.core`.
"""

from __future__ import annotations

import contextlib
import datetime as _dt
import re
import time

from pathlib import Path
from typing import Any, Callable

from ..discovery import _discovery_settings
from ..errors import HelmError, SafetyError
from ..paths import canonical
from ..values import _dt_now, _parse_iso, _safe_text, now, project_glyph


class HealthMixin:
    # ---------- worker health ----------

    # A worker that says nothing is indistinguishable from one that died, so
    # silence is measured rather than assumed benign.  Two clocks are needed:
    # a worker can be emitting plenty of terminal output while never pushing a
    # protocol message (busy but unreportable), or pushing nothing at all with
    # a frozen screen (stalled or dead).
    SILENCE_SECONDS = 300.0

    # Signatures of a session that has broken rather than finished. Helm cannot
    # see inside an agent's conversation, but it captures every byte the agent
    # printed -- so the evidence of a failure is already on disk, and was simply
    # never read. Deliberately narrow: a phrase that also appears in ordinary
    # work would make the check noise, and noise is how a warning stops working.
    #: Matched as regexes against the READABLE text of a line, so a signature
    #: can say where it must appear rather than only what it says. Most are
    #: distinctive enough as plain phrases; the exception is the shell's
    #: kill report, which as a bare substring matches the ordinary English
    #: word and flagged a foreman that was DESCRIBING a fix -- "a staleness
    #: horizon so a writer killed mid-append cannot wedge the day" -- as a
    #: worker that had died. An agent whose job is writing about failures
    #: will write the word constantly.
    _FAILURE_SIGNATURES = (
        r"API Error",
        r"Connection closed mid-response",
        r"rate limit",
        r"context left",
        r"Traceback \(most recent call last\)",
        r"command not found",
        # With a shell prefix ("zsh: killed  node ...") the word is
        # unambiguously the kill report, whatever follows it.
        r"^\s*[\w./-]+:\s*[Kk]illed\b",
        # Without one it must stand alone, name a process, or carry a signal.
        r"^\s*[Kk]illed\b(?:\s+process\b|\s*:?\s*\d*\s*$)",
        r"session ended",
        r"credit balance is too low",
    )

    # A prompt is not a failure, but it is just as fatal in a pane nobody is
    # watching: the agent is alive, printing, and will wait forever. Runtime
    # flags stop most of these being asked at all; this catches the ones a
    # future CLI invents.
    _PROMPT_SIGNATURES = (
        "Do you want to proceed",
        "Allow this",
        "approve this command",
        "[y/n]",
        "(y/N)",
        "Press enter to continue",
        "Waiting for approval",
    )

    def worker_prompts(self, worker_id: str, lines: int = 25) -> list[str]:
        """Interactive prompts visible in a worker's own output."""
        found: list[str] = []
        with contextlib.suppress(HelmError, OSError):
            for line in self.worker_output(worker_id, lines=lines):
                for signature in self._PROMPT_SIGNATURES:
                    if signature.lower() in line.lower() and line.strip() not in found:
                        found.append(line.strip()[:160])
                        break
        return found

    #: Terminal control sequences: CSI/OSC/DCS escapes, and the stray control
    #: bytes a TUI emits between them.
    _TERMINAL_NOISE = re.compile(
        r"\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)"   # OSC ... BEL or ST
        r"|\x1b[P_^][^\x1b]*\x1b\\"              # DCS/APC/PM ... ST
        r"|\x1b\[[0-9;?>=!]*[ -/]*[@-~]"          # CSI
        r"|\x1b[()*+,\-./][A-Za-z0-9@]"            # charset designation
        r"|\x1b[@-Z\\-_]"                          # other two-byte escapes
        r"|[\x00-\x08\x0b-\x1f\x7f]"              # stray control bytes
    )

    @classmethod
    def _readable(cls, line: str) -> str:
        """What a human would actually see on that line of the pane.

        A failure signature must be matched against TEXT, not against terminal
        control sequences. An interactive agent emits capability queries and
        cursor programming continuously, and matching raw bytes let one of
        those blobs be reported as a worker failure -- `[>0q+q4d73Gi=31337...`
        as the diagnosis for a reviewer that was working perfectly.

        It matters in both directions. The scan gets fewer false positives, and
        the line Helm SHOWS becomes something a human can read: an escape-soup
        "reason" is useless even on the occasions when the match is genuine.
        """
        return cls._TERMINAL_NOISE.sub("", line).strip()

    def worker_failures(self, worker_id: str, lines: int = 60) -> list[str]:
        """Failure signatures visible in a worker's own output."""
        found: list[str] = []
        with contextlib.suppress(HelmError, OSError):
            for raw in self.worker_output(worker_id, lines=lines):
                line = self._readable(raw)
                if not line:
                    continue
                for signature in self._FAILURE_SIGNATURES:
                    match = re.search(signature, line, re.IGNORECASE)
                    if match is None:
                        continue
                    # Report the MATCH with its surroundings, not the start of
                    # the line. An interactive agent redraws its whole pane
                    # without newlines, so "a line" can be a hundred thousand
                    # characters and the signature can sit sixty thousand in --
                    # a prefix then shows cursor programming and never the
                    # reason. The evidence has to contain the thing it is
                    # evidence of.
                    start = max(0, match.start() - 60)
                    excerpt = line[start:match.end() + 100].strip()
                    if start > 0:
                        excerpt = f"...{excerpt}"
                    if excerpt not in found:
                        found.append(excerpt)
                    break
        return found

    @staticmethod
    def _worker_last_message_at(worker: dict[str, Any]) -> str | None:
        """When the worker itself last pushed, ignoring Helm's own messages."""
        return worker.get("last_reported_at")

    LIVE_SILENCE_GRACE = 3.0

    #: A worker younger than this is never judged unhealthy: its first
    #: minutes are indistinguishable from death by every available signal,
    #: and every false verdict issued in that window today was acted on by
    #: something downstream.
    STARTUP_GRACE_SECONDS = 180.0

    def worker_health(
        self,
        *,
        silence_seconds: float | None = None,
        liveness: Callable[[dict[str, Any]], bool | None] | None = None,
    ) -> list[dict[str, Any]]:
        """Report every running worker's liveness without opening its UI.

        This is the check a human would otherwise perform by looking at each
        agent's pane. Helm owns it instead: the point of delegation is that
        nobody has to watch the workers.

        An idle log file is evidence, not a verdict. A worker Helm launched as
        a process is checked against its own pid, but a worker in a Herdr pane
        has no pid here and was judged on output alone -- so an agent thinking
        for six minutes was reported as stalled, which is how a healthy
        reviewer gets killed. `liveness` lets the caller that CAN ask the
        provider answer the question; core stays out of Herdr.

        It is deliberately not a blanket excuse for silence. A reviewer that
        ran for hours re-issuing one command was alive the whole time and was
        still broken, so being alive only buys a worker a short grace --
        `LIVE_SILENCE_GRACE` times the threshold -- after which it is reported
        however alive it is. Alive and silent for minutes is a long model
        call; alive and silent for an hour is a fault.
        """
        threshold = self.SILENCE_SECONDS if silence_seconds is None else silence_seconds
        data = self.store.load()
        report: list[dict[str, Any]] = []
        for worker in data.get("workers", {}).values():
            if worker.get("status") != "running":
                continue
            log_file = Path(worker["log_file"]) if worker.get("log_file") else None
            exit_file = Path(worker["exit_file"]) if worker.get("exit_file") else None
            output_idle: float | None = None
            if log_file is not None and log_file.exists():
                with contextlib.suppress(OSError):
                    output_idle = max(0.0, time.time() - log_file.stat().st_mtime)
            last_message = self._worker_last_message_at(worker)
            reported_idle: float | None = None
            if last_message:
                with contextlib.suppress(ValueError):
                    stamp = _dt.datetime.fromisoformat(last_message.replace("Z", "+00:00"))
                    reported_idle = max(
                        0.0, _dt.datetime.now(_dt.timezone.utc).timestamp() - stamp.timestamp()
                    )
            finished = exit_file is not None and exit_file.exists()
            # A process Helm started that is no longer there, with no exit
            # record to explain it, is dead however recently it spoke. Reading
            # liveness from its own messages called such a worker healthy for
            # as long as its last push stayed fresh -- and its last push is
            # necessarily fresh, because it died right after making it.
            vanished = (
                not finished
                and worker.get("execution") == "process"
                and not self._process_alive(worker.get("pid"))
            )
            # None means "could not tell", which must not read as either
            # alive or dead: an unavailable provider is not evidence.
            alive: bool | None = None
            if liveness is not None and not finished and worker.get("execution") != "process":
                with contextlib.suppress(Exception):
                    alive = liveness(worker)
            # Two restraints learned the expensive way, in one day:
            #
            # STARTUP GRACE. A worker's first minutes look exactly like death
            # to every signal here -- no output yet, no report yet, a pane
            # whose agent the provider has not recognised. Judging it in that
            # window produced the false "died" that a heal then acted on,
            # executing each new launch inside its first poll interval.
            #
            # PID-GRADE EVIDENCE. For a pane worker the provider's "not
            # alive" is an inference about recognition, not a process fact --
            # the runner-in-pane shape reads as unknown or dead while the
            # agent inside works, proposes gates, and posts blockers. So a
            # pane worker is called dead only when the provider says so AND it
            # has been silent past the threshold: silence corroborates,
            # activity acquits. Note RECENT activity. This once acquitted on
            # activity of any age, which meant a log file -- something every
            # launched worker has from its first second -- acquitted a pane
            # worker for ever, and the provider's verdict could never be
            # acted on at all. A foreman that was gone read as "stalled" for
            # six hours while messages to it were being recorded and
            # delivered nowhere.
            age_seconds: float | None = None
            with contextlib.suppress(Exception):
                started = worker.get("started_at") or worker.get("created_at")
                if started:
                    age_seconds = (
                        _dt_now() - _parse_iso(started)
                    ).total_seconds()
            in_grace = age_seconds is not None and age_seconds < self.STARTUP_GRACE_SECONDS
            recently_active = (
                output_idle is not None and output_idle <= threshold
            ) or (reported_idle is not None and reported_idle <= threshold)
            if alive is False and not finished and not in_grace:
                if worker.get("execution") == "process" or not recently_active:
                    vanished = True
            # An agent CLI keeps its session open after it finishes, so a
            # worker that has already delivered a terminal message is idle, not
            # stalled.  Calling that "attention" every time would train the
            # reader to ignore the list, which is the failure this whole check
            # exists to prevent.
            delivered = self.episode_outcome(data, worker) is not None
            # Paused on a human, not finished and not stuck. Its own session is
            # alive and correct to be idle, so every other signal here would
            # call it stalled or reported -- and neither says the thing the
            # commander has to act on.
            task_record = data.get("tasks", {}).get(worker["task_id"]) or {}
            hold = self.task_hold(task_record) or {}
            held = (
                hold.get("worker_id") == worker["id"]
                and hold.get("status") in {"waiting", "authorized-pending-delivery"}
            )
            stale_output = output_idle is not None and output_idle > threshold
            stale_reports = last_message is None or (
                reported_idle is not None and reported_idle > threshold
            )
            # A worker that asked and has not been answered is blocked on a
            # human, not working. It reports normally, so every other signal
            # says "healthy" -- which made an unanswered question look
            # identical to progress and stalled a task silently.
            asked_at = answered_at = -1
            for index, message in enumerate(data.get("messages", [])):
                if message.get("worker_id") != worker["id"]:
                    continue
                if message.get("kind") == "question":
                    asked_at = index
                elif message.get("kind") == "answer":
                    answered_at = index
            awaiting = asked_at > answered_at
            broke = self.worker_failures(worker["id"])
            if finished:
                # The process is already over; Helm simply has not caught up.
                verdict, detail = "finished", "process exited; poll to settle the record"
            elif vanished:
                verdict, detail = (
                    "died",
                    "its process is gone and it wrote no exit record; "
                    "any work it did is uncommitted in its worktree",
                )
                if task_record.get("read_only"):
                    # A read-only task's worktree is locked to every write, and
                    # some runtimes create a settings/cache directory in their
                    # own cwd on startup -- which is this worktree. Without this
                    # hint that failure looks identical to an ordinary crash, and
                    # the read-only lock is exactly the kind of thing a human
                    # investigating "why did my worker just die" would not think
                    # to suspect first.
                    detail += (
                        "; this task is read-only and its worktree has no write "
                        "permission at all -- if this runtime writes a "
                        "settings/cache directory into its own working "
                        "directory on startup, that write fails immediately "
                        "and can look exactly like this"
                    )
            elif not delivered and self.worker_prompts(worker["id"]):
                verdict, detail = (
                    "waiting-on-a-prompt",
                    "its own session is asking for confirmation and nobody is watching it",
                )
            elif broke and not delivered and stale_output:
                # Its own output says it failed, and it never reported. Left
                # unread this looks like healthy work for as long as the
                # session sits there. Only when the output has also gone
                # quiet, though: a session still writing past the failure
                # text has recovered from it, and the signature sitting in
                # scrollback re-flagged a healthy worker on every scan.
                verdict, detail = (
                    "erroring",
                    f"its output reports failure and it has not reported: {broke[-1]}",
                )
            elif held:
                pending = hold.get("status") == "authorized-pending-delivery"
                verdict, detail = (
                    "authorized-undelivered" if pending else "awaiting-approval",
                    (
                        f"{hold.get('action')} was authorized and has not reached it; "
                        "re-deliver with helm approval release"
                        if pending
                        else f"paused on {hold.get('action')}; the commander authorizes "
                        "it with helm approval release"
                    ),
                )
            elif awaiting:
                verdict, detail = (
                    "awaiting-answer",
                    "asked a question and is waiting; answer with helm worker answer",
                )
            elif delivered:
                verdict, detail = (
                    "reported",
                    "delivered a terminal message; session still open",
                )
            elif stale_output and stale_reports:
                within_grace = (
                    output_idle is not None
                    and output_idle <= threshold * self.LIVE_SILENCE_GRACE
                )
                if alive is True and within_grace:
                    verdict, detail = (
                        "working",
                        f"alive but silent for {int(output_idle)}s; the provider "
                        "confirms the session, so this is a long model call",
                    )
                else:
                    detail = (
                        f"no protocol message and no terminal output for {int(output_idle)}s"
                    )
                    if alive is True:
                        # Say what is known and stop there. Liveness rules out
                        # "it died"; it does NOT distinguish a wedged agent from
                        # one waiting on a slow model, and asserting "stuck"
                        # sends a reader to kill work that was merely thinking.
                        detail += "; the session is alive, so it is slow, wedged or looping, not gone"
                    verdict = "stalled"
            elif reported_idle is not None and reported_idle <= threshold:
                verdict, detail = "healthy", "reporting"
            elif last_message is None:
                # It has never reported, but its output is still moving and it
                # is inside the grace window: starting up, not stuck. Flagging
                # every new worker would make the attention list noise.
                verdict, detail = "starting", "running; no protocol message yet"
            elif output_idle is not None:
                # Its log IS moving, so it is alive and working; the only thing
                # missing is a protocol push. That is worth saying eventually --
                # a worker that never reports is indistinguishable from a dead
                # one -- but not at five minutes, when an agent is simply
                # mid-edit. Reusing the liveness grace keeps one rule for
                # "silent but demonstrably alive" instead of two.
                if reported_idle is not None and reported_idle <= threshold * self.LIVE_SILENCE_GRACE:
                    verdict, detail = (
                        "working",
                        f"producing output; no protocol message for "
                        f"{int(reported_idle)}s, still within the reporting grace",
                    )
                else:
                    verdict, detail = (
                        "quiet",
                        f"producing output but no protocol message for {int(reported_idle)}s",
                    )
            else:
                verdict, detail = "unknown", "no output log to read"
            role = (data.get("tasks", {}).get(worker["task_id"]) or {}).get("role", "worker")
            if role == "foreman" and verdict in {"quiet", "stalled"}:
                # A driver blocked on its own review or worker is doing exactly
                # its job. Reporting it as a fault trains the reader to ignore
                # the attention list, which is the same failure as filling that
                # list with healthy workers.
                driving = [
                    other
                    for other in data["workers"].values()
                    if other["id"] != worker["id"]
                    and other["project_id"] == worker["project_id"]
                    and other.get("status") == "running"
                ]
                if driving:
                    verdict, detail = (
                        "driving",
                        f"waiting on {len(driving)} running worker(s) it is driving",
                    )
            report.append({
                "worker_id": worker["id"],
                "task_id": worker["task_id"],
                "role": role,
                "project_id": worker["project_id"],
                "agent_id": worker.get("agent_id"),
                "execution": worker.get("execution"),
                "verdict": verdict,
                "detail": detail,
                "output_idle_seconds": output_idle,
                "reported_idle_seconds": reported_idle,
                "nudged_at": worker.get("nudged_at"),
            })
        # Foremen first. A stalled worker costs one task; a stalled foreman
        # costs everything that project was going to do next, because it is
        # the thing that would have noticed the stalled worker.
        return sorted(
            report, key=lambda entry: (entry["role"] != "foreman", entry["worker_id"])
        )

    def sweep_workers(self, *, silence_seconds: float | None = None) -> list[dict[str, Any]]:
        """Settle finished workers and surface the ones that need attention.

        Repair is limited to what is unambiguous: a worker whose process has
        already exited is polled so its task leaves `running`. A stalled worker
        is reported, never silently failed -- its pane is the evidence.
        """
        threshold = self.SILENCE_SECONDS if silence_seconds is None else silence_seconds
        report = self.worker_health(silence_seconds=silence_seconds)
        for entry in report:
            if entry["verdict"] == "finished":
                with contextlib.suppress(HelmError, SafetyError, OSError):
                    worker = self.poll_worker(entry["worker_id"])
                    entry["detail"] = f"settled to {worker['status']}"
                    entry["verdict"] = "settled"
            elif entry["verdict"] == "reported" and (entry["output_idle_seconds"] or 0) > threshold:
                # It said it was done and its session has gone quiet. Waiting
                # for a process that may never exit just strands the task.
                with contextlib.suppress(HelmError, SafetyError, OSError):
                    worker = self.settle_reported_worker(entry["worker_id"])
                    entry["detail"] = f"settled to {worker['status']} on its own terminal message"
                    entry["verdict"] = "settled"
        return report

    # The messages that end a worker's assignment. `approval-needed` is
    # deliberately not one of them: it is a gate, and settling the worker on it
    # marked a live agent failed, refused every further message it tried to
    # push, and left the task with no supported way back to running -- so the
    # outcome of the very action a human authorized could never be recorded.
    _TERMINAL_MESSAGE_TASK_STATE = {
        "result": "completed",
        "blocker": "blocked",
        "failure": "failed",
    }

    @classmethod
    def _ends_the_assignment(cls, task: dict[str, Any], kind: str) -> bool:
        """Whether a terminal-looking message actually ends this worker.

        A `blocker` ends a WORKER's assignment, and should: it could not do the
        one thing it was made for, so the answer is a new task rather than a
        revived one.

        It must not end a FOREMAN's. A foreman is a long-lived driver whose
        entire job is to meet obstacles and escalate them, and `blocker` is the
        only verb it has for "I need something from you" -- so reporting one
        killed the reporter. Every foreman death on this root that recorded an
        outcome was a blocker, twenty-two of them: the driver did exactly what
        the protocol asks of it, told the root immediately instead of retrying
        silently, and was settled for the telling. The project was then left
        with no driver until someone noticed, appointed a replacement and
        re-briefed it from nothing, which repeatedly cost more than the
        obstacle had. One reported a corrupt worktree in the same breath as
        "I am telling you immediately rather than retrying silently", and died
        of it.

        So a foreman's blocker PAUSES, exactly as `approval-needed` already
        does: the task shows blocked so `open_escalations` still surfaces it,
        the session stays live and addressable, and an answer resumes it.
        """
        if kind not in cls._TERMINAL_MESSAGE_TASK_STATE:
            return False
        return not (kind == "blocker" and task.get("role") == "foreman")

    def settle_reported_worker(self, worker_id: str) -> dict[str, Any]:
        """End a task on the worker's own terminal message.

        The protocol says a `result`, `blocker`, or `failure` finishes the
        work, but Helm was waiting on process exit instead -- so an agent CLI
        that reports and then keeps its session open left the task in
        `running` forever, and a session killed with its pane never wrote an
        exit record at all. The worker's word is the terminal signal; the
        process merely hosts it.
        """
        with self.store.locked() as data:
            worker = data["workers"].get(worker_id)
            if worker is None:
                raise HelmError(f"unknown worker: {worker_id}")
            if worker["status"] != "running":
                return worker
            task = self._task(data, worker["task_id"])
            project = self._project(data, worker["project_id"])
            kind = self.episode_outcome(data, worker)
            if kind is None:
                # Including a reopened worker whose previous round reported:
                # settling this round on last round's verdict would be the same
                # fabrication as inventing one.
                raise HelmError(
                    "worker has not delivered a terminal message; nothing to settle"
                )
            worker["status"] = "completed" if kind == "result" else "failed"
            worker["exit_code"] = 0 if kind == "result" else 1
            worker["protocol_outcome"] = kind
            worker["outcome_source"] = "protocol"
            worker["ended_at"] = now()
            if task["status"] in {"created", "allocated", "running"}:
                task["status"] = self._TERMINAL_MESSAGE_TASK_STATE[kind]
            self._message(
                data,
                project,
                task,
                worker,
                "status",
                f"Settled on the worker's own {kind} message; its session is gone or idle",
                {"status": task["status"]},
            )
            settled = dict(worker)
        # Captured after the lock is released and before anything that holds
        # the diagnosis can close. Ordering is the point: a tab released before
        # its evidence is written loses the reason permanently.
        with contextlib.suppress(HelmError, OSError):
            self.capture_evidence(worker_id)
        return settled

    _BOARD_STATES = {
        "running": ("working", "amber"),
        "blocked": ("blocked", "red"),
        "failed": ("failed", "red"),
        "approval-needed": ("needs you", "amber"),
        "completed": ("ready to review", "amber"),
        "approved": ("approved, not merged", "amber"),
        "pr-open": ("PR open", "amber"),
        "pr-merged": ("PR merged", "green"),
        "merged": ("landed", "green"),
    }

    def board(self, *, limit_per_project: int = 8) -> list[dict[str, Any]]:
        """Every project's work, in the shape a human wants to look at.

        A task worktree isolates work correctly and hides it completely. The
        result of an agent's afternoon is a branch and a file nobody can see
        without knowing the path. This collects what each task produced so it
        can be shown rather than described.
        """
        data = self.store.load()
        out: list[dict[str, Any]] = []
        for project in sorted(
            data.get("projects", {}).values(), key=lambda p: p["id"]
        ):
            tasks: list[dict[str, Any]] = []
            ordered = sorted(
                (
                    t
                    for t in data.get("tasks", {}).values()
                    if t["project_id"] == project["id"]
                    # The board answers "what did the agents produce". A
                    # foreman produces no branch and no artifact, so a card
                    # for it is a card with nothing on it.
                    and t.get("role") != "foreman"
                ),
                key=lambda t: t.get("created_at", ""),
                reverse=True,
            )
            for task in ordered[:limit_per_project]:
                label, tone = self._BOARD_STATES.get(task["status"], (task["status"], "grey"))
                entry: dict[str, Any] = {
                    "id": task["id"],
                    "brief": _safe_text(task.get("brief", "")).strip().splitlines()[0][:180],
                    "status": task["status"],
                    "label": label,
                    "tone": tone,
                    "agent": task.get("agent_id"),
                    "domain": task.get("domain"),
                    "branch": task.get("branch"),
                    "result": "",
                    "artifacts": [],
                    "diffstat": [],
                }
                for message in reversed(data.get("messages", [])):
                    if message.get("task_id") == task["id"] and message.get("kind") in {
                        "result", "blocker", "failure"
                    }:
                        entry["result"] = _safe_text(message.get("text", ""))[:700]
                        break
                with contextlib.suppress(HelmError, OSError, SafetyError):
                    outcome = self.task_outcome(task["id"])
                    entry["diffstat"] = outcome["diffstat"][-6:]
                    workspace = Path(outcome["workspace"])
                    for artifact in outcome["artifacts"]:
                        path = workspace / artifact["path"]
                        entry["artifacts"].append({
                            "path": artifact["path"],
                            "abs": str(path),
                            "exists": path.exists(),
                            "kind": path.suffix.lower().lstrip("."),
                        })
                    entry["workspace"] = outcome["workspace"]
                tasks.append(entry)
            if tasks:
                out.append({
                    "id": project["id"],
                    "name": project.get("name", project["id"]),
                    "glyph": project_glyph(project.get("color", "")),
                    "color": project.get("color", "#888888"),
                    "tasks": tasks,
                })
        return out

    SITUATION_KEPT = 12
    # Long enough for a decision and its reason in one line; short enough that
    # the record stays scannable and nobody is tempted to keep a document in
    # it. Exceeding it is an error, never a trim -- see record_situation.
    SITUATION_LINE_LIMIT = 800

    #: Worker pushes that end a piece of work rather than narrate it.
    TERMINAL_REPORT_KINDS = frozenset(
        {"result", "blocker", "failure", "approval-needed"}
    )

    @staticmethod
    def _project_wants_review(project: dict[str, Any]) -> bool:
        """Whether this project runs the independent-review loop. Default: yes.

        The record is preferred, then the project's own file, then the
        default -- the same freshness order as the foreman flag. Static and
        fed the already-loaded project so the reviewer-task refusal can run
        under the state lock without loading the store twice.
        """
        if isinstance(project.get("review"), bool):
            return project["review"]
        root = canonical(project["root"])
        if not (root / ".helm" / "project.json").exists():
            return True
        with contextlib.suppress(HelmError, SafetyError, OSError):
            return bool(_discovery_settings(root).get("review", True))
        return True

    # How long a stopped worker gets to exit on its own signal before it is
    # killed outright. Long enough for an agent CLI to flush its output --
    # that log is the evidence for why the task was abandoned -- and short
    # enough that stopping never feels like hanging.
    STOP_GRACE_SECONDS = 5.0

    #: Task states that still need a driver. Anything else is either finished
    #: or waiting on a human, and neither needs a foreman sitting on it.
    _DRIVEN_TASK_STATES = frozenset(
        {"created", "allocated", "running", "blocked", "pr-open"}
    )
