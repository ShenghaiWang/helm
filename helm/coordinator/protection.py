"""The protected-action lifecycle: the authorization, and the acts it gates.

A mixin over `CoordinatorBase`, split out of `core`. This is the strongest
thing Helm does and it was buried mid-file: a hold is one request bound to one
reviewed snapshot, a standing grant is the same answer given in advance for a
class of action, and approve/merge/publish/cleanup/release are the acts that
authorization exists to gate. They are one subject seen from three distances,
so the transition table and the code that decides whether a transition is
legal stay in one file.

What is deliberately NOT here, despite sitting beside it in `core` for a long
time: the worker message protocol, artifact and PR recording, and escalations.
Those were inside the same line range and a range-based cut would have swept
them in -- which is how a module ends up meaning "authorization AND five other
things", the same drift that turned `status` into a second `core`.

Moved verbatim; it imports nothing from `helm.core`.
"""

from __future__ import annotations

import contextlib
import hashlib
import shutil
from pathlib import Path
from typing import Any

from ..errors import HelmError, SafetyError
from ..git import _git
from ..paths import _file_digest, canonical
from ..values import (
    DELIVERED_TASK_STATES,
    HOLD_OPEN_STATUSES,
    HOLD_TASK_STATUS,
    HOLD_TRANSITIONS,
    PROTECTED_ACTIONS,
    WORKTREELESS_ROLES,
    _safe_text,
    _validate_grantable_action,
    _validate_project_id,
    _validate_protected_action,
    new_id,
    now,
    shape_policy,
    task_owns_branch,
)


def _stamp_epoch(value: Any) -> float | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        import datetime as _dt
        return _dt.datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


class ProtectionMixin:
    """Holds, standing grants, and the protected acts they gate."""

    def _snapshot(
        self,
        data: dict[str, Any],
        project: dict[str, Any],
        task: dict[str, Any],
        *,
        subject_task_id: str | None = None,
    ) -> dict[str, Any]:
        """Exactly what an authorization is about, by content, or an exception.

        The previous binding hashed the *text* of `git status --porcelain`,
        which is path/status pairs. Rewriting every byte of an already-untracked
        render left that text identical, so the authorization to publish one
        file silently covered a different one -- the common publish case this is
        for. Ignored build outputs were not in it at all.

        So this binds content: the committed revision and tree, the index, the
        full tracked diff against HEAD (staged and unstaged, binary included),
        every untracked path by digest, and every artifact this task declared --
        by artifact id, path and digest -- plus whatever sits under the
        project's declared delivery directories, which is where ignored outputs
        live. Workspace identity is verified first, through the same isolation
        check every other operation uses, so a swapped or missing worktree is a
        refusal rather than an empty binding.
        """
        if task.get("role") in WORKTREELESS_ROLES:
            # A foreman asking to push a worker's branch is asking about THAT
            # branch. Binding its request to the project root's HEAD -- the
            # commander's own checkout -- tied an approval to a revision the
            # request never mentioned, and any fetch or checkout there
            # invalidated a push the commander had just approved. The
            # subject is named on the request and kept on the hold, so every
            # later comparison looks at the same tree.
            subject_id = subject_task_id or (self.task_hold(task) or {}).get("subject_task_id")
            if subject_id:
                subject = data.get("tasks", {}).get(subject_id)
                if subject is None or subject.get("project_id") != project["id"]:
                    raise SafetyError(
                        f"approval subject {subject_id} is not a task of project "
                        f"{project['id']}"
                    )
                if subject.get("role") in WORKTREELESS_ROLES:
                    raise SafetyError(
                        f"approval subject {subject_id} has no worktree of its own to bind to"
                    )
                bound = self._snapshot(data, project, subject)
                bound["scope"] = "subject"
                bound["subject_task_id"] = subject_id
                return bound
            # No checkout and no branch. There is no content to bind, and
            # pretending otherwise would be a fiction; the scope says so
            # explicitly instead of returning nothing and being read as absent.
            root = canonical(project["root"])
            return {
                "scope": "project",
                "project_root": str(root),
                "revision": _git(root, "rev-parse", "HEAD"),
            }
        workspace = self._verify_workspace_record(data, project, task)
        branch = task.get("branch")
        if not branch:
            raise SafetyError(
                f"task {task['id']} has no branch to bind an authorization to"
            )
        head = _git(workspace, "rev-parse", "--abbrev-ref", "HEAD").strip()
        if head != branch:
            raise SafetyError(
                f"task worktree is on {head}, not its own branch {branch}; "
                "refusing to bind an authorization to it"
            )
        untracked = [
            entry
            for entry in _git(
                workspace, "ls-files", "--others", "--exclude-standard", "-z"
            ).split("\0")
            if entry
        ]
        artifacts = sorted(
            (
                {
                    "id": artifact["id"],
                    "path": artifact["path"],
                    "digest": self._content_digest(workspace / artifact["path"]),
                }
                for artifact in data.get("artifacts", [])
                if artifact.get("task_id") == task["id"] and artifact.get("path")
            ),
            key=lambda entry: entry["id"],
        )
        delivered: list[dict[str, str]] = []
        for folder in self._discovery_settings(canonical(project["root"])).get("deliver", []):
            source = workspace / folder
            if not source.is_dir():
                continue
            for found in sorted(source.rglob("*")):
                if found.is_file():
                    delivered.append({
                        "path": found.relative_to(workspace).as_posix(),
                        "digest": self._content_digest(found),
                    })
        return {
            "scope": "workspace",
            "workspace": str(workspace),
            "branch": branch,
            "branch_tip": _git(workspace, "rev-parse", f"refs/heads/{branch}").strip(),
            "revision": _git(workspace, "rev-parse", "HEAD").strip(),
            "tree": _git(workspace, "rev-parse", "HEAD^{tree}").strip(),
            "index": hashlib.sha256(
                _git(workspace, "ls-files", "--stage", "-z").encode("utf-8")
            ).hexdigest(),
            # Content, not status: every tracked byte that differs from HEAD,
            # staged or not, in one comparable digest.
            "diff": hashlib.sha256(
                _git(workspace, "diff", "HEAD", "--binary").encode(
                    "utf-8", errors="surrogateescape"
                )
            ).hexdigest(),
            "untracked": [
                {"path": path, "digest": self._content_digest(workspace / path)}
                for path in sorted(untracked)
            ],
            "artifacts": artifacts,
            "delivered": delivered,
        }
    def _hold_history(self, task: dict[str, Any]) -> list[dict[str, Any]]:
        holds = task.get("holds")
        if not isinstance(holds, list):
            holds = []
            task["holds"] = holds
        return holds
    def task_hold(self, task: dict[str, Any]) -> dict[str, Any] | None:
        """The task's open hold, or None. History is never overwritten."""
        for hold in reversed(self._hold_history(task)):
            if hold.get("status") in HOLD_OPEN_STATUSES:
                return hold
        return None
    def latest_hold(self, task: dict[str, Any]) -> dict[str, Any] | None:
        history = self._hold_history(task)
        return history[-1] if history else None
    def _move_hold(
        self,
        data: dict[str, Any],
        project: dict[str, Any],
        task: dict[str, Any],
        hold: dict[str, Any],
        event: str,
        *,
        detail: str = "",
        payload: dict[str, Any] | None = None,
        worker: dict[str, Any] | None = None,
        message_kind: str = "",
    ) -> dict[str, Any]:
        """The only way a hold changes state, and the only place task status follows.

        Every route is in `HOLD_TRANSITIONS`. A movement that is not written
        there is a bug in the caller, not a state to fall into: five call sites
        each doing their own local update is how a failed task kept a live hold
        that could then be released back into `running`.
        """
        current = str(hold.get("status"))
        target = HOLD_TRANSITIONS.get((current, event))
        if target is None:
            raise SafetyError(
                f"hold {hold.get('id')} cannot {event} from {current}"
            )
        hold["status"] = target
        hold.setdefault("history", []).append(
            {"at": now(), "event": event, "from": current, "to": target, "detail": detail}
        )
        if target not in HOLD_OPEN_STATUSES:
            hold["closed_at"] = now()
        implied = HOLD_TASK_STATUS.get(target)
        if implied:
            task["status"] = implied
        if message_kind:
            self._message(
                data,
                project,
                task,
                worker,
                message_kind,
                detail,
                {"hold_id": hold["id"], "hold_status": target, **(payload or {})},
            )
        return hold
    def _abandon_open_hold(
        self,
        data: dict[str, Any],
        project: dict[str, Any],
        task: dict[str, Any],
        reason: str,
    ) -> None:
        """Let go of a hold nobody can act on, whatever ended the task.

        Keyed on the task no longer being answerable rather than on how it said
        so: abandonment used to depend on the message *kind*, so `--status
        failed` left a waiting hold behind that a later release resurrected
        into `running`.
        """
        hold = self.task_hold(task)
        if hold is None:
            return
        self._move_hold(
            data, project, task, hold, "abandon",
            detail=f"Approval hold abandoned: {reason}",
            message_kind="approval-abandoned",
        )
        if task["status"] == "approval-needed":
            # Nothing is waiting on a human any more, and a task parked in
            # `approval-needed` with no answerable hold is residue: cleanup
            # refuses it and no round can reopen it. Failed is the honest,
            # cleanable, retryable state, and its log is still the evidence.
            task["status"] = "failed"
    def _hold_request(
        self,
        data: dict[str, Any],
        project: dict[str, Any],
        task: dict[str, Any],
        worker: dict[str, Any],
        message: dict[str, Any],
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        """Open, or restate, the pause a worker's `approval-needed` push asks for.

        Every hold names an exact action. An unspecified request used to be
        stored as an empty string, and release then accepted whichever action
        the commander happened to type -- a request for nothing authorizing a
        `delete`. `merge` is refused here rather than at release, because a
        worker cannot perform Helm's merge at all: it finishes and reports, and
        the branch is reviewed through `helm task approve`.

        A repeat of the same unanswered request restates it. Anything else while
        a hold is open is refused, so a live authorization cannot be replaced by
        a different request and lose its state and outcome linkage.
        """
        action = payload.get("action")
        if action not in PROTECTED_ACTIONS:
            raise HelmError(
                "an approval request must name the exact protected action with "
                f"--action (one of {', '.join(sorted(PROTECTED_ACTIONS - {'merge'}))})"
            )
        if action == "merge":
            raise HelmError(
                "merging is Helm's own operation and no worker performs it: "
                "finish and report your result, and the branch is reviewed with "
                "helm task approve before Helm merges it"
            )
        subject_task_id = payload.get("subject") or None
        if subject_task_id is not None and not isinstance(subject_task_id, str):
            raise HelmError("--subject names one task id")
        snapshot = self._snapshot(data, project, task, subject_task_id=subject_task_id)
        open_hold = self.task_hold(task)
        if open_hold is not None:
            if open_hold["status"] != "waiting" or open_hold["action"] != action:
                raise HelmError(
                    f"task {task['id']} already has a {open_hold['status']} hold for "
                    f"{open_hold['action']} ({open_hold['id']}); resolve it before "
                    "asking for something else"
                )
            if open_hold.get("snapshot") == snapshot:
                # The same unanswered request, unchanged. One thing for the
                # commander to decide, not two.
                return self._move_hold(
                    data, project, task, open_hold, "restate",
                    detail=f"Approval request restated: {action}",
                    worker=worker,
                )
            # Same action, different work. Refreshing the old hold in place
            # would let a request the commander is already reading change
            # underneath them, so the old one is superseded visibly and this
            # becomes a new request with its own id and its own snapshot.
            self._move_hold(
                data, project, task, open_hold, "abandon",
                detail=(
                    f"Superseded: the {action} request was restated for different "
                    "work, so the earlier one no longer describes anything"
                ),
                worker=worker,
                message_kind="approval-abandoned",
            )
        hold = {
            "id": new_id("h"),
            "status": "waiting",
            "action": action,
            "worker_id": worker["id"],
            "message_id": message["id"],
            "text": _safe_text(message.get("text", ""))[:900],
            "requested_at": now(),
            # The exact state being asked about. Bound now, compared later, and
            # never silently replaced: this is what the commander is deciding.
            "snapshot": snapshot,
            "subject_task_id": subject_task_id,
            "authorization": None,
            "delivery": {"attempts": 0, "delivered_at": None, "acknowledged_at": None},
            "outcome": {"started_at": None, "receipts": [], "reported_at": None},
            "history": [{"at": now(), "event": "request", "from": None, "to": "waiting",
                         "detail": f"Approval requested: {action}"}],
        }
        self._hold_history(task).append(hold)
        task["status"] = "approval-needed"
        return hold
    def _hold_abandoned_situation(self, task: dict[str, Any]) -> str:
        return self._situation_line(
            f"worker: task {task['id']} [{task.get('status')}] ",
            self._HOLD_ABANDONED_NOTE,
        )
    def _hold_resolved_event(
        self, task: dict[str, Any], worker: dict[str, Any], hold_id: str
    ) -> dict[str, Any]:
        """The unlocked half of abandoning a hold nobody can answer.

        Exit observation runs entirely under the lock, so without this the
        commander kept an "Authorize or refuse" item for a hold that had
        already been abandoned and a task that had already failed.
        """
        event = self._noop_event(task, worker, "approval-abandoned")
        event["resolve_key"] = hold_id
        event["situation"] = self._hold_abandoned_situation(task)
        return event
    def _reconcile_hold_attention(self, event: dict[str, Any]) -> None:
        """Make the commander's view true whatever order the effects ran in.

        The request's own effects run with the state lock released, so a poll
        can abandon that hold in between -- resolving an action item that does
        not exist yet, after which this thread creates it and appends
        "Approval request" as the newest word on a task that has already
        failed. Both halves are then wrong, and neither is at fault.

        So the request re-reads the hold once its effects have landed: if it is
        no longer open, the item it just created is closed and the abandonment
        is restated as the latest line. Idempotent and order-free -- whichever
        of the two runs last leaves the same record, and a run where nothing
        raced does nothing.
        """
        hold_id = event.get("action_item_key")
        if event.get("hold_event") != "request" or not hold_id:
            return
        data = self.store.load()
        task = data.get("tasks", {}).get(event["task_id"]) or {}
        holds = task.get("holds") if isinstance(task.get("holds"), list) else []
        hold = next((h for h in holds if h.get("id") == hold_id), None)
        if hold is None or hold.get("status") in HOLD_OPEN_STATUSES:
            return
        project_id = event["project_id"]
        with contextlib.suppress(HelmError, OSError):
            self.resolve_project_action_items(project_id, hold_id)
        line = self._hold_abandoned_situation(task)
        with contextlib.suppress(HelmError, OSError):
            status = self.project_status(project_id)
            live = [
                entry for entry in status.get("situation", [])
                if not entry.get("superseded_by")
            ]
            if live and live[-1].get("text") == line:
                return
        with contextlib.suppress(HelmError, OSError):
            self.record_situation(project_id, line)
    def _resolve_hold_on_event(
        self,
        data: dict[str, Any],
        project: dict[str, Any],
        task: dict[str, Any],
        worker: dict[str, Any],
        hold: dict[str, Any],
        kind: str,
        receipts: list[Any],
    ) -> str:
        """What one non-approval event does to the open hold. Keyed on outcome."""
        if task["status"] in {"blocked", "failed"}:
            self._abandon_open_hold(
                data, project, task, f"task ended {task['status']} with the hold open"
            )
            return "abandon"
        if hold["status"] == "in-flight" and kind == "question":
            # THE ACTION WAS ATTEMPTED AND DID NOT COMPLETE, and the worker is
            # asking what to do about it. Before this, an in-flight hold closed
            # only on a `result` or receipts -- so a worker whose authorized
            # action FAILED had no way to say so: claiming a result would be a
            # lie, and anything else left the hold in-flight forever, which
            # then refuses every future approval-needed on that task. A publish
            # that exited before its first API call got stuck exactly there,
            # unable to ask for the retry that would have been safe.
            #
            # Closing it here spends nothing and hides nothing: the
            # authorization was already spent at action-start, the question
            # carries the account of what happened, and a retry needs a FRESH
            # authorization bound to a fresh snapshot -- which is precisely the
            # protection that was unreachable while the hold dangled.
            self._move_hold(
                data, project, task, hold, "outcome",
                detail=(
                    f"Authorized {hold['action']} did not complete; the worker "
                    "asked rather than retrying. A fresh authorization is needed."
                ),
                payload={"receipts": hold["outcome"].get("receipts", [])},
                worker=worker,
                message_kind="approval-outcome",
            )
            return "outcome"
        if hold["status"] == "in-flight" and (kind == "result" or receipts):
            # The authorized action ran and this is its outcome. Receipts are
            # recorded as outcome data; they are never a precondition.
            hold["outcome"]["receipts"] = list(hold["outcome"].get("receipts", [])) + list(receipts)
            hold["outcome"]["reported_at"] = now()
            self._move_hold(
                data, project, task, hold, "outcome",
                detail=f"Authorized {hold['action']} reported complete",
                payload={"receipts": hold["outcome"]["receipts"]},
                worker=worker,
                message_kind="approval-outcome",
            )
            if kind == "result":
                task["status"] = "completed"
            return "outcome"
        if receipts and hold["status"] != "in-flight":
            # An action reported without the one-use ticket ever being consumed.
            # Helm has no evidence the precondition held when it happened, so
            # the authorization is spent and a human has to look.
            self._move_hold(
                data, project, task, hold, "invalidate",
                detail=(
                    f"Reported acting on {hold['action']} without consuming the "
                    "authorization; it must be approved again"
                ),
                worker=worker,
                message_kind="approval-invalidated",
            )
            return "invalidate"
        if kind == "result":
            # It finished without ever using the authorization. That is not a
            # failure and not an approved action either; the record says so
            # rather than closing the hold as if it had been used.
            self._abandon_open_hold(
                data, project, task, "worker finished without using the authorization"
            )
            task["status"] = "completed"
            return "abandon"
        return ""
    def _late_delivery(self, worker: dict[str, Any], kind: str) -> str:
        """What to do with a push that arrived after the worker settled.

        `accept` for the word the worker was trying to get out when its own
        process exit won the lock race -- including a repeat of a verdict
        already given, which the one intake then folds away -- `noop` for a
        second late `approval-needed`, and `refuse` otherwise. Narrow on purpose: only a worker settled
        by observation alone qualifies, because an explicit stop, a foreman
        stand-down and `settle_reported_worker` are decisions rather than
        races. See docs/worker-lifecycle.md.
        """
        if kind in self._TERMINAL_MESSAGE_TASK_STATE and self.terminal_protocol_outcome(worker):
            # Already gave a verdict, whichever way it settled. Admitted rather
            # than refused so the one intake decides: a repeat records nothing,
            # a contradiction is kept once as evidence and changes no outcome.
            return "accept"
        if not worker.get("exit_observed") or not worker.get("process_settled"):
            return "refuse"
        if kind in self._TERMINAL_MESSAGE_TASK_STATE:
            return "accept"
        if kind == self.HOLD_MESSAGE_KIND:
            return "noop" if worker.get("late_hold_recorded") else "accept"
        return "refuse"
    def grant_approval(
        self,
        action: str,
        *,
        project_id: str | None = None,
        note: str,
        granted_by: str = "user",
        stale_days: int | None = None,
    ) -> dict[str, Any]:
        """Record one scoped standing approval a human decided in advance.

        A grant is the human's own policy, written once instead of re-answered
        per task. It lives here, in Helm-owned state: a project or domain file
        is untrusted guidance and must never be able to authorize a protected
        action, and neither can a worker message.

        A `cleanup` grant lets `helm watch` and the watchdog shed what a
        delivered task still holds; with `stale_days` it also sheds failed,
        never-launched and undelivered-completed tasks older than that.
        """
        action = _validate_grantable_action(action)
        self.authority(f"granting a standing approval for {action}")
        note = _safe_text(note).strip()
        if not note:
            # A grant outlives the conversation that created it. Without a
            # reason, nobody reviewing it later can tell whether it still
            # reflects what the human wanted.
            raise HelmError("a standing approval requires --note explaining what it permits and why")
        if stale_days is not None:
            if action != "cleanup":
                raise HelmError("--stale-days applies to a cleanup grant only")
            if int(stale_days) < 1:
                raise HelmError("--stale-days must be at least 1")
        with self.store.locked() as data:
            if project_id is not None:
                project_id = _validate_project_id(project_id)
                self._project(data, project_id)
            grant_id = new_id("g")
            grant = {
                "id": grant_id,
                "action": action,
                "project_id": project_id,
                "note": note,
                "granted_by": _safe_text(granted_by),
                "created_at": now(),
                "revoked_at": None,
                "revoked_note": "",
                "stale_days": int(stale_days) if stale_days is not None else None,
            }
            data["approval_grants"][grant_id] = grant
            return dict(grant)
    def revoke_approval_grant(self, grant_id: str, note: str = "") -> dict[str, Any]:
        self.authority("revoking a standing approval")
        with self.store.locked() as data:
            grant = data["approval_grants"].get(grant_id)
            if grant is None:
                raise HelmError(f"unknown approval grant: {grant_id}")
            if grant["revoked_at"] is None:
                grant["revoked_at"] = now()
                grant["revoked_note"] = _safe_text(note)
            return dict(grant)
    def list_approval_grants(self, *, include_revoked: bool = False) -> list[dict[str, Any]]:
        data = self.store.load()
        grants = [
            dict(grant)
            for grant in data.get("approval_grants", {}).values()
            if include_revoked or grant.get("revoked_at") is None
        ]
        return sorted(grants, key=lambda grant: grant["created_at"])
    def approval_grant_for(
        self, action: str, project_id: str | None = None
    ) -> dict[str, Any] | None:
        """Find the live grant covering one action, or ``None``.

        A project-scoped grant is preferred over an all-project one so the
        narrower policy is the one recorded as the authority. Scope never
        widens: a grant for one project says nothing about another.
        """
        action = _validate_grantable_action(action)
        candidates = [
            grant
            for grant in self.list_approval_grants()
            if grant["action"] == action
            and (grant["project_id"] is None or grant["project_id"] == project_id)
        ]
        if not candidates:
            return None
        candidates.sort(key=lambda grant: (grant["project_id"] is None, grant["created_at"]))
        return candidates[0]
    def _authorizing_grant(
        self,
        data: dict[str, Any],
        action: str,
        project_id: str,
        grant_id: str | None,
    ) -> dict[str, Any]:
        """The live standing grant that covers one action here, or an error.

        Checked at the moment it is used, against this task's own project. A
        revoked or differently scoped grant is not an approval, and acting
        anyway would make both revocation and scope meaningless.
        """
        if grant_id:
            grant = data.get("approval_grants", {}).get(grant_id)
            if grant is None:
                raise HelmError(f"unknown approval grant: {grant_id}")
        else:
            grant = self.approval_grant_for(action, project_id)
            if grant is None:
                raise SafetyError(
                    f"no standing approval covers {action} for project {project_id}; "
                    "this is the commander's decision: ask, then pass --confirm"
                )
        if grant.get("revoked_at") is not None:
            raise SafetyError(f"approval grant {grant['id']} was revoked; it cannot approve")
        if grant["action"] != action:
            raise SafetyError(
                f"approval grant {grant['id']} covers {grant['action']}, not {action}"
            )
        if grant["project_id"] is not None and grant["project_id"] != project_id:
            raise SafetyError(
                f"approval grant {grant['id']} is scoped to project {grant['project_id']}"
            )
        return grant
    def release_task_hold(
        self,
        task_id: str,
        *,
        action: str,
        note: str = "",
        grant_id: str | None = None,
        confirm: bool = False,
    ) -> dict[str, Any]:
        """Authorize the protected action a paused task asked for, and resume it.

        This is the other side of `approval-needed`. A worker that needs a
        merge, push, publish, deletion, or other external action stops and
        asks; nothing could then answer it, so the work it had been authorized
        to do could never be reported. Releasing records the human's decision,
        binds it to the worktree state they were deciding about, and puts the
        task back to `running` so the same session can finish and report.

        Deliberately not `helm task approve`. That command gates a *reviewed
        branch* on its way to a merge -- it requires a finished worker and a
        clean tree, and merging stays Helm's own operation. This one answers a
        live worker mid-task, which is a different question with different
        preconditions, and conflating them would let a merge be authorized
        without the review that gate exists for.
        """
        action = _validate_protected_action(action)
        if confirm and grant_id:
            raise HelmError(
                "authorize either by explicit confirmation or under one standing "
                "grant, not both: --confirm and --grant say different things about "
                "who decided"
            )
        if action == "merge":
            raise SafetyError(
                "merging is Helm's own gated operation, not something a worker "
                "performs: review the branch with helm task approve, then land it "
                "with helm task merge"
            )
        authority = self.authority(f"authorizing {action}")
        with self.store.locked() as data:
            task = self._task(data, task_id)
            project = self._project(data, task["project_id"])
            hold = self.task_hold(task)
            if hold is None or hold["status"] not in {"waiting", "authorized-pending-delivery"}:
                state = (hold or self.latest_hold(task) or {}).get("status", "none")
                raise HelmError(
                    f"task {task_id} is not waiting on an approval (hold: {state})"
                )
            if hold["action"] != action:
                # The commander answers the question that was asked. Releasing
                # a different action would authorize something nobody reviewed.
                raise SafetyError(
                    f"task {task_id} asked for {hold['action']}, not {action}; "
                    "authorize the action that was asked for"
                )
            worker = data.get("workers", {}).get(hold.get("worker_id"))
            if worker is None or worker.get("status") != "running":
                raise HelmError(
                    f"the session that asked ({hold.get('worker_id')}) is no longer "
                    "running, so an authorization cannot reach it; repair the task "
                    f"with helm approval repair {task_id}"
                )
            if not self._resumable_session(worker):
                # A print-mode process worker has no input channel, so nothing
                # can hand it the go-ahead. Refusing here leaves the hold
                # untouched: a spent authorization nobody could deliver is worse
                # than an honest refusal.
                raise SafetyError(
                    f"worker {worker['id']} runs as a plain process with no input "
                    "channel, so same-session resume is not available; it cannot be "
                    f"told. Repair the task with helm approval repair {task_id} and "
                    "run the work in an interactive session"
                )
            grant = None
            if not confirm:
                grant = self._authorizing_grant(data, action, project["id"], grant_id)
            # The precondition is the state the commander was shown, not
            # whatever the worktree has become since. Rebinding here silently
            # authorized a revision nobody had read.
            current = self._snapshot(data, project, task)
            recorded = hold.get("snapshot")
            if recorded and current != recorded:
                self._move_hold(
                    data, project, task, hold, "abandon",
                    detail=(
                        "The work changed after the approval was requested; the "
                        "commander was shown a different state. Ask again from the "
                        "state that exists now."
                    ),
                    payload={
                        "requested_revision": recorded.get("revision"),
                        "current_revision": current.get("revision"),
                    },
                    worker=worker,
                    message_kind="approval-invalidated",
                )
                self.store.save(data)
                raise SafetyError(
                    f"task {task_id} changed after it asked: the request was for "
                    f"{recorded.get('revision')} and the worktree is now "
                    f"{current.get('revision')}. Nothing was authorized; have the "
                    "worker request approval for the state it is actually in."
                )
            first_release = hold["status"] == "waiting"
            authorization = hold.get("authorization") or {}
            self._move_hold(
                data, project, task, hold, "authorize",
                detail=(
                    (
                        f"Authorized {action} under standing grant {grant['id']}"
                        if grant
                        else f"Authorized {action} on an explicit confirmation"
                    )
                    if first_release
                    else f"Re-delivering the existing authorization for {action}"
                ),
                payload={"action": action, "note": note, "authority": authority.mode},
                worker=worker,
                message_kind="approval" if first_release else "",
            )
            if first_release:
                hold["authorization"] = {
                    "authorized_at": now(),
                    "action": action,
                    "note": _safe_text(note),
                    "worker_id": worker["id"],
                    # Which authority answered: a person confirming now, or a
                    # standing grant they wrote earlier, and which boundary
                    # actually verified them.
                    "grant_id": grant["id"] if grant else None,
                    "grant_note": grant["note"] if grant else "",
                    "authority": authority.record(),
                    # One use, spent by the worker's own action-start call
                    # immediately before it acts.
                    "ticket": new_id("k"),
                    "ticket_consumed_at": None,
                    "snapshot": recorded,
                }
            else:
                hold["authorization"] = authorization
            hold["delivery"]["attempts"] = int(hold["delivery"].get("attempts", 0)) + 1
            released = dict(task)
            released["hold"] = dict(hold)
        if first_release:
            # The commander's decision belongs in the project's own record, not
            # only in this conversation: whoever takes the project over next has
            # to be able to see what was authorized without reading a transcript.
            with contextlib.suppress(HelmError, OSError):
                self.record_situation(
                    project["id"],
                    f"Commander authorized {action} for task {task_id} "
                    f"(awaiting delivery to {released['hold']['worker_id']})"
                    + (f": {_safe_text(note).strip()[:200]}" if note else ""),
                )
        return released
    def mark_hold_delivered(self, task_id: str, *, delivered: bool) -> dict[str, Any]:
        """Record whether the authorization actually reached the worker's session.

        Delivery is a separate fact from the decision. Recording it here keeps
        the retry honest: an undelivered authorization stays pending, the task
        stays paused, and the escalation stays open, so `helm approval release`
        can be run again for the same hold without a second decision.
        """
        with self.store.locked() as data:
            task = self._task(data, task_id)
            hold = self.task_hold(task)
            if hold is None or hold["status"] != "authorized-pending-delivery":
                raise HelmError(f"task {task_id} has no authorization awaiting delivery")
            if delivered:
                hold["delivery"]["delivered_at"] = now()
            return dict(hold)
    def hold_worker_id(self, task_id: str) -> str:
        """Which session a task's open hold belongs to, or "" when there is none.

        Exists so a caller can gather provider evidence about that exact session
        before asking core to act on it.
        """
        data = self.store.load()
        hold = self.task_hold(self._task(data, task_id))
        if hold is None:
            # A legacy request has no hold yet; name the session that asked, so
            # repair can still check whether it is alive.
            requests = [
                message
                for message in data.get("messages", [])
                if message.get("task_id") == task_id
                and message.get("kind") == self.HOLD_MESSAGE_KIND
            ]
            return str((requests[-1].get("worker_id") if requests else "") or "")
        return str(hold.get("worker_id") or "")
    def start_authorized_action(self, worker_id: str) -> dict[str, Any]:
        """The worker's own gate, immediately before it performs the action.

        This is where an authorization is validated and spent, and it is the
        only route from "approved" to "acting". Checking at result time was
        bookkeeping: the side effect had already happened, so an authorization
        that had gone stale could not be stopped, and a publish that wrote its
        own receipt invalidated itself for succeeding.

        Consuming the ticket here also *is* the delivery acknowledgement --
        only the live session that received the go-ahead can make this call --
        which is why the task stays paused until it happens.
        """
        with self.store.locked() as data:
            worker = data["workers"].get(worker_id)
            if worker is None:
                raise HelmError(f"unknown worker: {worker_id}")
            if worker["status"] != "running":
                raise HelmError("worker is no longer running")
            task = self._task(data, worker["task_id"])
            project = self._project(data, worker["project_id"])
            hold = self.task_hold(task)
            if hold is None:
                raise HelmError(
                    f"task {task['id']} has no approval hold; ask for one with "
                    "--type approval-needed --action <action>"
                )
            if hold["worker_id"] != worker_id:
                raise SafetyError(
                    f"hold {hold['id']} belongs to worker {hold['worker_id']}"
                )
            if hold["status"] == "in-flight":
                raise SafetyError(
                    f"the authorization for {hold['action']} has already been used; "
                    "report the outcome, and ask again if you need to act twice"
                )
            if hold["status"] != "authorized-pending-delivery":
                raise SafetyError(
                    f"{hold['action']} is not authorized (hold: {hold['status']}); "
                    "wait for the commander's decision and do not act"
                )
            authorization = hold.get("authorization") or {}
            if authorization.get("ticket_consumed_at"):
                raise SafetyError("this authorization has already been spent")
            current = self._snapshot(data, project, task)
            approved = authorization.get("snapshot") or hold.get("snapshot")
            if approved and current != approved:
                self._move_hold(
                    data, project, task, hold, "invalidate",
                    detail=(
                        "The work changed after the commander approved it; the "
                        f"authorization for {hold['action']} no longer covers it"
                    ),
                    payload={
                        "approved_revision": approved.get("revision"),
                        "current_revision": current.get("revision"),
                    },
                    worker=worker,
                    message_kind="approval-invalidated",
                )
                self.store.save(data)
                raise SafetyError(
                    "do not act: the work changed since the commander approved it, "
                    "so the authorization was invalidated and must be given again"
                )
            authorization["ticket_consumed_at"] = now()
            hold["delivery"]["acknowledged_at"] = now()
            hold["outcome"]["started_at"] = now()
            self._move_hold(
                data, project, task, hold, "consume",
                detail=f"Authorized {hold['action']} started by worker {worker_id}",
                payload={"action": hold["action"]},
                worker=worker,
                message_kind="approval-consumed",
            )
            return {
                "task_id": task["id"],
                "hold_id": hold["id"],
                "action": hold["action"],
                "note": authorization.get("note", ""),
                "authorized_at": authorization.get("authorized_at"),
                "status": hold["status"],
            }
    def repair_task_hold(
        self, task_id: str, *, session_live: bool, note: str = ""
    ) -> dict[str, Any]:
        """Recover a task stranded on an approval, including one from an older build.

        A pre-change root has the shape this whole change came from: an
        `approval-needed` message, no hold, and a worker already marked failed.
        Nothing could release it and nothing could report against it, so the
        session that prompted all of this stayed mute after upgrading.

        Repair is evidence-led, never inventive. `session_live` is supplied by
        the caller from provider evidence, because core does not talk to a
        presentation service and must not guess. With a live session and an
        unambiguous action, the hold is reconstructed from the state that exists
        now and that same worker is revived. Otherwise the hold is abandoned so
        the task can be retried or cleaned up, and an ambiguous request is
        handed back to the worker to restate rather than filled in by Helm.
        """
        self.authority("repairing an approval hold")
        with self.store.locked() as data:
            task = self._task(data, task_id)
            project = self._project(data, task["project_id"])
            open_hold = self.task_hold(task)
            legacy = [
                message
                for message in data.get("messages", [])
                if message.get("task_id") == task_id
                and message.get("kind") == self.HOLD_MESSAGE_KIND
            ]
            if open_hold is None and not legacy:
                raise HelmError(
                    f"task {task_id} has no approval request to repair"
                )
            request = legacy[-1] if legacy else None
            worker_id = (
                open_hold["worker_id"] if open_hold else str((request or {}).get("worker_id") or "")
            )
            worker = data.get("workers", {}).get(worker_id)
            action = (
                open_hold["action"]
                if open_hold
                else ((request or {}).get("payload") or {}).get("action")
            )
            if not session_live or worker is None:
                if open_hold is not None:
                    self._abandon_open_hold(
                        data, project, task,
                        note or "its session is gone; repaired so the task can be retried",
                    )
                else:
                    task["status"] = "failed"
                    self._message(
                        data, project, task, worker, "approval-abandoned",
                        "Legacy approval request abandoned: its session is gone. "
                        "The task is failed so it can be cleaned up or retried.",
                        {"message_id": (request or {}).get("id")},
                    )
                return {"task_id": task_id, "outcome": "abandoned", "hold": None}
            if action not in PROTECTED_ACTIONS or action == "merge":
                # Never invent the action. An unusable request is handed back to
                # the live worker to restate in the supported form.
                self._message(
                    data, project, task, worker, "answer",
                    "Your approval request did not name a usable protected action. "
                    "Re-report it as: --type approval-needed --action "
                    f"<{'|'.join(sorted(PROTECTED_ACTIONS - {'merge'}))}> with what "
                    "you would do.",
                    {"repair": "restate"},
                )
                return {"task_id": task_id, "outcome": "restate-requested", "hold": None}
            if worker.get("status") != "running":
                # Provider evidence says this session is alive, so the record is
                # what is wrong. Only this same worker is revived, and only here.
                worker["status"] = "running"
                worker["exit_code"] = None
                worker["ended_at"] = None
                self.begin_worker_episode(worker)
                self._message(
                    data, project, task, worker, "status",
                    "Worker revived on provider evidence that its session is live",
                    {"repair": "revive"},
                )
            if open_hold is None:
                hold = {
                    "id": new_id("h"),
                    "status": "waiting",
                    "action": action,
                    "worker_id": worker["id"],
                    "message_id": (request or {}).get("id"),
                    "text": _safe_text((request or {}).get("text", ""))[:900],
                    "requested_at": now(),
                    "snapshot": self._snapshot(data, project, task),
                    "authorization": None,
                    "delivery": {"attempts": 0, "delivered_at": None, "acknowledged_at": None},
                    "outcome": {"started_at": None, "receipts": [], "reported_at": None},
                    "history": [{
                        "at": now(), "event": "repair", "from": None, "to": "waiting",
                        "detail": "Hold reconstructed from a legacy approval request",
                    }],
                }
                self._hold_history(task).append(hold)
                task["status"] = "approval-needed"
                self._message(
                    data, project, task, worker, "approval-repaired",
                    f"Reconstructed the {action} approval hold from the recorded "
                    "request, bound to the work as it stands now",
                    {"hold_id": hold["id"], "action": action},
                )
                open_hold = hold
            return {
                "task_id": task_id,
                "outcome": "reconstructed",
                "hold": dict(open_hold),
            }
    @staticmethod
    def _refuse_read_only_delivery(task: dict[str, Any], verb: str) -> None:
        """Refuse a `read_only` task at every reachable delivery path.

        The locked worktree stops a worker from *authoring* new content, but
        an index-only commit (`git rm --cached` + commit, or
        `hash-object`/`update-index`) needs no worktree write and so is not
        stopped by it. A read-only task was never meant to reach delivery at
        all -- not local merge, not a PR push, not artifact copy-out, and not
        recording a PR opened or merged against its branch -- so every one of
        those entry points refuses it here regardless of how a commit landed
        on its branch.
        """
        if task.get("read_only") and not task.get("was_state_changing"):
            # `read_only` reflects the LATEST round; a delivery task that
            # closed with a read-only verification round is still a delivery
            # task. Only work that was never state-changing is refused here.
            raise SafetyError(
                f"task {task['id']} is read-only and was never a candidate for "
                f"delivery; a --read-only task cannot be {verb}"
            )
    def approve_task(
        self, task_id: str, note: str = "", *, grant_id: str | None = None
    ) -> dict[str, Any]:
        authority = self.authority("approving a reviewed branch")
        with self.store.locked() as data:
            task = self._task(data, task_id)
            self._refuse_read_only_delivery(task, "approved")
            project = self._project(data, task["project_id"])
            worker = self._require_terminal_worker(
                data, task, "approval", require_completed=True
            )
            if task["status"] not in {"completed", "approval-needed"}:
                raise SafetyError(f"task requires completion or approval-needed status, got {task['status']}")
            workspace = self._verify_workspace_record(data, project, task)
            if not self._workspace_clean(workspace):
                raise SafetyError("approval requires a clean reviewed worker workspace")
            check = task.get("shape_check") or {}
            if task.get("shape") == "small" and check.get("suggested") == "critical":
                raise SafetyError(
                    "task is shaped small, but its review found "
                    f"{'; '.join(check.get('findings') or [])[:300]}. Re-shape it before approving: "
                    f"helm task shape {task_id} critical --reason '...' (or standard, if the finding is wrong)"
                )
            if shape_policy(task).get("evidence_required"):
                # A critical change is approved on evidence, not on a
                # reviewer's word: the full suite's exit, recorded against
                # the exact revision being approved.
                head = _git(workspace, "rev-parse", "HEAD").strip()
                reports = [
                    (message.get("payload") or {}).get("full_suite")
                    for message in data.get("messages", [])
                    if message.get("task_id") == task_id
                    and isinstance((message.get("payload") or {}).get("full_suite"), dict)
                ]
                green = [
                    report for report in reports
                    if head.startswith(str(report.get("tip") or "\0")) or str(report.get("tip") or "").startswith(head)
                ]
                if not any(int(report.get("exit", 1) or 0) == 0 for report in green):
                    raise SafetyError(
                        f"task is shaped critical, so approval needs the full suite's exit recorded "
                        f"for revision {head[:10]}: helm task evidence {task_id} --tip {head[:10]} "
                        "--command '<suite command>' --exit 0"
                    )
            grant = None
            if grant_id is not None:
                grant = data["approval_grants"].get(grant_id)
                if grant is None:
                    raise HelmError(f"unknown approval grant: {grant_id}")
                # A grant is checked at the moment it is used, against this
                # task's own project. A revoked or differently scoped grant is
                # not an approval, and silently approving anyway would make
                # revocation meaningless.
                if grant["revoked_at"] is not None:
                    raise SafetyError(f"approval grant {grant_id} was revoked; it cannot approve")
                if grant["action"] != "merge":
                    raise SafetyError(
                        f"approval grant {grant_id} covers {grant['action']}, not merge"
                    )
                if grant["project_id"] is not None and grant["project_id"] != project["id"]:
                    raise SafetyError(
                        f"approval grant {grant_id} is scoped to project {grant['project_id']}"
                    )
            revision = _git(workspace, "rev-parse", "HEAD")
            branch_tip = _git(workspace, "rev-parse", f"refs/heads/{task['branch']}")
            tree = _git(workspace, "rev-parse", "HEAD^{tree}")
            task["approval"] = {
                "approved_at": now(),
                "note": _safe_text(note),
                "worker_id": worker["id"],
                "branch": task["branch"],
                "branch_tip": branch_tip,
                "revision": revision,
                "tree": tree,
                # Which authority approved this: a person answering now, or a
                # standing grant they wrote earlier. Both are recorded; the
                # binding to revision and tree is identical either way.
                "grant_id": grant["id"] if grant else None,
                "grant_note": grant["note"] if grant else "",
                # Which boundary verified the human: a configured capability, or
                # this session's role alone. An audit that cannot tell them apart
                # claims more than was checked.
                "authority": authority.record(),
            }
            task["status"] = "approved"
            self._message(
                data,
                project,
                task,
                None,
                "approval",
                (
                    f"Approval recorded under standing grant {grant['id']} for an immutable worker revision"
                    if grant
                    else "Explicit approval recorded for an immutable worker revision"
                ),
                {
                    "note": note,
                    "worker_id": worker["id"],
                    "revision": revision,
                    "tree": tree,
                    "grant_id": grant["id"] if grant else None,
                },
            )
            return task
    def deliver_task_artifacts(
        self, task_id: str, *, force: bool = False
    ) -> list[dict[str, Any]]:
        """Copy a task's build outputs from its worktree into the project.

        A merge moves tracked files only, so a rendered video -- the actual
        product -- stays in the task worktree and dies with it. Delivery moves
        the outputs the worker declared as artifacts, plus anything under the
        directories the project names in `.helm/project.json` `deliver`, which
        catches outputs a worker forgot to report.

        Copying never leaves the project root, never escapes the worktree, and
        never silently replaces a different existing file.
        """
        data = self.store.load()
        task = self._task(data, task_id)
        self._refuse_read_only_delivery(task, "delivered")
        project = self._project(data, task["project_id"])
        workspace = canonical(task["workspace"])
        if not workspace.is_dir():
            raise HelmError(f"task worktree is gone; nothing to deliver: {workspace}")
        project_root = canonical(project["root"])

        wanted: list[str] = []
        for artifact in data.get("artifacts", []):
            if artifact.get("task_id") == task_id and artifact.get("path"):
                wanted.append(str(artifact["path"]))
        for folder in self._discovery_settings(project_root).get("deliver", []):
            source_dir = workspace / folder
            if not source_dir.is_dir():
                continue
            for found in sorted(source_dir.rglob("*")):
                if found.is_file():
                    wanted.append(str(found.relative_to(workspace)))

        delivered: list[dict[str, Any]] = []
        seen: set[str] = set()
        for relative in wanted:
            if relative in seen:
                continue
            seen.add(relative)
            source = self._safe_configuration_path(
                workspace / relative, workspace, "task artifact"
            )
            if not source.is_file():
                delivered.append({"path": relative, "status": "missing"})
                continue
            destination = self._safe_configuration_path(
                project_root / relative, project_root, "delivered artifact"
            )
            if destination.exists():
                if destination.stat().st_size == source.stat().st_size and (
                    _file_digest(destination) == _file_digest(source)
                ):
                    delivered.append({"path": relative, "status": "identical"})
                    continue
                if not force:
                    delivered.append({"path": relative, "status": "exists"})
                    continue
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, destination)
            delivered.append({"path": relative, "status": "delivered"})

        copied = [entry for entry in delivered if entry["status"] == "delivered"]
        if copied:
            with self.store.locked() as live:
                live_task = self._task(live, task_id)
                live_project = self._project(live, live_task["project_id"])
                self._message(
                    live,
                    live_project,
                    live_task,
                    None,
                    "status",
                    f"Delivered {len(copied)} output(s) into the project: "
                    + ", ".join(entry["path"] for entry in copied),
                    {"delivered": [entry["path"] for entry in copied]},
                )
        return delivered
    def publish_task_branch(
        self,
        task_id: str,
        *,
        remote: str = "origin",
        grant_id: str | None = None,
        confirm: bool = False,
    ) -> dict[str, Any]:
        """Push a task branch so the change can be reviewed on the remote.

        This is the other way to see a change: a PR shows it where review
        tooling lives, instead of landing it on main first. Pushing is a
        protected action and leaves the machine, so it happens only on an
        explicit confirmation or a standing `push` grant -- never as a side
        effect of finishing work.
        """
        self.authority("pushing a task branch")
        data = self.store.load()
        task = self._task(data, task_id)
        project = self._project(data, task["project_id"])
        self._refuse_read_only_delivery(task, "published")
        if not confirm:
            grant = (
                data.get("approval_grants", {}).get(grant_id)
                if grant_id
                else self.approval_grant_for("push", project["id"])
            )
            if grant is None or grant.get("revoked_at") is not None:
                raise SafetyError(
                    "pushing leaves this machine and needs explicit authorization: "
                    "pass --confirm, or grant it once with "
                    f"helm approval grant push --project {project['id']} --note '...'"
                )
            grant_id = grant["id"]
        workspace = canonical(task["workspace"])
        if not workspace.is_dir():
            raise HelmError(f"task worktree is gone; nothing to push: {workspace}")
        if not self._workspace_clean(workspace):
            # Uncommitted work is not in the branch, so the PR would silently
            # show less than the task actually produced.
            raise SafetyError(
                "worktree has uncommitted changes; commit them or the push omits them"
            )
        root = canonical(project["root"])
        remotes = _git(root, "remote", check=False).split()
        if remote not in remotes:
            raise HelmError(
                f"project {project['id']} has no '{remote}' remote; add one or pass --remote"
            )
        branch = task["branch"]
        _git(workspace, "push", "--set-upstream", remote, branch)
        url = _git(root, "remote", "get-url", remote, check=False).strip()
        with self.store.locked() as live:
            live_task = self._task(live, task_id)
            live_project = self._project(live, live_task["project_id"])
            delivery = live_task.setdefault(
                "delivery", {
                    "policy": live_task["delivery_policy"],
                    "state": "worktree",
                    "events": [],
                },
            )
            delivery.update({
                "policy": live_task["delivery_policy"],
                "last_pushed_at": now(),
                "remote": remote,
                "remote_url": url,
                "branch": branch,
            })
            delivery.setdefault("events", []).append({
                "at": delivery["last_pushed_at"],
                "state": "branch-pushed",
                "remote": remote,
                "remote_url": url,
                "branch": branch,
                "grant_id": grant_id,
            })
            self._message(
                live,
                live_project,
                live_task,
                None,
                "status",
                f"Pushed {branch} to {remote} for review",
                {"remote": remote, "branch": branch, "grant_id": grant_id},
            )
        return {
            "task_id": task_id,
            "branch": branch,
            "remote": remote,
            "remote_url": url,
            "base_branch": task["base_branch"],
            "authorized_by": grant_id or "explicit --confirm",
        }
    def _workspace_clean(self, workspace: Path) -> bool:
        """Whether THIS workspace is clean -- never whether some other repo is.

        `git status` run in a directory that is not a repository silently
        ascends to the nearest enclosing one. A worktreeless role's directory
        lives under the Helm state tree and has no boundary of its own, so the
        question "is this workspace dirty" was being answered by the HELM
        REPOSITORY: an empty reviewer directory read as dirty because Helm's own
        checkout had an untracked file, and cleanup refused. A reviewer reported
        the same ascent from the other side, seeing Helm's commits when it asked
        for the project's.

        Answering from the wrong repository is the fault, not the direction of
        the answer -- the same read would have called a directory CLEAN because
        the parent happened to be. So the toplevel is resolved first, and git is
        trusted only when it is describing this directory. Otherwise the honest
        measure of a plain directory is whether it holds anything.
        """
        toplevel = _git(workspace, "rev-parse", "--show-toplevel", check=False).strip()
        if not toplevel or canonical(toplevel) != canonical(workspace):
            with contextlib.suppress(OSError):
                return not any(workspace.iterdir())
            return True
        status = _git(workspace, "status", "--porcelain=v1", "--untracked-files=all")
        unresolved = _git(workspace, "diff", "--name-only", "--diff-filter=U")
        return not self._dirt_worth_keeping(status) and not unresolved
    #: Helm's own output directory is not the worker's uncommitted work, and a
    #: guard that cannot tell them apart blocks cleanup on the evidence Helm
    #: put there itself. Four tasks were held open by exactly one untracked
    #: file under `.helm-out/`, written by the read-only lane that has nowhere
    #: else to put a deliverable. The task was finished, the checkout carried
    #: nothing of the worker's, and no operator action could clear it: deleting
    #: the file by hand is the one move that discards the report.
    #:
    #: Only this directory is disregarded, and only as a porcelain path. Build
    #: output, editor state and a genuinely modified tracked file all still
    #: count, because those are the cases the guard exists for.
    def _dirt_worth_keeping(self, status: str) -> str:
        kept = []
        for line in status.splitlines():
            path = line[3:].strip() if len(line) > 3 else ""
            if path.startswith('"') and path.endswith('"'):
                path = path[1:-1]
            path = path.split(" -> ")[-1]
            if path == self.READ_ONLY_OUTPUT_DIR or path.startswith(
                self.READ_ONLY_OUTPUT_DIR + "/"
            ):
                continue
            kept.append(line)
        return "\n".join(kept)
    def merge_task(self, task_id: str) -> dict[str, Any]:
        self.authority("merging a task branch")
        with self.store.locked() as data:
            task = self._task(data, task_id)
            self._refuse_read_only_delivery(task, "merged")
            project = self._project(data, task["project_id"])
            if task["delivery_policy"] != "local":
                raise SafetyError("PR delivery has no merge automation in v1; merge it through the approved external flow")
            if task["status"] != "approved" or not task.get("approval"):
                raise SafetyError("merge requires an explicit helm task approve command")
            self._require_terminal_worker(data, task, "merge", require_completed=True)
            workspace = self._verify_workspace_record(data, project, task)
            approval = task["approval"]
            current_revision = _git(workspace, "rev-parse", "HEAD")
            current_branch_tip = _git(workspace, "rev-parse", f"refs/heads/{task['branch']}")
            current_tree = _git(workspace, "rev-parse", "HEAD^{tree}")
            reviewed_revision = approval.get("revision", approval.get("branch_tip"))
            reviewed_branch_tip = approval.get("branch_tip", reviewed_revision)
            if (
                approval.get("branch") != task["branch"]
                or current_revision != reviewed_revision
                or current_branch_tip != reviewed_branch_tip
                or current_tree != approval.get("tree")
            ):
                task["approval"] = None
                task["status"] = "approval-needed"
                self._message(
                    data,
                    project,
                    task,
                    None,
                    "approval-invalidated",
                    "Reviewed worker revision changed; re-review is required",
                    {"reviewed_revision": reviewed_revision, "current_revision": current_revision},
                )
                # The rejection itself is durable: the old approval must not
                # remain usable after the lock context rolls back exceptions.
                self.store.save(data)
                raise SafetyError("reviewed worker content changed after approval; re-review is required")
            if not self._workspace_clean(workspace):
                raise SafetyError("refusing merge: worker workspace is dirty or unresolved")
            root = canonical(project["root"])
            # Helm's own project-local directory sits untracked in the base
            # checkout by design -- `.helm/project.json` is where a project
            # pins its agent or opts into turns -- so it is not dirtiness. It
            # refused every local merge on a project that had one.
            if _git(root, "status", "--porcelain", "--", ":!.helm", check=False):
                raise SafetyError("refusing merge: project main worktree is dirty")
            branch = _git(root, "symbolic-ref", "--quiet", "--short", "HEAD", check=False)
            if branch != task["base_branch"]:
                raise SafetyError(
                    f"refusing merge: project is on {branch or 'detached HEAD'}, expected {task['base_branch']}"
                )
            ahead = _git(root, "rev-list", "--count", f"{task['base_branch']}..{task['branch']}")
            if ahead == "0":
                raise SafetyError("worker branch has no commit to merge")
            _git(root, "merge", "--ff-only", task["branch"])
            task["status"] = "merged"
            task["merged_at"] = now()
            self._message(data, project, task, None, "merged", "Approved local fast-forward merge completed", {})
            self.resolve_delivery_decisions(
                project["id"], reason="merged", data=data
            )
            with contextlib.suppress(HelmError, OSError):
                self.refresh_finalization_decisions(project["id"], data=data)
        # Auto-cleanup, behind the root's own opt-in (cleanup.after_merge =
        # auto). The merge above was --ff-only into the base the task was cut
        # from, so the branch is provably contained in what just landed and
        # the residue holds nothing. This is the ONLY case the preference can
        # reach: failed, blocked, completed-unmerged and PR-delivery work all
        # still require the commander's word, and so does every root that
        # never opted in. A refusal here (dirty workspace, held branch)
        # un-merges nothing -- the manual path remains exactly as it was.
        if self.preferences().cleanup_after_merge == "auto":
            # Deliver BEFORE the worktree is destroyed. The comment above says
            # the residue holds nothing, and that is true of TRACKED files --
            # which is exactly the set a merge already moved. A build output
            # is the other set: gitignored, living only in the worktree, and
            # the actual product. Cleaning first deleted a finished render and
            # the caller's own delivery call then found no worktree and said
            # so into a suppressed exception, so nothing anywhere reported
            # that the deliverable was gone.
            with contextlib.suppress(HelmError, SafetyError, OSError):
                # Reported back on the returned record so the caller does not
                # have to ask again. Asking again is what produced a "build
                # outputs were NOT delivered" warning on every healthy
                # auto-cleaning merge: delivery had just succeeded, and the
                # second attempt found the worktree correctly gone.
                task["delivered_artifacts"] = self.deliver_task_artifacts(task_id)
            with contextlib.suppress(HelmError, SafetyError, OSError):
                self.cleanup_task(task_id, delete_branch=True)
        return task
    def _remove_task_branch(
        self,
        data: dict[str, Any],
        project: dict[str, Any],
        task: dict[str, Any],
        *,
        force: bool,
    ) -> None:
        """Delete a cleaned task's own branch without discarding its work.

        Cleanup refuses a dirty workspace to preserve work; commits the base
        branch does not have are that same work one step further along, so an
        unmerged branch survives cleanup too and says so. Without this the
        worktree went away and the branch stayed forever, so every cleaned
        task leaked a `helm/<project>/<task>` ref nobody could account for.
        """
        branch = task["branch"]
        # Only ever the branch Helm itself named for this task -- including the
        # ticketed form, which this test used to miss, leaking every ticketed
        # task's ref. A record that somehow carried a base or user branch must
        # not be deletable here.
        if not task_owns_branch(task):
            return
        root = canonical(project["root"])
        ref = f"refs/heads/{branch}"
        if not _git(root, "rev-parse", "--verify", "--quiet", ref, check=False):
            task["branch_removed"] = True
            return
        # A registration whose directory is already gone still makes git call
        # the branch checked out, which would refuse the delete below.
        _git(root, "worktree", "prune", check=False)
        counted = _git(
            root, "rev-list", "--count", f"{task['base_branch']}..{branch}", check=False
        )
        unmerged = int(counted) if counted.isdigit() else None
        if not force and unmerged != 0:
            task["branch_removed"] = False
            detail = (
                f"{unmerged} commit(s) not in {task['base_branch']}"
                if unmerged
                else f"its state against {task['base_branch']} could not be determined"
            )
            self._message(
                data, project, task, None, "cleanup",
                f"Task branch {branch} kept: {detail}; discard it with --delete-branch",
                {"branch": branch, "unmerged": unmerged},
            )
            return
        _git(root, "branch", "-D" if force else "-d", branch, check=False)
        removed = not _git(root, "rev-parse", "--verify", "--quiet", ref, check=False)
        task["branch_removed"] = removed
        self._message(
            data, project, task, None, "cleanup",
            f"Task branch {branch} deleted" if removed
            else f"Task branch {branch} could not be deleted; it may be checked out elsewhere",
            {"branch": branch, "unmerged": unmerged},
        )
    def _release_hold(
        self, data: dict[str, Any], project: dict[str, Any], task: dict[str, Any]
    ) -> str | None:
        """Why this task still holds something, or None when it can be released."""
        if task.get("role") in WORKTREELESS_ROLES:
            # No checkout and no branch; there is no work product to lose.
            return None
        if task["status"] in {"blocked", "failed"}:
            return f"{task['status']}: its log is the diagnosis"
        if task["status"] == "approval-needed":
            return "waiting on a human decision"
        if task["status"] in {"created", "allocated", "running"}:
            return f"still {task['status']}"
        if task["status"] in {"merged", "pr-merged"}:
            return None
        if task["status"] == "pr-open":
            return "PR open; monitor comments/checks until it merges"
        branch = task.get("branch")
        if not branch:
            return None
        root = canonical(project["root"])
        if not _git(root, "show-ref", "--verify", f"refs/heads/{branch}", check=False):
            return None
        ahead = _git(
            root, "rev-list", "--count", f"{task['base_branch']}..{branch}", check=False
        ).strip()
        if ahead and ahead != "0":
            # The change itself. Completed is not delivered: it is still
            # awaiting review, and review reads the branch.
            return f"holds {ahead} unmerged commit(s) on {branch}"
        return None
    def sweep_residue_under_grants(self, *, now_epoch: float | None = None) -> dict[str, Any]:
        """Shed what delivered and stale tasks still hold, where a cleanup grant says so.

        Cleanup stays the commander's decision; a grant is that decision made
        once. A delivered task (`merged`, `pr-merged`) holds nothing worth
        keeping, so its worktree, worker directories and branch go. With
        `stale_days` on the grant, a failed or never-launched task older than
        that goes too, branch included, and a completed-but-undelivered one
        sheds its worktree and directories while its branch -- finished work
        nobody decided on -- is kept and named. Every refusal cleanup would
        make by hand it still makes here, and is reported.
        """
        import time

        current = time.time() if now_epoch is None else now_epoch
        data = self.store.load()
        cleaned: list[dict[str, Any]] = []
        skipped: list[dict[str, Any]] = []
        without_grant = 0
        for task in sorted(data.get("tasks", {}).values(), key=lambda t: t.get("created_at") or ""):
            if not self.task_retained_resources(task, data):
                continue
            status = task.get("status")
            if status in {"approval-needed", "approved", "pr-open"} or task.get("role") == "foreman":
                continue
            grant = self.approval_grant_for("cleanup", task.get("project_id"))
            if grant is None:
                without_grant += 1
                continue
            delivered = status in DELIVERED_TASK_STATES
            stale_days = grant.get("stale_days")
            created = _stamp_epoch(task.get("created_at"))
            age_days = (current - created) / 86400 if created else 0.0
            if delivered:
                reason, delete_branch = f"delivered ({status})", True
            elif stale_days is not None and age_days >= stale_days and status in {"failed", "created", "allocated", "blocked"}:
                reason, delete_branch = f"{status} for {age_days:.0f} days", True
            elif stale_days is not None and age_days >= stale_days and status == "completed":
                reason, delete_branch = f"completed but undelivered for {age_days:.0f} days", False
            else:
                continue
            try:
                self.cleanup_task(task["id"], delete_branch=delete_branch)
            except (SafetyError, HelmError) as exc:
                skipped.append({"task_id": task["id"], "reason": _safe_text(str(exc))[:160]})
                continue
            with self.store.locked() as live:
                record = live["tasks"].get(task["id"])
                if record is not None:
                    record["cleaned_under_grant"] = grant["id"]
                    record["cleaned_under_grant_reason"] = reason
            cleaned.append({"task_id": task["id"], "reason": reason, "grant_id": grant["id"]})
        return {"cleaned": cleaned, "skipped": skipped, "without_grant": without_grant}

    def release_project(self, project_id: str) -> dict[str, Any]:
        """Release what a finished project still holds, and report what it kept.

        Closing a project's space releases the pane and nothing else, so the
        worktrees, worker directories and branches stayed. That is each
        decision behaving correctly and no step ever saying "this project is
        done, let go of what it holds" -- which is how tens of gigabytes
        accumulated behind projects Helm considered finished, with nothing
        reporting it.

        Deliberately a command rather than a side effect of the space closing.
        A human closing their own pane must not delete work, and a health check
        must not either: a completed task is not a delivered one, and its
        branch is what a review reads.

        What is kept is returned with the reason, because residue nobody is
        told about is how this got to 35 GB in the first place.
        """
        data = self.store.load()
        project = self._project(data, project_id)
        running = [
            worker["id"]
            for worker in data["workers"].values()
            if worker.get("project_id") == project_id and worker.get("status") == "running"
        ]
        if running:
            raise SafetyError(
                f"{project_id} still has running worker(s): {', '.join(sorted(running))}. "
                "Let them finish or stop them first."
            )
        released: list[str] = []
        kept: list[dict[str, str]] = []
        for task in sorted(
            (t for t in data["tasks"].values() if t.get("project_id") == project_id),
            key=lambda t: t.get("created_at") or "",
        ):
            hold = self._release_hold(data, project, task)
            if hold is not None:
                kept.append({"task_id": task["id"], "reason": hold})
                continue
            try:
                self.cleanup_task(task["id"])
                released.append(task["id"])
            except (SafetyError, HelmError) as exc:
                kept.append({"task_id": task["id"], "reason": _safe_text(str(exc))[:160]})
        return {"project_id": project_id, "released": released, "kept": kept}
    def cleanup_task(self, task_id: str, *, delete_branch: bool = False) -> dict[str, Any]:
        with self.store.locked() as data:
            task = self._task(data, task_id)
            project = self._project(data, task["project_id"])
            # A task that never launched a worker is shed here too: it holds a
            # checkout and nothing else, and it is the one shape that could
            # never satisfy "a terminal worker" no matter how long it waited.
            self._require_terminal_worker(data, task, "cleanup", allow_never_started=True)
            # Cleaning up a task that escalated IS the human answering it. The
            # ask stays live until something is recorded as an answer, and
            # nothing was: `helm worker answer` refuses a settled session, and
            # cleanup left the task `blocked`, so the escalation sat in the
            # attention list permanently. Twelve of them accumulated, some for
            # days, crowding out the asks a human could still act on -- and
            # every one of them had already been dealt with or abandoned.
            #
            # Recorded as an explicit answer rather than by deleting the
            # blocker, so what the worker reported survives in the log and the
            # decision to stop pursuing it is visible beside it.
            for worker in data.get("workers", {}).values():
                if worker.get("task_id") != task["id"]:
                    continue
                self._message(
                    data, project, task, worker, "answer",
                    "Resolved by cleanup: the commander cleared this task's "
                    "residue, so its escalation is no longer awaiting an answer.",
                    {"source": "cleanup"},
                )
            # A task no worker was ever assigned to holds a checkout with
            # nothing in it. The gate below exists to stop a directory being
            # pulled out from under work that is not reviewed yet -- and there
            # is no such work here, because none ever ran. Keyed on "never had
            # a worker" rather than on the status, so a task that is merely
            # *between* create and launch is still protected the moment one is
            # assigned, and `_require_terminal_worker` above already refuses
            # while any worker of this task is running.
            never_ran = not self._task_workers(data, task["id"])
            if (
                not never_ran
                and task["status"] not in {"completed", "failed", "merged", "pr-merged"}
                # The status gate protects a checkout: work not yet reviewed,
                # or waiting on approval, must not have the directory holding
                # it removed underneath. A role with no worktree has no such
                # directory -- a foreman's workspace is empty and its branch
                # does not exist -- and the record of why it stopped is its
                # blocker message, which lives in state and outlives cleanup.
                #
                # Applying the gate to them meant a foreman that escalated --
                # which is how a foreman is supposed to end -- left an empty
                # directory that nothing could ever shed, one per escalation,
                # for the life of the root.
                and task.get("role") not in WORKTREELESS_ROLES
            ):
                # `pr-open` is the one status here that is routinely stale
                # rather than genuinely unfinished. Nothing polls a pull
                # request after Helm hands it over, so a PR that merged hours
                # ago still reads as open, and this refusal -- correct on the
                # record it can see -- is the only thing the commander meets.
                # It said "preserve work awaiting approval" about work that had
                # already landed, with no hint that a command exists to settle
                # the question. Naming it costs nothing and turns a dead end
                # into one step.
                if task["status"] == "pr-open":
                    raise SafetyError(
                        "task is recorded as pr-open, so cleanup would remove a checkout whose "
                        "review may still be live. If the PR has since merged or closed, settle "
                        f"the record first with: helm task pr-sync {task['id']}"
                    )
                raise SafetyError(
                    "cleanup is allowed only for completed, failed, or merged tasks; preserve work awaiting approval"
                )
            # A worktree removed outside Helm -- by hand, or by a tool that
            # got there first -- left the record claiming it still exists,
            # with cleanup the only command that could correct it and cleanup
            # refusing because the directory was gone. Reconcile instead:
            # there is nothing to protect in a directory that is not there,
            # and a record nobody can correct is its own kind of failure.
            if task.get("workspace_removed") or not canonical(task["workspace"]).exists():
                if not task.get("workspace_removed"):
                    task["workspace_removed"] = True
                    task["workspace_removed_at"] = now()
                    self._message(
                        data, project, task, None, "cleanup",
                        "Worker workspace was already gone; record reconciled", {},
                    )
                # A reconciled record still owns its branch, and this early
                # return was the one cleanup path that could never shed it.
                # It owns its worker directories for the same reason: a task
                # cleaned before those were removed at all would otherwise have
                # no command able to reach them again.
                self._remove_worker_directories_locked(data, task)
                self._remove_task_branch(data, project, task, force=delete_branch)
                self.resolve_delivery_decisions(
                    project["id"], reason="cleaned up", data=data
                )
                with contextlib.suppress(HelmError, OSError):
                    self.refresh_finalization_decisions(project["id"], data=data)
                return task
            workspace = self._verify_workspace_record(data, project, task)
            # WORKTREELESS_ROLES own a directory, not a checkout, so neither has
            # git state to be dirty. The exemption named only `foreman`, which
            # left every reviewer cleanup running a git check against a
            # directory that is not a repository -- see `_workspace_clean`.
            if task.get("role") not in WORKTREELESS_ROLES and not self._workspace_clean(workspace):
                raise SafetyError("refusing cleanup: workspace is dirty or has unresolved changes")
            # The clean check above is a snapshot; a session still alive in
            # this directory can write to it a moment later, and the removal
            # below is forced. Stopping the worker is what ends the session,
            # and it records the exit this looks for.
            still_live = [
                worker
                for worker in self._task_workers(data, task["id"])
                if self._session_still_live(worker)
            ]
            if still_live:
                raise SafetyError(
                    f"refusing cleanup: worker {still_live[0]['id']} reported a terminal result "
                    f"but its session has not ended; end it with "
                    f"helm worker stop {still_live[0]['id']} first"
                )
            # --force is safe here and necessary. Git refuses outright to
            # remove a worktree containing populated submodules, which made
            # cleanup impossible for any project that has one -- their
            # worktrees accumulated forever with no supported way to remove
            # them. The check --force overrides is git's own dirty check, and
            # Helm has already done that itself two lines above and refused;
            # so this widens nothing, it only gets past the submodule refusal.
            # A read-only task's directory tree was locked to keep an agent
            # from writing into it; removal is Helm's own doing and needs the
            # write bit back first, or `rmtree`/`worktree remove` cannot even
            # unlink the files it just confirmed are clean.
            if task.get("read_only"):
                self._set_workspace_writable(workspace, writable=True)
            if task.get("role") in WORKTREELESS_ROLES:
                # A plain Helm-owned directory, so there is no worktree to
                # deregister -- and _verify_workspace_record has just confirmed
                # it is inside Helm's own state before anything is removed.
                shutil.rmtree(workspace)
            else:
                _git(canonical(project["root"]), "worktree", "remove", "--force", str(workspace))
            task["workspace_removed"] = True
            task["workspace_removed_at"] = now()
            self._remove_worker_directories_locked(data, task)
            self._message(data, project, task, None, "cleanup", "Clean worker workspace removed", {})
            self._remove_task_branch(data, project, task, force=delete_branch)
            self.resolve_delivery_decisions(
                project["id"], reason="cleaned up", data=data
            )
            # Cleanup is what answers this gate -- but only for what it
            # actually shed. A branch kept because it holds unmerged commits
            # leaves the item open, now naming just the branch.
            with contextlib.suppress(HelmError, OSError):
                self.refresh_finalization_decisions(project["id"], data=data)
            return task
