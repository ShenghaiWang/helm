"""Local Helm coordinator state and safety checks.

The coordinator deliberately keeps worker input narrow: a worker gets one
context document and one worktree. Worker output is recorded as data and only
moves a task through the small set of non-approval states defined here.
"""
from __future__ import annotations

import contextlib
import copy
import datetime as _dt
import fcntl
import hashlib
import json
import os
import re
import shlex
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import time
import uuid
from pathlib import Path
from typing import Any, Callable, Iterator, Sequence

from . import models
from . import preferences as prefs
from . import runtimes
from .errors import HelmError, SafetyError
from .paths import (
    _safe_configuration_path,
    _file_digest,
    _private_dir,
    _private_file,
    _write_private_text,
    canonical,
    inside,
    overlaps,
)
from .state import SCHEMA_VERSION, SUPPORTED_SCHEMA_VERSIONS, StateStore, _migrate_state
from .values import (
    BLOCKING_GATE_KINDS,
    COMMANDER_ASK_KIND,
    COMMANDER_ASK_REASONS,
    RUNTIME_DEFAULT_MODEL,
    HOLD_OPEN_STATUSES,
    HOLD_STATUSES,
    HOLD_TASK_STATUS,
    HOLD_TRANSITIONS,
    TASK_STATUSES,
    _color_for,
    LEARNING_EVIDENCE_KINDS,
    LEARNING_PROPOSAL_STATUSES,
    _dt_now,
    _parse_iso,
    PORTABLE_SKILL_ROOT,
    RUNTIME_SKILL_ROOTS,
    SKILL_CONTENT_LIMIT,
    SKILL_MANIFEST,
    SKILL_SELECTION_LIMIT,
    SKILL_TOTAL_LIMIT,
    TASK_ROLES,
    _MAX_DOMAIN_DEPTH,
    _ROLE_DIRECTORY,
    _SKILL_STOPWORDS,
    DELIVERED_TASK_STATES,
    DELIVERY_DECISION_KIND,
    DELIVERY_DECISION_PROJECT_TEXT,
    DELIVERY_DECISION_TASK_TEXT,
    DELIVERY_POLICIES,
    EFFORT_LEVELS,
    FAILURE_ACTION_KIND,
    FINALIZATION_ACTION_KIND,
    FOLLOW_UP_ACTION_KIND,
    FOREMAN_DOMAIN,
    FOREMAN_RULES,
    GATE_ACTION_KINDS,
    GATE_TYPES,
    HEALTHY_WORKER_VERDICTS,
    PROTECTED_ACTIONS,
    REQUIREMENT_GATE_KIND,
    SAFE_TEXT_LIMIT,
    SOLUTION_GATE_KIND,
    WORKTREELESS_ROLES,
    _COLOR_PALETTE,
    _PALETTE_GLYPHS,
    _SAFE_TEXT_NOTICE,
    _TERMINAL_WORKER_TASK_STATES,
    _safe_text,
    _string_list,
    _validate_agent_id,
    _validate_branch_name,
    _validate_domain_id,
    _validate_effort,
    _validate_model_id,
    _validate_project_id,
    _validate_protected_action,
    _validate_ticket_id,
    _words,
    finalization_text,
    new_id,
    now,
    project_glyph,
    task_branch_name,
    task_owns_branch,
)
from .git import (
    CHECKOUT_OPERATION_MARKERS,
    _StaleBaseResolution,
    _advance_tracking_ref_without_rewinding,
    _bounded_ls_remote,
    _delete_ref,
    _git,
    _git_common_dir,
    _git_root,
    _has_head,
    _project_checkout_conflict,
    _remote_has_branch,
    _remote_is_empty,
    _remote_symbolic_default,
    _repository_default_branch,
    _resolve_base_branch,
    _resolve_task_base,
    base_after_merges,
)
from .discovery import _discovery_settings, _launch_runtime_id, _parse_frontmatter
from .policy import CORE_SAFETY_RULES
from .launching import (
    _TRUST_CONFIGS,
    _command_executable_available,
    _pretrust_workspace,
    worker_environment,
)
from .authority import AUTHORITY_ENV, Authority
from .processes import _process_parents, _scan_worker_pid
from .coordinator.base import CoordinatorBase
from .coordinator.agents import AgentsMixin
from .coordinator.archive import ArchiveMixin
from .coordinator.pull_requests import PullRequestsMixin
from .coordinator.ledger import LedgerMixin
from .coordinator.adopt import AdoptMixin
from .coordinator.knowledge import KnowledgeMixin
from .coordinator.tidy import TidyMixin
from .coordinator.caller import CallerMixin
from .coordinator.decisions import DecisionsMixin
from .coordinator.lifecycle import LifecycleMixin
from .coordinator.protection import ProtectionMixin
from .coordinator.foremen import ForemenMixin
from .coordinator.gates import GatesMixin
from .coordinator.health import HealthMixin
from .coordinator.launch import LaunchMixin
from .coordinator.learning import LearningMixin
from .coordinator.skills import SkillsMixin
from .coordinator.workers import WorkersMixin
from .coordinator.status import StatusMixin



class Coordinator(
    ArchiveMixin,
    PullRequestsMixin,
    LedgerMixin,
    AdoptMixin,
    KnowledgeMixin,
    TidyMixin,
    ProtectionMixin,
    LifecycleMixin,
    AgentsMixin,
    HealthMixin,
    LaunchMixin,
    LearningMixin,
    SkillsMixin,
    StatusMixin,
    GatesMixin,
    DecisionsMixin,
    ForemenMixin,
    WorkersMixin,
    CallerMixin,
    CoordinatorBase,
):

    def use_preferences(self, preferences: prefs.Preferences | None) -> None:
        """Read preferences from `preferences` instead of resolving them.

        `preferences_path` honours `HELM_PREFERENCES_FILE`, which is right for
        Helm at large and wrong for a caller whose contract is that it opens
        only what the root's layout names. Such a caller resolves the file
        itself, structurally, and pins the result here -- otherwise a redirect
        it explicitly refused would still be followed by any method that
        happens to consult preferences on its behalf, which is a guarantee that
        holds only until the next call is added.
        """
        self._preferences_source = preferences

    def initialize_root(self, root: str | os.PathLike[str]) -> Path:
        """Initialize the configured root layout through the coordinator API."""
        return self.store.initialize_root(root)

    # ---------- project and task records ----------


    #: Moved to `helm.paths`; the alias keeps `Coordinator._safe_configuration_path`
    #: and `self._safe_configuration_path` naming the very same function object.
    _safe_configuration_path = staticmethod(_safe_configuration_path)

    # ---------- isolation ----------



    # ---------- approval holds ----------

    #: The one worker message that pauses a task instead of ending it.
    HOLD_MESSAGE_KIND = "approval-needed"
    #: Helm's own inbound push -- a reply to a worker's question, or a request
    #: `helm route` hands to a foreman. Named because "is this message one the
    #: worker sent, or one it was sent" decides both liveness and pendingness.
    ANSWER_MESSAGE_KIND = "answer"

    @staticmethod
    def _content_digest(path: Path) -> str:
        """A stable digest of one path's bytes, or a refusal.

        Fail-closed on purpose. A snapshot that quietly recorded "unreadable"
        would compare equal to the next unreadable reading, which is exactly
        how an unbound file becomes an unbound authorization.
        """
        try:
            if path.is_symlink():
                return "symlink:" + hashlib.sha256(
                    os.readlink(path).encode("utf-8")
                ).hexdigest()
            if path.is_dir():
                return "dir"
            if not path.exists():
                return "absent"
            digest = hashlib.sha256()
            with path.open("rb") as stream:
                for chunk in iter(lambda: stream.read(1 << 20), b""):
                    digest.update(chunk)
            return f"sha256:{digest.hexdigest()}"
        except OSError as exc:
            raise SafetyError(f"cannot bind {path}: {exc}") from exc







    def _record_artifact(
        self,
        data: dict[str, Any],
        project: dict[str, Any],
        task: dict[str, Any],
        worker: dict[str, Any],
        payload: dict[str, Any],
    ) -> None:
        raw_path = payload.get("path")
        if not raw_path:
            self._message(data, project, task, worker, "artifact-rejected", "Artifact has no path", {})
            return
        workspace = self._verify_workspace_record(data, project, task)
        # THE WORKER'S OWN DIRECTORY COUNTS TOO, and leaving it out made two
        # shipped rules contradict each other. Helm's reporting document tells
        # every worker to report each file it produces with --type artifact
        # --path; the verification guidance tells it to write reports and logs
        # into its OWN directory and never into the worktree, because an
        # untracked file there either pollutes the branch or blocks approval.
        # Obey both and you produce exactly what this check rejected: one
        # worker failed five artifact calls in four seconds doing as it was
        # told.
        #
        # This is not a hole in isolation. The worker directory is Helm's own
        # state, created by Helm for that one worker, and already handed to the
        # runtime as an added directory -- it is the place Helm told the worker
        # to write. What isolation forbids is reaching into ANOTHER task's
        # worktree or another project, and neither root permits that.
        roots = [workspace]
        worker_dir = self.store.directory / "workers" / str(worker.get("id") or "")
        with contextlib.suppress(OSError):
            roots.append(canonical(worker_dir))
        candidate = canonical(raw_path) if os.path.isabs(str(raw_path)) else canonical(workspace / str(raw_path))
        home = next((root for root in roots if inside(candidate, root)), None)
        if home is None or not candidate.is_file():
            self._message(
                data,
                project,
                task,
                worker,
                "artifact-rejected",
                "Artifact path is outside the assigned workspace and this worker's "
                "own directory, or does not exist",
                {"path": _safe_text(raw_path)},
            )
            return
        relative = candidate.relative_to(home).as_posix()
        artifact = {
            "id": new_id("a"),
            "project_id": project["id"],
            "task_id": task["id"],
            "worker_id": worker["id"],
            "path": relative,
            "workspace": str(workspace),
            "description": _safe_text(payload.get("description", "")),
            "kind": _safe_text(payload.get("kind", "file")),
            "created_at": now(),
        }
        data["artifacts"].append(artifact)

    #: Worker message kinds Helm accepts, on either intake path.
    WORKER_MESSAGE_KINDS = frozenset({
        "status", "result", "blocker", "failure", "approval-needed", "artifact",
        "question", "answer",
    })

    #: The one kind in WORKER_MESSAGE_KINDS that the worker did not send --
    #: `answer` is Helm's own reply to a worker's question, or (via `helm
    #: route`) a request handed to a foreman's task. Recorded on the same
    #: worker/task for the same durable-record reasons as everything else
    #: here, but it must never refresh `last_reported_at`: that field is the
    #: worker's own liveness signal (`_worker_last_message_at` -- "when the
    #: worker itself last pushed, ignoring Helm's own messages"), and an
    #: outbound push is not evidence the worker is alive to receive it, let
    #: alone that it did. Treating it as such let a request delivered to a
    #: pane that had actually just died read as freshly "healthy".
    _COORDINATOR_ORIGINATED_MESSAGE_KINDS = frozenset({"answer"})

    @staticmethod
    def _receipts(payload: dict[str, Any]) -> list[Any]:
        """Post-action evidence a worker reported: remote ids, URLs, tracker refs.

        Deliberately outcome data. It is recorded beside the hold and never
        compared against the pre-action snapshot -- a publish that writes its own
        receipt used to invalidate the very authorization it had just satisfied.
        """
        for key in ("receipt", "receipts"):
            value = payload.get(key)
            if isinstance(value, list):
                return [_safe_text(entry)[:300] if isinstance(entry, str) else entry for entry in value]
            if value not in (None, "", {}, []):
                return [_safe_text(value)[:300] if isinstance(value, str) else value]
        return []


    #: What the record says when a hold outlived the session that asked for it.
    #: One phrasing, because the reconciliation below compares against it.
    _HOLD_ABANDONED_NOTE = (
        "approval hold abandoned: its session ended before the authorization "
        "was used"
    )




    @staticmethod
    def _noop_event(
        task: dict[str, Any], worker: dict[str, Any], kind: str
    ) -> dict[str, Any]:
        """An event that moved nothing, in the shape the effects pass expects.

        Every side effect is off: an event Helm deliberately did not record
        must not raise a second action item, a second situation line, a second
        learning proposal or a second delivery decision.
        """
        return {
            "kind": kind,
            "task_id": task["id"],
            "project_id": task["project_id"],
            "task_id": task["id"],
            "worker_id": worker["id"],
            "role": task.get("role", "worker"),
            "task_status": task.get("status"),
            "hold_id": None,
            "hold_status": None,
            "hold_event": "",
            "terminal": False,
            "situation": None,
            "source": "worker",
            "action_item": None,
            "action_item_key": None,
            "resolve_key": None,
            "capture_evidence": False,
            "learning": False,
            "delivery_decision_task": None,
            "delivery_decision_project": False,
        }

    def _ingest_worker_event(
        self,
        data: dict[str, Any],
        worker: dict[str, Any],
        kind: str,
        text: str,
        payload: dict[str, Any],
        requested_status: str | None,
        *,
        late: bool = False,
    ) -> dict[str, Any]:
        """Record one worker event and move everything it moves. Call under lock.

        `late` marks an event that lost the lock race with its own process
        exit: the worker was already settled by observation alone, and this is
        the word it was trying to get out. See docs/worker-lifecycle.md.

        The single intake point for both paths: a `helm worker message` push and
        a JSON protocol line parsed out of a worker's stdout. They used to be
        two copies with different behavior, which is why the process fallback
        silently skipped hold settlement, the approval action item, evidence,
        and the project's own record. Returns the metadata the unlocked
        side-effect pass needs, so nothing has to be recomputed or remembered.
        """
        task = self._task(data, worker["task_id"])
        project = self._project(data, worker["project_id"])
        settled = self.terminal_protocol_outcome(worker)
        if (
            settled is not None
            and kind in self._TERMINAL_MESSAGE_TASK_STATE
            and worker.get("status") != "running"
        ):
            # The worker has already given its verdict. Saying it again -- a
            # duplicate push, or the same line read twice -- must record
            # nothing new, and a *different* second verdict must not overwrite
            # the first: first word wins, and the disagreement is kept once as
            # evidence. Both routes reach here, so a duplicate JSON line in one
            # poll is covered as well as a duplicate push.
            if kind != settled and not worker.get("protocol_conflict_recorded"):
                worker["protocol_conflict_recorded"] = True
                self._message(
                    data, project, task, worker, "protocol-conflict",
                    f"Worker reported {kind} after {settled}; the {settled} "
                    f"stands as the task outcome",
                    {"outcome": settled, "conflicting": kind, "text": _safe_text(text)},
                )
            return self._noop_event(task, worker, kind)
        message = self._message(
            data, project, task, worker, kind, text, payload, status=requested_status
        )
        receipts = self._receipts(payload)
        hold_event = ""
        if kind == "artifact":
            self._record_artifact(data, project, task, worker, payload)
        elif kind == self.HOLD_MESSAGE_KIND:
            hold = self._hold_request(data, project, task, worker, message, payload)
            hold_event = "request"
        else:
            self._transition_from_message(task, kind, requested_status)
            hold = self.task_hold(task)
            if hold is not None:
                hold_event = self._resolve_hold_on_event(
                    data, project, task, worker, hold, kind, receipts
                )
        if self._ends_the_assignment(task, kind):
            # The message is terminal even when the provider process or pane
            # stays open. Mark only the worker/task lifecycle here; approval,
            # merge, publish, and other protected actions remain separate
            # coordinator-controlled operations.
            worker["status"] = "completed" if kind == "result" else "failed"
            worker["exit_code"] = 0 if kind == "result" else 1
            worker["ended_at"] = now()
            # The authoritative record, and the only writer of it: a synthesized
            # fallback message never sets this. Arriving after a process exit
            # overrides the fallback outcome without rewriting the observation.
            worker["protocol_outcome"] = kind
            worker["outcome_source"] = "protocol"
            if late:
                task["status"] = self._TERMINAL_MESSAGE_TASK_STATE[kind]
            self._exit_mismatch_evidence(data, project, task, worker, kind)
        if kind not in self._COORDINATOR_ORIGINATED_MESSAGE_KINDS:
            worker["last_reported_at"] = now()
        latest = self.latest_hold(task)
        return self._event_metadata(
            task, worker, kind, text, payload, hold_event, latest, data
        )


    def _event_metadata(
        self,
        task: dict[str, Any],
        worker: dict[str, Any],
        kind: str,
        text: str,
        payload: dict[str, Any],
        hold_event: str,
        hold: dict[str, Any] | None,
        data: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Everything the unlocked side-effect pass needs, computed once.

        Computed here, under the state lock, and applied afterwards without it.
        The project's record has its own file lock, and taking it while holding
        the state lock inverts the order every other writer acquires them in --
        so what reaches that record is *decided* here and *written* there.
        """
        role = task.get("role")
        is_foreman = role == "foreman"
        summary = _safe_text(text).strip()[:900]
        # A non-foreman result is the outcome somebody still has to decide
        # about, so it is named as such rather than filed as one more summary.
        source = (
            "Approval request"
            if kind == self.HOLD_MESSAGE_KIND
            else "Foreman report"
            if is_foreman
            else "Worker result"
            if kind == "result"
            else "Worker failure"
            if kind == "failure"
            else "Worker summary"
        )
        worth_recording = bool(summary) and (
            (is_foreman and kind in {"result", "blocker", "failure", "approval-needed"})
            # A worker's own final result. Without this the one message that
            # says what was actually produced reached only a pane that the
            # clean-result path is about to release.
            or (not is_foreman and kind == "result")
            # And its failure. This used to be left out on the grounds that a
            # blocker or failure "already reaches the commander as captured
            # evidence and an attention entry" -- true of a blocker, which
            # `open_escalations` lists, and false of a failure, which lists
            # nowhere. A failed task produced no situation line, no action
            # item and no escalation: it showed only inside `helm project
            # status`, under a heading nothing points at. Work that died
            # looked exactly like work nobody had started.
            or (not is_foreman and kind == "failure")
            or (kind == "status" and self._summary_payload(payload))
            # A gate opening or resolving is the one thing nobody should have to
            # go looking for, on either intake path.
            or hold_event in {"request", "outcome", "invalidate", "abandon"}
        )
        action_item = None
        if worth_recording:
            action_item = (
                self._action_item_from_payload(payload)
                or self._action_item_from_summary(summary)
            )
            if kind == "failure" and not action_item:
                # A failure is a decision the commander owns -- retry, another
                # round, or cleanup -- and it must not depend on the worker
                # having phrased its report with a marker word that
                # `_action_item_from_summary` happens to recognise. Deriving it
                # from the wording is a denylist, and the failure nobody
                # phrased conveniently is the one that disappears.
                action_item = f"Task failed and needs a decision: {summary}"
            if kind == self.HOLD_MESSAGE_KIND and not action_item:
                # A task paused on the commander is the definition of a
                # follow-up item; it must not depend on the worker having
                # phrased its request with a marker word.
                action_item = f"Authorize or refuse: {summary}"
        return {
            "kind": kind,
            "task_id": task["id"],
            "project_id": task["project_id"],
            "worker_id": worker["id"],
            "role": role,
            "task_status": task.get("status"),
            "hold_id": (hold or {}).get("id"),
            "hold_status": (hold or {}).get("status"),
            "hold_event": hold_event,
            "terminal": kind in self._TERMINAL_MESSAGE_TASK_STATE,
            # Trimmed to fit rather than built and hoped for. `record_situation`
            # refuses an over-long line, and the effects pass suppresses that
            # refusal -- so a long final summary was dropped from the durable
            # record silently, which is the exact disappearance this gate
            # exists to prevent. The full text stays in the message record.
            "situation": (
                self._situation_line(
                    f"{source}: task {task['id']} [{task.get('status')}] ", summary
                )
                if worth_recording
                else None
            ),
            # A terminal report is the commander's to hear, not to discover. It
            # is the answer to "what came of it", so it is never one of the
            # older lines a busy project's backlog quietly marks seen -- five
            # dry research rounds went into one project's record exactly that
            # way, and read as silence. Routine `status` pushes stay ordinary:
            # surfacing everything is the same failure as surfacing nothing.
            "situation_surface": (
                kind in self.TERMINAL_REPORT_KINDS or hold_event == "request"
            ),
            "source": source,
            "action_item": action_item,
            # The commander's attention item is keyed to the hold, so resolving
            # it is possible at all: an unkeyed "Authorize or refuse" line stayed
            # open forever after the action succeeded.
            "action_item_key": (hold or {}).get("id"),
            "resolve_key": (
                (hold or {}).get("id")
                if hold_event in {"outcome", "invalidate", "abandon"}
                else None
            ),
            "capture_evidence": (
                kind in self._TERMINAL_MESSAGE_TASK_STATE or hold_event == "request"
            ),
            "learning": kind == "result",
            # The gate, decided from the state this event has just changed.
            # A worker's result with no live foreman leaves work nobody is
            # driving, so it names that task. A foreman's own terminal report
            # means the driver itself has stopped, so everything still
            # undelivered in the project is now the commander's to decide.
            # Deliberately independent of `worth_recording`: a report with an
            # empty or unreadable summary still leaves a decision behind.
            "delivery_decision_task": (
                task["id"]
                if (
                    not is_foreman
                    and kind == "result"
                    and data is not None
                    and self._live_foreman_in(data, task["project_id"]) is None
                )
                else None
            ),
            "delivery_decision_project": (
                is_foreman and kind in self.TERMINAL_REPORT_KINDS
            ),
        }

    def _apply_event_effects(self, event: dict[str, Any]) -> None:
        """The durable, unlocked half of one worker event.

        Runs outside the state lock because it writes the project's own record
        and can raise learning proposals, which take the lock themselves.
        """
        project_id = event["project_id"]
        if event["situation"]:
            with contextlib.suppress(HelmError, OSError):
                self.record_situation(
                    project_id,
                    event["situation"],
                    surface=event.get("situation_surface", False),
                    task_id=event.get("task_id", ""),
                )
        if event["action_item"]:
            with contextlib.suppress(HelmError, OSError):
                self.record_project_action_item(
                    project_id,
                    event["action_item"],
                    source=event["source"],
                    task_id=event["task_id"],
                    key=event["action_item_key"],
                )
        if event["resolve_key"]:
            with contextlib.suppress(HelmError, OSError):
                self.resolve_project_action_items(project_id, event["resolve_key"])
        # Reconcile the pause against what the state says *now*, because these
        # effects ran with the lock released and the hold may have been
        # abandoned in between.
        self._reconcile_hold_attention(event)
        if event["capture_evidence"]:
            # Preserve terminal output as evidence before a later cleanup or
            # provider teardown can remove its pane/log. A pause is captured
            # too: what the session was doing when it stopped to ask is exactly
            # what the person deciding needs to see.
            with contextlib.suppress(HelmError, OSError):
                self.capture_evidence(event["worker_id"])
        if event["learning"]:
            # A finished task is the evidence the learning flow wants, and
            # asking a coordinator to remember to harvest it made knowledge
            # depend on memory -- which is the failure every other rule here
            # was moved into code to avoid. Proposals are inert: they still
            # cannot approve, apply, or teach anything by themselves.
            with contextlib.suppress(HelmError, SafetyError, OSError):
                self.generate_learning_proposals(event["task_id"])
        # The gate itself, written here rather than under the state lock. It
        # runs before any tab release or space close, because those are driven
        # by the same terminal message and a worker's confirmation prints onto
        # the very pane about to be removed.
        if event["delivery_decision_task"]:
            with contextlib.suppress(HelmError, OSError):
                self.record_delivery_decision(
                    project_id,
                    task_id=event["delivery_decision_task"],
                    source=event["source"],
                )
        if event["delivery_decision_project"]:
            with contextlib.suppress(HelmError, OSError):
                self.raise_delivery_decision_for_project(
                    project_id, source=event["source"]
                )


    def record_task_evidence(
        self,
        task_id: str,
        *,
        tip: str,
        command: str,
        exit_code: int,
        detail: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Record a suite result as the evidence a reviewer actually reads.

        The review pipeline reads one structured field. Reporting the same
        facts as prose produces no evidence at all, and the failure is silent
        both ways: the author believes it reported, the reviewer sees nothing
        newer than the last payload and rejects the change for stale
        evidence. Two consecutive reviews were spent that way on a suite that
        had in fact run green. So the shape stops being something a worker has
        to remember and becomes something Helm builds.
        """
        tip = _safe_text(tip).strip()
        command = _safe_text(command).strip()
        if not tip:
            raise HelmError("evidence needs the tip its suite ran against")
        if not command:
            raise HelmError("evidence needs the command that produced it")
        report = {
            "tip": tip,
            "command": command,
            "exit": int(exit_code),
            "recorded_at": now(),
        }
        if detail:
            report.update(detail)
        with self.store.locked() as data:
            task = self._task(data, task_id)
            workers = sorted(
                self._task_workers(data, task_id),
                key=lambda w: (w.get("started_at") or "", w.get("id") or ""),
            )
            if not workers:
                raise HelmError(
                    f"task {task_id} has no worker to attribute this evidence to"
                )
            worker_id = workers[-1]["id"]
            # A COMPLETE MESSAGE, built the same way every other one is.
            # This used to append a hand-rolled dict with NO `id` and an `at`
            # where the rest of the store writes `created_at`, and two separate
            # consumers have now broken on it: a reader that keyed on
            # `created_at` saw an undated record, and the pane router did
            # `message["id"]` and took the whole launcher down with a KeyError
            # mid-round -- traceback to stdout, exit code that read as success.
            #
            # Hand-building a record next to a builder that exists is how a
            # field gets forgotten. Every consumer is entitled to assume the
            # shape, so the shape is not optional.
            data.setdefault("messages", []).append(
                {
                    "id": new_id("m"),
                    "worker_id": worker_id,
                    "task_id": task_id,
                    "project_id": task["project_id"],
                    "kind": "status",
                    "text": f"full suite at {tip}: {command} exited {int(exit_code)}",
                    "payload": {"full_suite": report},
                    "created_at": now(),
                    "status": None,
                }
            )
        return report

    #: How close together two answers to one worker have to be before Helm
    #: reads them as two drivers racing rather than as a deliberate follow-up.
    #: Wide enough to catch the real case -- a root and a foreman both replying
    #: to the same question within a minute -- and narrow enough that pushing a
    #: new instruction to a worker minutes later is still ordinary work.
    ANSWER_RACE_SECONDS = 120.0

    def recent_answer(self, worker_id: str) -> dict[str, Any] | None:
        """The answer this worker was sent moments ago, if it was sent one.

        One worker must have one driver.  Prose said so and nothing enforced
        it, so a root and a project's foreman both answered the same question
        inside a minute: the second `send-text` arrived while the agent was
        already acting on the first, interleaved with its own redraw, and
        Claude Code read the arriving keystrokes as an interrupt.  They agreed
        that time, so the only casualty was a pane nobody could read
        afterwards; two answers that DISAGREED would have raced, and whichever
        landed second would have silently won.

        Deliberately recency-scoped rather than "has this question been
        answered".  Answering is not the only thing this path carries -- a
        driver also pushes fresh instructions to a worker that asked nothing --
        and refusing those would break the ordinary case to prevent the rare
        one.  What is never ordinary is two answers inside two minutes.
        """
        cutoff = _dt.datetime.now(_dt.timezone.utc).timestamp() - self.ANSWER_RACE_SECONDS
        data = self.store.load()
        for message in reversed(data.get("messages", [])):
            if message.get("worker_id") != worker_id or message.get("kind") != "answer":
                continue
            stamp = message.get("created_at")
            try:
                when = _dt.datetime.fromisoformat(str(stamp).replace("Z", "+00:00"))
            except ValueError:
                return None
            return message if when.timestamp() >= cutoff else None
        return None

    def _record_turn(
        self,
        data: dict[str, Any],
        project: dict[str, Any],
        task: dict[str, Any],
        worker: dict[str, Any],
        item: dict[str, Any],
    ) -> dict[str, Any] | None:
        """A turns runner closed one turn: keep the session and the last words.

        The session id is what a later turn resumes, so it goes on the worker
        record the moment it is known. The agent's final message of the turn
        is recorded as a summary status when it pushed no report itself
        during the turn, so a turn that ended with "I need X decided" in
        prose still reaches the foreman and the commander.
        """
        session = item.get("session_id")
        if isinstance(session, str) and session:
            worker["agent_session_id"] = session
        turn = item.get("turn")
        turns = worker.setdefault("turns", [])
        if isinstance(turn, int) and turn not in [t.get("turn") for t in turns if isinstance(t, dict)]:
            turns.append({"turn": turn, "exit": item.get("exit"), "at": now()})
        worker["turns"] = turns[-50:]
        text = _safe_text(item.get("text") or "").strip()
        if not text:
            return None
        # Same intake as a pushed status line, so the record, the routing to
        # the project's pane and the foreman's wake all happen as they would
        # for a report the agent had made itself.
        return self._ingest_worker_event(
            data, worker, "status",
            f"turn {turn} ended: {text[:1500]}",
            {"summary": True, "turn": turn, "exit": item.get("exit")},
            None,
        )

    def record_worker_message(
        self,
        worker_id: str,
        kind: str,
        text: str,
        *,
        payload: dict[str, Any] | None = None,
        requested_status: str | None = None,
    ) -> dict[str, Any]:
        # "question" lets a worker ask instead of guessing or stopping: the
        # coordinator answers from the goal and the work continues.  "answer" is
        # the coordinator's reply, recorded so the exchange stays auditable.
        if kind not in self.WORKER_MESSAGE_KINDS:
            raise HelmError(f"unsupported worker message type: {kind}")
        if requested_status == "approval-needed":
            # The status route could never name an action, so it created a
            # paused task with no hold and nothing to release.
            raise HelmError(
                "a pause is asked for with --type approval-needed --action <action>, "
                "not with --status approval-needed"
            )
        payload = payload or {}
        with self.store.locked() as data:
            worker = data["workers"].get(worker_id)
            if worker is None:
                raise HelmError(f"unknown worker: {worker_id}")
            late = False
            if worker["status"] != "running":
                verdict = self._late_delivery(worker, kind)
                if verdict == "refuse":
                    raise HelmError("worker is no longer running")
                if verdict == "noop":
                    # Already recorded in this exact form. Saying so again must
                    # not append a second outcome or a second diagnostic.
                    return dict(self._task(data, worker["task_id"]))
                late = True
            event = self._ingest_worker_event(
                data, worker, kind, text, payload, requested_status, late=late
            )
            if late and kind == self.HOLD_MESSAGE_KIND:
                # Evidence preservation, not a revived worker: the reason the
                # worker paused is recorded, and then abandoned against the
                # session that has already ended -- the same durable reason and
                # the same failed task the approval-first order produces.
                worker["late_hold_recorded"] = True
                self._abandon_open_hold(
                    data,
                    self._project(data, worker["project_id"]),
                    self._task(data, worker["task_id"]),
                    "its session ended before the authorization was used",
                )
                # The hold was open when the metadata was built, so the effects
                # pass is still carrying an "Authorize or refuse" item for a
                # hold nobody can answer, and a situation line announcing a
                # pause on a task this same call has just failed. Neither may
                # reach the commander: the item is resolved instead, and the
                # abandonment is what the record's newest line says.
                settled_task = self._task(data, worker["task_id"])
                event["resolve_key"] = event["action_item_key"]
                event["action_item"] = None
                event["task_status"] = settled_task.get("status")
                event["situation"] = self._hold_abandoned_situation(settled_task)
            task = dict(self._task(data, worker["task_id"]))
        self._apply_event_effects(event)
        return task

    def _parse_output_line(
        self,
        data: dict[str, Any],
        project: dict[str, Any],
        task: dict[str, Any],
        worker: dict[str, Any],
        line: str,
    ) -> dict[str, Any] | None:
        # A line that is not a protocol push is terminal output, and terminal
        # output is not state. The runner already writes every byte of it to
        # the worker's own log, which is what `helm tail` reads and what
        # `worker_output_mark` measures -- so recording it here stored a second
        # copy that nothing ever read back.
        #
        # It was not free. Half a million such lines had accumulated as 99.4%
        # of a 224 MB state file, and because a save rewrites the whole
        # document, every one of them paid to serialise all the ones before it.
        stripped = line.rstrip("\r\n")
        if not stripped:
            return None
        try:
            item = json.loads(stripped)
        except json.JSONDecodeError:
            return None
        if not isinstance(item, dict) or item.get("helm") != 1:
            return None
        kind = item.get("type")
        if kind == "turn":
            return self._record_turn(data, project, task, worker, item)
        if kind not in self.WORKER_MESSAGE_KINDS or kind == "answer":
            return None
        text = _safe_text(item.get("text", item.get("message", "")))
        payload = item.get("payload") if isinstance(item.get("payload"), dict) else {}
        if kind == "artifact":
            # The path is taken only from the bounded artifact payload and is
            # checked against the assigned worktree by _record_artifact.
            payload = dict(payload)
            if "path" not in payload and "path" in item:
                payload["path"] = item["path"]
            if "description" not in payload and "description" in item:
                payload["description"] = item["description"]
        requested = item.get("status") if isinstance(item.get("status"), str) else None
        if requested == "approval-needed":
            requested = None
        # Same intake as a direct push, so a stdout protocol line gets the hold,
        # the record, the action item and the evidence a push would have got.
        # A malformed request is the worker's error to see in its own log, not a
        # reason to abandon the rest of the line processing.
        try:
            return self._ingest_worker_event(
                data, worker, kind, text, payload, requested
            )
        except (HelmError, SafetyError) as exc:
            self._message(
                data, project, task, worker, "protocol-rejected",
                f"Rejected {kind} from worker output: {exc}", {},
            )
            return None

    @staticmethod
    def _read_exit_record(worker: dict[str, Any]) -> int | None:
        """The return code the runner *observed*, or None if there is none yet.

        An unreadable or malformed record is a failed exit, not an absent one:
        the runner writes it last, so its presence is the completion signal.

        A record Helm asserted rather than observed -- `helm worker stop` and
        `mark_worker_lost` write `{"returncode": null, "stopped": true}` -- is
        deliberately not an observation and returns None. Reading it as exit 1
        would let a commanded stop masquerade as the process fallback, and the
        late-delivery path is only ever open to a worker settled by a real
        observation.
        """
        exit_path = Path(worker["exit_file"])
        _private_file(exit_path)
        if not exit_path.exists():
            return None
        try:
            record = json.loads(exit_path.read_text(encoding="utf-8"))
        except (OSError, ValueError, json.JSONDecodeError):
            return 1
        if not isinstance(record, dict):
            return 1
        if record.get("stopped") or record.get("returncode") is None:
            return None
        try:
            return int(record["returncode"])
        except (ValueError, KeyError, TypeError):
            return 1

    #: Everything the lifecycle contract records about one *episode* of a
    #: worker's life -- its verdict and the observation of its session ending.
    #: A reopened worker starts a new one, so these are cleared rather than
    #: carried across: a reviewer kept live for a second round must be able to
    #: give that round its own verdict, and a revived session's next exit is a
    #: new observation rather than one already folded in.
    _EPISODE_FIELDS = (
        "protocol_outcome",
        "outcome_source",
        "process_settled",
        "exit_observed",
        "process_exit_code",
        "process_exited_at",
        "exit_mismatch_recorded",
        "protocol_conflict_recorded",
        "late_hold_recorded",
    )

    #: The subset of those that are flags, so clearing means False, not None.
    _EPISODE_FLAGS = frozenset({
        "process_settled",
        "exit_observed",
        "exit_mismatch_recorded",
        "protocol_conflict_recorded",
        "late_hold_recorded",
    })

    @classmethod
    def begin_worker_episode(cls, worker: dict[str, Any]) -> None:
        """Clear one episode's lifecycle record as a worker is put back to work.

        Called by every path that returns a settled worker to `running`. See
        docs/worker-lifecycle.md.
        """
        for field in cls._EPISODE_FIELDS:
            worker[field] = False if field in cls._EPISODE_FLAGS else None

    def episode_outcome(
        self, data: dict[str, Any], worker: dict[str, Any]
    ) -> str | None:
        """This episode's terminal outcome, for readers that judge liveness.

        Message history is cumulative and an episode is not: a reviewer kept
        live for round two still has round one's `result` in the record, and a
        reader that scans for one classifies the new round as already reported
        -- or settles it on the previous round's verdict. The persisted
        `protocol_outcome` is the authority, including when it is None.

        The scan survives only as a fallback for a worker recorded before that
        field existed, which is why the test is `in` rather than truthiness:
        present-and-None is an answer, not a missing value.
        """
        if "protocol_outcome" in worker:
            return self.terminal_protocol_outcome(worker)
        for message in reversed(data.get("messages", [])):
            if (
                message.get("worker_id") == worker["id"]
                and message.get("kind") in self._TERMINAL_MESSAGE_TASK_STATE
            ):
                return str(message["kind"])
        return None

    @staticmethod
    def terminal_protocol_outcome(worker: dict[str, Any]) -> str | None:
        """The terminal outcome this worker itself reported, if any.

        Read from the worker's durable `protocol_outcome`, never by scanning
        message kinds: the process fallback synthesizes `result` and `failure`
        messages so a task's history reads the same either way, and scanning
        would read Helm's own fallback back as the worker's word. See
        docs/worker-lifecycle.md.
        """
        outcome = worker.get("protocol_outcome")
        return outcome if isinstance(outcome, str) else None

    def _exit_mismatch_evidence(
        self,
        data: dict[str, Any],
        project: dict[str, Any],
        task: dict[str, Any],
        worker: dict[str, Any],
        outcome: str,
    ) -> None:
        """Record, once, that the return code disagrees with the outcome.

        The disagreement is real information and is kept (rule 6); it never
        moves the outcome (rule 2). Guarded by `exit_mismatch_recorded` so it
        reads the same whichever order the two events arrived in and does not
        repeat on a duplicate.
        """
        if not worker.get("exit_observed") or worker.get("exit_mismatch_recorded"):
            return
        exit_code = worker.get("process_exit_code")
        if exit_code is None or (exit_code == 0) == (outcome == "result"):
            return
        worker["exit_mismatch_recorded"] = True
        self._message(
            data, project, task, worker, "exit-evidence",
            f"Session exited with code {exit_code} against its {outcome} "
            f"message; the {outcome} stands as the task outcome",
            {"exit_code": exit_code, "outcome": outcome},
        )

    def _apply_process_exit(
        self,
        data: dict[str, Any],
        project: dict[str, Any],
        task: dict[str, Any],
        worker: dict[str, Any],
        exit_code: int | None,
    ) -> str | None:
        """Fold one process-exit observation into the lifecycle. Under lock.

        The single place an exit moves anything, so the outcome cannot depend on
        whether Helm saw the worker's own terminal message first. Observation is
        recorded once (`exit_observed`); a second poll of the same worker is a
        no-op rather than a second failure message. `exit_code` is None when the
        session is known to be gone but left no return code -- evidence with no
        verdict in it, so it is recorded and nothing is concluded from it.

        Returns the id of a hold this abandoned, so the caller can clear the
        commander's action item for it once the lock is released.
        """
        if worker.get("exit_observed"):
            return None
        worker["exit_observed"] = True
        worker["process_exit_code"] = exit_code
        worker["process_exited_at"] = now()
        held = (self.task_hold(task) or {}).get("id")
        outcome = self.terminal_protocol_outcome(worker)
        if outcome is not None:
            # Contract rule 2: the worker's word already decided the task, and
            # the return code cannot overwrite it -- only stand beside it.
            if exit_code is None:
                self._message(
                    data, project, task, worker, "exit-evidence",
                    f"Session is gone with no completion record after its "
                    f"{outcome} message; the {outcome} stands as the task outcome",
                    {"exit_code": None, "outcome": outcome},
                )
            else:
                self._exit_mismatch_evidence(data, project, task, worker, outcome)
            self._abandon_open_hold(
                data, project, task,
                "its session ended before the authorization was used",
            )
            return held
        exit_code = 1 if exit_code is None else exit_code
        worker["outcome_source"] = "process"
        worker["process_settled"] = True
        worker["status"] = "completed" if exit_code == 0 else "failed"
        worker["exit_code"] = exit_code
        worker["ended_at"] = now()
        if exit_code != 0:
            task["status"] = "failed"
            self._message(
                data, project, task, worker, "failure",
                f"Worker exited with code {exit_code}",
                {"exit_code": exit_code, "source": "process-fallback"},
            )
            self._abandon_open_hold(
                data, project, task, f"its session exited with code {exit_code}"
            )
        elif task["status"] == "blocked" and not self.terminal_protocol_outcome(worker):
            # A PAUSED FOREMAN WHOSE SESSION THEN ENDED. `blocked` is in
            # neither branch above, so a clean exit here used to fall through
            # both: the worker was recorded `completed`, no message was
            # written, and the task sat blocked forever beside a worker that
            # looked finished and was in fact gone.
            #
            # That is precisely the ambiguity the foreman pause was introduced
            # to remove. Pausing on a blocker is only an improvement if
            # "waiting for an answer" stays distinguishable from "dead" -- a
            # pause nobody is listening to is worse than an honest failure,
            # because the reader believes an answer will reach someone.
            #
            # The task stays blocked: the escalation still stands and still
            # needs a human. What changes is that the worker reads failed and
            # the reason is on the record, so the next reader knows a new
            # driver has to take it.
            worker["status"] = "failed"
            worker["exit_code"] = 1
            self._message(
                data, project, task, worker, "failure",
                "Its session ended while the task was blocked, so the pause is "
                "over and nothing is listening for an answer. The blocker still "
                "stands: a new driver has to pick it up.",
                {"exit_code": exit_code, "source": "process-fallback"},
            )
        elif task["status"] in {"created", "allocated", "running"}:
            task["status"] = "completed"
            self._message(
                data, project, task, worker, "result",
                "Worker completed; explicit approval is still required before merge",
                {"status": "completed", "source": "process-fallback"},
            )
        # A session that has ended cannot answer or act on anything, so no hold
        # survives it -- including the one belonging to a task sitting in
        # `approval-needed`, which is the state that used to be permanently
        # unreleasable and uncleanable. Rule 4: never silent, always with the
        # reason recorded.
        self._abandon_open_hold(
            data, project, task,
            "its session ended before the authorization was used",
        )
        return held

    def poll_worker(self, worker_id: str) -> dict[str, Any]:
        events: list[dict[str, Any]] = []
        settled: dict[str, Any]
        with self.store.locked() as data:
            worker = data["workers"].get(worker_id)
            if worker is None:
                raise HelmError(f"unknown worker: {worker_id}")
            task = self._task(data, worker["task_id"])
            project = self._project(data, worker["project_id"])
            if worker["status"] != "running":
                # Settled on its own terminal message. Its exit record is still
                # worth reading once -- as evidence, per the lifecycle contract
                # -- but its log is not drained again, so a line written after
                # the terminal message cannot reopen a settled task.
                resolve_key = None
                if (
                    not worker.get("exit_observed")
                    and self.terminal_protocol_outcome(worker) is not None
                ):
                    recorded = self._read_exit_record(worker)
                    if recorded is None and worker.get("external") is not True:
                        # A worker Helm launched itself, whose pid is gone with
                        # nothing written: the session ending is still a fact
                        # worth keeping, it just carries no return code. An
                        # asserted stop is excluded -- it writes a record that
                        # `_read_exit_record` deliberately does not observe --
                        # by the pid check, since stop kills the process it owns
                        # only after recording the decision.
                        #
                        # Collect the corpse before asking whether it is alive.
                        # A child Helm disowned is never waited on, so once it
                        # exits it stays a zombie for the life of this process
                        # -- and a zombie is still in the process table, so
                        # `os.kill(pid, 0)` succeeds and `_pid_alive` says True
                        # about a process that is already dead. The branch below
                        # then never fires, and a worker that died without
                        # writing an exit record stays `running` for as long as
                        # the coordinator lives. That is the case this check
                        # exists for: the one where nothing was reported and the
                        # session simply ended. Reaping here is what makes the
                        # question answerable; it cannot cost an exit code,
                        # because the code is read from the runner's exit file
                        # above and never from `waitpid`.
                        self._reap_child(worker.get("pid"), blocking=False)
                        if not self._pid_alive(worker.get("pid")):
                            # The record was absent a moment ago and the runner
                            # is gone now -- and the runner writes its record
                            # in exactly that gap, just before it exits. Read
                            # once more before concluding it left none; the
                            # first look was the race, not the fact.
                            recorded = self._read_exit_record(worker)
                            resolve_key = self._apply_process_exit(
                                data, project, task, worker, recorded
                            )
                    elif recorded is not None:
                        resolve_key = self._apply_process_exit(
                            data, project, task, worker, recorded
                        )
                if resolve_key:
                    events.append(self._hold_resolved_event(task, worker, resolve_key))
                settled = dict(worker)
            else:
                log_path = Path(worker["log_file"])
                _private_file(log_path)
                lines: list[str] = []
                if log_path.exists():
                    lines = log_path.read_text(encoding="utf-8", errors="replace").splitlines()
                start = int(worker.get("processed_lines", 0))
                for line in lines[start:]:
                    event = self._parse_output_line(data, project, task, worker, line)
                    if event is not None:
                        events.append(event)
                worker["processed_lines"] = len(lines)

                finished = False
                exit_code: int | None = self._read_exit_record(worker)
                if exit_code is not None:
                    finished = True
                elif worker.get("pid") and not self._child_alive(worker):
                    # Any worker whose process is known and gone, provider-
                    # launched or not. The guard used to exempt every Herdr
                    # worker, so a session the OS killed overnight -- laptop
                    # asleep, memory gone -- stayed "running" with its pane
                    # showing a shell prompt, and the coordinator reported it
                    # alive twice. The adopted pid is the agent's own; a pane
                    # is not a process.
                    # The record is checked again before the process's absence is
                    # believed. The runner writes its exit record and *then*
                    # exits, so between the check above and this one it can have
                    # done both -- and the pid can also vanish early, because
                    # anything else creating a subprocess in this interpreter may
                    # reap an already-finished child. Concluding "no completion
                    # record" from that ordering failed workers that had exited 0.
                    recorded = self._read_exit_record(worker)
                    finished = True
                    if recorded is not None:
                        exit_code = recorded
                    else:
                        exit_code = 1
                        if self.terminal_protocol_outcome(worker) is None:
                            # No completion record and no word from the worker: the
                            # runner died. With a terminal outcome this is only
                            # evidence, and `_apply_process_exit` records it as
                            # such -- writing a `failure` message here would forge
                            # a protocol outcome the worker never sent.
                            self._message(
                                data,
                                project,
                                task,
                                worker,
                                "failure",
                                "Worker runner exited without a completion record",
                                {"source": "process-fallback"},
                            )
                if finished:
                    resolved = self._apply_process_exit(
                        data, project, task, worker, exit_code
                    )
                    if resolved:
                        events.append(self._hold_resolved_event(task, worker, resolved))
                settled = dict(worker)
        return self._settled(settled, events)

    def _settled(
        self, worker: dict[str, Any], events: list[dict[str, Any]]
    ) -> dict[str, Any]:
        """Run one poll's unlocked effects and return the settled worker.

        A local child whose exit has been observed is also reaped here, without
        blocking. Polling used to record the exit and leave the zombie for
        whoever called `wait_worker` next -- and nothing has to, so a coordinator
        that only ever polls accumulated one per worker.
        """
        if worker.get("exit_observed") and worker.get("external") is not True:
            self._reap_child(worker.get("pid"), blocking=False)
        for event in events:
            self._apply_event_effects(event)
        return worker

    @staticmethod
    def _reap_child(pid: int | None, *, blocking: bool = True) -> None:
        if not pid:
            return
        try:
            os.waitpid(int(pid), 0 if blocking else os.WNOHANG)
        except (ChildProcessError, ProcessLookupError, OSError):
            # Async CLI launches are reparented when their coordinator exits;
            # those children are not waitable from a later invocation.
            pass

    def wait_worker(self, worker_id: str, timeout: float | None = None) -> dict[str, Any]:
        started = time.monotonic()
        while True:
            worker = self.poll_worker(worker_id)
            if worker["status"] != "running":
                # Waiting is waiting for the *assignment*, which a terminal
                # protocol message settles whether or not the session exits --
                # so this returns on that, promptly, including the default
                # `timeout=None`. An interactive agent that reports and keeps
                # its session open is finished, and blocking on its pid was the
                # contract being contradicted by the wait path.
                #
                # A caller that additionally needs the *session* gone -- only
                # cleanup does, because it removes the directory the session
                # sits in -- has its own gate in `_session_still_live`.
                self._reap_child(
                    worker.get("pid"), blocking=bool(worker.get("exit_observed"))
                )
                return worker
            if timeout is not None and time.monotonic() - started >= timeout:
                return worker
            # Back off. A fixed 50ms was chosen for a test that finishes in
            # under a second and then applied to a worker that runs for an
            # hour, which is twenty state reads a second for the whole hour.
            # The first polls stay fast, because that is what makes a short
            # launch feel immediate; a long one settles to a check every two
            # seconds, which is far below the resolution anyone waiting on a
            # model actually needs.
            waited = time.monotonic() - started
            time.sleep(0.05 if waited < 2 else min(2.0, waited / 20))

    # ---------- explicit approval and local delivery ----------

    def _require_terminal_worker(
        self,
        data: dict[str, Any],
        task: dict[str, Any],
        operation: str,
        *,
        require_completed: bool = False,
        allow_never_started: bool = False,
    ) -> dict[str, Any]:
        """The task's last worker, once it is safe to act on the task.

        `allow_never_started` is for cleanup alone. A task can be created,
        given a worktree, and never launched -- appointing a foreman does it
        every time, and replacing a stuck one leaves the old task behind. Those
        accumulated with no supported way to shed them, because a task with no
        worker can never have a *terminal* one, so cleanup refused them
        forever: 63 on this root, holding 63 checkouts, none of which had ever
        run anything.

        Nothing else may set it. Approve, merge and learning proposals all ask
        whether work STANDS, and a task that never ran has no work to stand --
        accepting one there would approve an empty tree.
        """
        workers = self._task_workers(data, task["id"])
        if not workers:
            if allow_never_started:
                return {}
            raise SafetyError(f"{operation} requires a recorded terminal worker")
        live = [worker for worker in workers if worker.get("status") == "running"]
        if live:
            raise SafetyError(f"{operation} refused while worker {live[0]['id']} is still running")
        if require_completed:
            # The question is whether the work STANDS, and only the last round
            # answers it. Asking every worker the task ever had makes one
            # failed round permanently unapprovable -- a branch reviewed over
            # forty rounds could never be merged because round four's worker
            # died, even though every later round completed and the reviewer
            # signed off on the tip. A round that failed and was replaced is
            # history, not an open defect; a task whose LATEST round failed is
            # the case this guard is actually for.
            # A launch that died before its runner ever wrote a byte is not a
            # round: it produced nothing to judge, and letting it shadow the
            # real latest round made genuinely delivered work unapprovable
            # until someone hand-edited state.
            def _ran(worker: dict[str, Any]) -> bool:
                if worker.get("status") != "failed":
                    return True
                log_file = worker.get("log_file")
                try:
                    return bool(log_file) and Path(log_file).stat().st_size > 0
                except OSError:
                    return False
            ran = [worker for worker in workers if _ran(worker)] or workers
            latest = max(
                ran,
                key=lambda worker: (worker.get("started_at") or "", worker.get("id") or ""),
            )
            if latest.get("status") != "completed":
                raise SafetyError(
                    f"{operation} requires a successfully completed worker; the "
                    f"latest is {latest['id']} [{latest.get('status')}]"
                )
        return workers[0]

    # ---------- standing approval grants ----------







    @staticmethod
    def _resumable_session(worker: dict[str, Any]) -> bool:
        """Whether anything can be said to this worker's session.

        A Herdr pane hosts an interactive agent that reads what is sent to it. A
        worker started by the plain process launcher runs in print mode with its
        stdio detached, so there is no channel at all -- and pretending
        otherwise is what let an authorization be spent on a session that could
        never hear it.
        """
        return worker.get("execution") != "process"









    def record_pr_opened(
        self,
        task_id: str,
        url: str,
        *,
        source: str = "manual",
    ) -> dict[str, Any]:
        """Record that a PR-delivered task has reached its review surface."""
        url = _safe_text(url).strip()
        if not url:
            raise HelmError("PR URL is required")
        with self.store.locked() as data:
            task = self._task(data, task_id)
            self._refuse_read_only_delivery(task, "recorded as a PR")
            project = self._project(data, task["project_id"])
            if task["delivery_policy"] != "pr":
                raise SafetyError(
                    f"task {task_id} uses {task['delivery_policy']} delivery; PR state belongs to PR-delivered tasks"
                )
            if task["status"] not in {"completed", "approved", "pr-open", "pr-merged"}:
                raise SafetyError(
                    f"PR creation can be recorded only after worker result/approval, got {task['status']}"
                )
            delivery = task.setdefault(
                "delivery", {"policy": task["delivery_policy"], "state": "worktree", "events": []}
            )
            delivery.update({
                "policy": "pr",
                "state": "pr-open",
                "url": url,
                "opened_at": delivery.get("opened_at") or now(),
                "source": _safe_text(source).strip() or "manual",
            })
            delivery.setdefault("events", []).append({
                "at": now(),
                "state": "pr-open",
                "url": url,
                "source": delivery["source"],
            })
            task["status"] = "pr-open"
            self._message(
                data,
                project,
                task,
                None,
                "pr-created",
                f"Pull request opened for {task['branch']}: {url}",
                {"url": url, "source": delivery["source"], "branch": task["branch"]},
            )
            return task

    def record_pr_status(
        self,
        task_id: str,
        *,
        state: str,
        url: str = "",
        comments: int | None = None,
        checks: str = "",
        review_decision: str = "",
        merge_commit: str = "",
    ) -> dict[str, Any]:
        """Record the observed state of an open PR, including the terminal merge."""
        observed = _safe_text(state).strip().lower()
        if observed not in {"open", "merged", "closed"}:
            raise HelmError("PR state must be open, merged, or closed")
        with self.store.locked() as data:
            task = self._task(data, task_id)
            self._refuse_read_only_delivery(task, "recorded as a PR")
            project = self._project(data, task["project_id"])
            if task["delivery_policy"] != "pr":
                raise SafetyError(
                    f"task {task_id} uses {task['delivery_policy']} delivery; PR monitoring belongs to PR-delivered tasks"
                )
            if task["status"] not in {"completed", "approved", "pr-open", "pr-merged"}:
                raise SafetyError(
                    f"PR monitoring requires worker result/approval or an open PR record, got {task['status']}"
                )
            delivery = task.setdefault(
                "delivery", {"policy": task["delivery_policy"], "state": "worktree", "events": []}
            )
            if observed == "open" and not (_safe_text(url).strip() or delivery.get("url")):
                raise HelmError("recording an open PR requires --url unless one is already recorded")
            event = {
                "at": now(),
                "state": f"pr-{observed}",
                "url": _safe_text(url).strip() or delivery.get("url", ""),
                "comments": comments,
                "checks": _safe_text(checks).strip(),
                "review_decision": _safe_text(review_decision).strip(),
                "merge_commit": _safe_text(merge_commit).strip(),
            }
            delivery.setdefault("events", []).append(event)
            if event["url"]:
                delivery["url"] = event["url"]
            delivery["last_checked_at"] = event["at"]
            delivery["last_observed_state"] = observed
            delivery["comments"] = comments
            delivery["checks"] = event["checks"]
            delivery["review_decision"] = event["review_decision"]
            if observed == "merged":
                task["status"] = "pr-merged"
                delivery["state"] = "pr-merged"
                delivery["merged_at"] = event["at"]
                if event["merge_commit"]:
                    delivery["merge_commit"] = event["merge_commit"]
                kind = "pr-merged"
                text = f"Pull request merged: {delivery.get('url', '')}".strip()
            elif observed == "closed":
                task["status"] = "approval-needed"
                delivery["state"] = "pr-closed"
                kind = "pr-status"
                text = f"Pull request closed without merge: {delivery.get('url', '')}".strip()
            else:
                task["status"] = "pr-open"
                delivery["state"] = "pr-open"
                kind = "pr-status"
                text = f"Pull request still {observed}: {delivery.get('url', '')}".strip()
            self._message(data, project, task, None, kind, text, event)
            if observed == "merged":
                self.resolve_delivery_decisions(
                    project["id"], reason="pr-merged", data=data
                )
                # Delivered is not finished. Raise the cleanup gate in the same
                # breath, so the moment the delivery decision closes there is
                # still something saying what this task holds.
                with contextlib.suppress(HelmError, OSError):
                    self.refresh_finalization_decisions(project["id"], data=data)
            return task

    def task_outcome(self, task_id: str) -> dict[str, Any]:
        """Everything needed to judge a task's work without merging it.

        Merging to see the result would mean reviewing after the fact, and with
        several workers in flight only the first could fast-forward anyway. The
        work is already readable where it is: a task worktree is a real
        checkout, and its branch diffs against the base like any other.
        """
        data = self.store.load()
        task = self._task_anywhere(data, task_id)
        project = data["projects"].get(task["project_id"]) or {"id": task["project_id"], "root": "", "color": ""}
        workspace = canonical(task["workspace"])
        root = canonical(project["root"]) if project.get("root") else workspace
        outcome: dict[str, Any] = {
            "task_id": task_id,
            "project_id": project["id"],
            "glyph": project_glyph(project.get("color", "")),
            "status": task["status"],
            "brief": task["brief"],
            "branch": task["branch"],
            "base_branch": task["base_branch"],
            "base_revision": task.get("base_revision"),
            "base_upstream": task.get("base_upstream"),
            "workspace": str(workspace),
            "workspace_exists": workspace.is_dir(),
            "agent_id": task.get("agent_id"),
            "delivery": task.get("delivery") or {
                "policy": task.get("delivery_policy"),
                "state": task.get("status"),
            },
            "diffstat": [],
            "commits": [],
            "dirty": [],
            "artifacts": [],
            "delivered": [],
            "messages": [],
        }
        if outcome["workspace_exists"]:
            # The pinned commit, not the branch name: `base_branch` moves,
            # and by the time an outcome is read the project's branch may
            # already sit ahead of where this task actually started --
            # exactly the shape that once put a stranger's commit inside a
            # reviewed diff. Fall back to the branch name only for a record
            # old enough to predate `base_revision`.
            base = task.get("base_revision") or task["base_branch"]
            # ...unless the branch merged its base branch back in, which puts
            # that branch's whole history between the pin and the tip. See
            # `base_after_merges`, which moves forward only when it is safe.
            with contextlib.suppress(HelmError, OSError, subprocess.SubprocessError):
                base = base_after_merges(workspace, task, base) or base
            with contextlib.suppress(HelmError, OSError, subprocess.SubprocessError):
                outcome["commits"] = [
                    line
                    for line in _git(
                        workspace, "log", "--oneline", f"{base}..HEAD", check=False
                    ).splitlines()
                    if line
                ]
            with contextlib.suppress(HelmError, OSError, subprocess.SubprocessError):
                outcome["diffstat"] = [
                    line
                    for line in _git(
                        workspace, "diff", "--stat", f"{base}...HEAD", check=False
                    ).splitlines()
                    if line
                ]
            with contextlib.suppress(HelmError, OSError, subprocess.SubprocessError):
                outcome["dirty"] = [
                    line
                    for line in _git(
                        workspace, "status", "--porcelain=v1", check=False
                    ).splitlines()
                    if line
                ]
        for artifact in data.get("artifacts", []):
            if artifact.get("task_id") != task_id:
                continue
            path = str(artifact.get("path") or "")
            outcome["artifacts"].append({
                "path": path,
                "in_worktree": bool(path) and (workspace / path).exists(),
                "in_project": bool(path) and (root / path).exists(),
            })
        for message in data.get("messages", []):
            if message.get("task_id") == task_id and message.get("kind") in {
                "result", "blocker", "failure", "question", "approval-needed",
            }:
                outcome["messages"].append({
                    "kind": message["kind"],
                    "text": _safe_text(message.get("text", ""))[:2000],
                })
        return outcome




    def _session_still_live(self, worker: dict[str, Any]) -> bool:
        """True when the OS session behind a settled worker is not known to be over.

        A terminal protocol message settles a worker so its work becomes
        reviewable, deliberately without waiting for the provider to exit --
        interactive agents report a result and keep their session open, and a
        session killed with its pane never writes an exit record at all. That
        is right for review, and wrong for destroying the directory the
        session is still sitting in: cleanup ends in `worktree remove
        --force`, which would pull the worktree out from under an agent that
        can still write to it. So the runner's exit record stays the gate for
        that one operation, and only for it.
        """
        exit_file = worker.get("exit_file")
        if exit_file and Path(exit_file).exists():
            return False
        # A worker Helm launched itself is answerable through its own pid.
        if worker.get("external") is not True:
            return self._pid_alive(worker.get("pid"))
        # Anything else reported itself terminal without the runner recording
        # an exit. That cannot be told apart from a session still sitting in
        # the worktree, and this is the one place the difference destroys work.
        return True



    def open_escalations(self, project_id: str | None = None) -> list[dict[str, Any]]:
        """Messages that asked a human for something and never got an answer.

        A `question`, `blocker`, or `approval-needed` is the one kind of worker
        output that cannot be acted on by the sender. Four foremen escalated
        real problems this way -- a duplicate task holding a dev-server port, a
        learning proposal mis-filed into a shared domain -- and a reviewer asked
        whether it could run the test suite. None was answered, because nothing
        listed them: `helm status` prints every message in full, and a page of
        prose hides an escalation exactly as well as dropping it would.

        Liveness is judged per kind, because each kind is answered a different
        way and a worker can have more than one open ask at once:

        - A `question` pauses nothing; it is live for as long as the worker
          that asked it is still running its session, whatever the *task's*
          status says -- a worker can ask a question while its own task sits
          in `approval-needed` on an unrelated hold, and that question must
          not be hidden just because the task is not `running`. Once that
          worker settles -- reports a terminal message, or is settled by
          observation -- its question is done being live even if the task is
          later reopened for another round under a different worker: a new
          round's worker is a new worker record, and an old round's question
          must never resurface under it.
        - A `blocker` is live only while the task is still `blocked`; the
          task-status route to worker recovery does not exist in this state
          machine, but a task no longer `blocked` (superseded by a harder
          terminal message from the same worker, or state repaired outside
          Helm) is no longer waiting on this particular blocker.
        - An `approval-needed` message is live only while its *own* hold is
          still open. "Some hold is open on this task" is not enough: a task
          continued for another round drops its approval on the way through
          and can open a brand new hold for a different action, and matching
          only on task status would resurrect the earlier round's already
          settled request under the new one. A task carries at most one open
          hold, so this is derived per task, from that hold, rather than by
          scanning messages -- one entry per task no matter how many
          historical approval-needed messages or restatements it has. A hold
          records the id of the exact message that opened it, so equality on
          `message_id` is checked first, and that resolved message is then
          checked against the hold and task it claims to belong to (kind,
          task, worker, project) before its text or timestamp is trusted --
          a corrupted or foreign id must never lend an escalation someone
          else's ask. A hold from before `message_id` existed falls back to
          matching the hold's own `worker_id` against the message's, which is
          still specific to one worker's one ask rather than "a hold exists
          somewhere on this task". The hold record is the truth here, not an
          `answer` message: a foreman or commander routinely sends `answer`
          text to a paused worker ("looking at it") without that touching the
          hold at all, and treating that as resolving the escalation would
          hide a still-open protected-action decision.

        `question` and `blocker` treat a later `answer` to the same worker as
        resolving it, same as before; `approval-needed` does not, per above.
        Only the newest pending ask *of each kind* per worker is considered --
        an older one is superseded by construction -- but a live question and
        a live approval-needed hold on the same worker are two different
        asks and both stay visible.

        A worker or task record that has gone missing or unreadable is not
        silently treated as resolved: liveness cannot be verified either way,
        so it is surfaced as its own diagnostic entry rather than invented
        away, using only what the message itself already recorded (its own
        `project_id` and `task_id`, stamped at write time) -- nothing here
        guesses at facts the missing record can no longer confirm. A live
        hold whose worker record has gone missing appears exactly once, still
        actionable (the hold itself says it is open) but marked unverified,
        rather than once from the hold and again from a separate orphan scan.

        Nothing here deletes or rewrites the underlying message -- the full
        history stays exactly as recorded; this only decides what still needs
        a human now. Anything still open is returned newest first, because
        the reader wants what is waiting now, not the order it arrived in.
        """
        data = self.store.load()
        messages = data.get("messages", [])
        workers = data.get("workers", {})
        tasks = data.get("tasks", {})
        message_by_id = {m["id"]: m for m in messages if m.get("id")}
        # Indexed by position in the log rather than by `created_at`: two
        # messages recorded in the same second compare equal as timestamps,
        # and an escalation immediately followed by its own answer must not
        # be mistaken for arriving before it.
        last_answer_index: dict[str, int] = {}
        for index, message in enumerate(messages):
            if message.get("kind") == "answer":
                last_answer_index[str(message.get("worker_id") or "")] = index

        open_items: list[dict[str, Any]] = []

        # --- question / blocker: latest message of each kind per worker ---
        # A question and a blocker from the same worker are different asks
        # and must not collapse into one another, but two blockers -- or two
        # questions -- from the same worker are the same ask restated, and
        # only the newest matters.
        latest: dict[tuple[str, str], tuple[int, dict[str, Any]]] = {}
        for index, message in enumerate(messages):
            kind = message.get("kind")
            if kind not in {"question", "blocker"}:
                continue
            worker_id = str(message.get("worker_id") or "")
            latest[(worker_id, kind)] = (index, message)

        for (worker_id, kind), (index, message) in latest.items():
            msg_project_id = message.get("project_id")
            if project_id and msg_project_id != project_id:
                continue
            answered = index <= last_answer_index.get(worker_id, -1)
            worker = workers.get(worker_id)
            task = tasks.get(str(message.get("task_id") or ""))
            if worker is None or task is None:
                # Cannot be proven resolved without the record that would
                # prove it -- surfaced rather than dropped, and not answered
                # for it either: an `answer` alone is not the same claim as
                # the record itself confirming the ask is done.
                open_items.append(self._orphaned_escalation(message, worker_id))
                continue
            status = task.get("status")
            if kind == "question":
                if worker.get("status") != "running" or answered:
                    continue
            elif kind == "blocker":
                if status != "blocked" or answered:
                    continue
                # A FOREMAN'S BLOCKER IS ANSWERED BY ITS REPLACEMENT. A
                # foreman that escalates ends `blocked` and stays `blocked`
                # forever -- there is no route out of that state for a task
                # nobody will continue -- so this test alone kept summoning a
                # human to escalations that had been dealt with hours before.
                # Six of them, aged 7 to 18 hours, were still on the attention
                # list the morning after the work they described was finished
                # and merged. An attention list that is mostly answered items
                # trains its reader to skim, which is the one failure it
                # cannot afford.
                #
                # The record stays; only the summons is dropped.
                if self._superseded_foreman_report(
                    data, {"task_id": str(message.get("task_id") or "")}
                ):
                    continue
            open_items.append({
                "kind": kind,
                "project_id": msg_project_id,
                "role": task.get("role", "worker"),
                "worker_id": worker_id,
                "task_id": message.get("task_id"),
                "created_at": str(message.get("created_at") or ""),
                "text": str(message.get("text", "")),
            })

        # --- approval-needed: bound to each task's own currently open hold ---
        # "Some hold is open on this task" is not enough: a task continued for
        # another round drops its approval on the way through and can open a
        # brand new hold on a new worker for a different action, and matching
        # on task status alone would resurrect the earlier round's already
        # settled request under the new one. So this is never derived by
        # picking "the latest approval-needed message" -- a restated request
        # appends a new message without moving the hold's own `message_id` --
        # it is always resolved from the hold record itself, and a task holds
        # at most one open hold, so this produces at most one entry per task
        # no matter how many historical approval-needed messages it has.
        #
        # A message a hold points to is trusted only after it is checked
        # against the hold and task it is supposed to belong to: a corrupted
        # or foreign `message_id` must never lend its text or timestamp to an
        # escalation for a different ask.
        for task in tasks.values():
            if project_id and task.get("project_id") != project_id:
                continue
            if task.get("status") != "approval-needed":
                continue
            hold = self.task_hold(task)
            if hold is None:
                continue
            hold_worker_id = str(hold.get("worker_id") or "")
            hold_message_id = hold.get("message_id")
            message = message_by_id.get(hold_message_id) if hold_message_id else None
            if message is not None and (
                message.get("kind") != "approval-needed"
                or str(message.get("task_id") or "") != task["id"]
                or str(message.get("worker_id") or "") != hold_worker_id
                or message.get("project_id") != task.get("project_id")
            ):
                # The id resolved to a real message, but not to the one this
                # hold actually opened -- trusting its text or timestamp
                # would be answering this hold with someone else's ask.
                message = None
            elif message is None and hold_message_id is None:
                # A hold recorded before `message_id` existed: fall back to
                # the newest approval-needed message from the same worker on
                # this task, which is still specific to one worker's one ask
                # rather than "a hold exists somewhere on this task".
                candidates = [
                    m for m in messages
                    if m.get("kind") == "approval-needed"
                    and str(m.get("task_id") or "") == task["id"]
                    and str(m.get("worker_id") or "") == hold_worker_id
                ]
                message = candidates[-1] if candidates else None
            worker = workers.get(hold_worker_id)
            entry = {
                "kind": "approval-needed",
                "project_id": task.get("project_id"),
                "role": task.get("role", "worker"),
                "worker_id": hold_worker_id,
                "task_id": task["id"],
                "created_at": (
                    str(message.get("created_at") or "")
                    if message is not None
                    else str(hold.get("requested_at") or "")
                ),
                "text": (
                    str(message.get("text", "")) if message is not None
                    else str(hold.get("text") or "")
                ),
            }
            if message is None:
                entry["diagnostic"] = (
                    "this hold's own escalation message could not be found or "
                    "does not match its record; liveness could not be verified"
                )
            elif worker is None:
                # The ask itself is genuine and its hold is still open --
                # still actionable -- but the worker record behind it is
                # gone, so this stays visible exactly once, marked as unable
                # to be fully verified rather than duplicated by a separate
                # orphan pass.
                entry["diagnostic"] = "worker record is missing; liveness could not be verified"
            open_items.append(entry)

        # An approval-needed ask whose *task* record itself is gone cannot be
        # reached by the hold-driven pass above at all -- there is no task to
        # iterate. Surfaced separately, collapsed to the newest ask per
        # worker so a restated request or several rounds of history under one
        # worker do not each mint their own row.
        latest_taskless: dict[str, tuple[int, dict[str, Any]]] = {}
        for index, message in enumerate(messages):
            if message.get("kind") != "approval-needed":
                continue
            if tasks.get(str(message.get("task_id") or "")) is not None:
                continue
            worker_id = str(message.get("worker_id") or "")
            latest_taskless[worker_id] = (index, message)
        for worker_id, (_, message) in latest_taskless.items():
            msg_project_id = message.get("project_id")
            if project_id and msg_project_id != project_id:
                continue
            open_items.append(self._orphaned_escalation(message, worker_id))

        return sorted(open_items, key=lambda item: item["created_at"], reverse=True)

    @staticmethod
    def _orphaned_escalation(message: dict[str, Any], worker_id: str) -> dict[str, Any]:
        """An ask whose worker or task record is missing or unreadable.

        Liveness cannot be proven either way without that record, so this is
        surfaced as its own diagnostic entry rather than silently dropped --
        using only what the message itself already recorded at write time,
        never a guess at what the missing record would have said.
        """
        return {
            "kind": message.get("kind"),
            "project_id": message.get("project_id"),
            "role": "worker",
            "worker_id": worker_id,
            "task_id": message.get("task_id"),
            "created_at": str(message.get("created_at") or ""),
            "text": str(message.get("text", "")),
            "diagnostic": "worker or task record is missing; liveness could not be verified",
        }


    def _remove_worker_directories_locked(
        self, data: dict[str, Any], task: dict[str, Any]
    ) -> None:
        """Shed the directories a task's workers ran in.

        `helm worker stop` tells the reader "its log and worktree are kept as
        evidence; remove them with helm task cleanup", and cleanup removed the
        worktree and left the directory. 110 of 126 on disk belonged to tasks
        whose worktree had already been cleaned.

        A worker directory is not only a log. It is the scratch space the agent
        runs in, and one spike had pointed Xcode's derivedDataPath at it and
        left 15 GB there.
        """
        for worker in self._task_workers(data, task["id"]):
            config_file = worker.get("config_file")
            if not config_file:
                continue
            # A live session is still writing in there.
            if self._session_still_live(worker):
                continue
            worker_dir = canonical(Path(config_file).parent)
            # Never outside Helm's own state, whatever a record claims.
            if not overlaps(worker_dir, self.store.directory / "workers"):
                continue
            with contextlib.suppress(OSError):
                shutil.rmtree(worker_dir)
            worker["directory_removed"] = True


    # ---------- inspection ----------

    def inspect_task(self, task_id: str) -> dict[str, Any]:
        data = self.store.load()
        task = data["tasks"].get(task_id)
        if task is None:
            # Cleaned up and archived: the record is whole, in its own file.
            record = self.archived_task(task_id)
            if record is None:
                raise HelmError(f"unknown task: {task_id}")
            project = data["projects"].get(record["task"].get("project_id")) or {
                "id": record["task"].get("project_id"), "archived": True,
            }
            return {
                "task": record["task"],
                "project": project,
                "workers": list(record.get("workers", {}).values()),
                "messages": record.get("messages", []),
                "artifacts": record.get("artifacts", []),
                "archived_at": record.get("archived_at"),
            }
        return {
            "task": task,
            "project": self._project(data, task["project_id"]),
            "workers": [worker for worker in data["workers"].values() if worker["task_id"] == task_id],
            "messages": [message for message in data["messages"] if message["task_id"] == task_id],
            "artifacts": [artifact for artifact in data["artifacts"] if artifact["task_id"] == task_id],
        }

    def status(self, project_id: str | None = None) -> dict[str, Any]:
        data = self.store.load()
        projects = list(data["projects"].values())
        tasks = list(data["tasks"].values())
        if project_id:
            self._project(data, project_id)
            projects = [project for project in projects if project["id"] == project_id]
            tasks = [task for task in tasks if task["project_id"] == project_id]
        tasks.sort(key=lambda task: task["created_at"], reverse=True)
        messages = [message for message in data["messages"] if not project_id or message["project_id"] == project_id]
        return {
            "projects": sorted(projects, key=lambda project: project["created_at"]),
            "tasks": tasks,
            "workers": list(data["workers"].values()),
            "messages": messages[-20:],
            "artifacts": [artifact for artifact in data["artifacts"] if not project_id or artifact["project_id"] == project_id],
        }
