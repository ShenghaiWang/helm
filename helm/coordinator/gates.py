"""The requirement and solution gates that authorize one state-changing task.

A mixin over `CoordinatorBase`, split out of `status` -- which had grown to
cover seven of these at once. Moved verbatim; it imports nothing from
`helm.core`.
"""

from __future__ import annotations

import contextlib
import copy
import re
import sys
from typing import Any

from ..errors import HelmError, SafetyError
from ..values import GATE_TYPES, REQUIREMENT_GATE_KIND, SOLUTION_GATE_KIND, _safe_text, now


class GatesMixin:
    def _inherited_gates(self, data: dict[str, Any], project_id: str) -> dict[str, Any]:
        """Carry an UNSPENT gate decision onto a project's next foreman task.

        Gates were recorded on one foreman task row and read from the project's
        *live* foreman, so replacing the foreman -- it failed, was stopped, or
        stood down having finished -- silently discarded the commander's
        decision. The successor came up with empty gates, `gate propose --type
        solution` refused for want of a decided requirement, and the pair had to
        be confirmed again.

        The cost is not the second keystroke, it is that the loss is INVISIBLE:
        a fresh foreman with empty gates is indistinguishable from one that has
        simply not proposed yet, so a decision that WAS made reads as a decision
        still pending. A confirmed pair is a statement about the work -- this
        project may do this next thing -- not about whoever happens to be
        driving it, and a driver dying must not revoke it.

        A SPENT pair is deliberately not carried. Once `bound_task_id` names the
        task that consumed it, the authorization is used, and copying it onto a
        new foreman would hand out a second state-changing task on one
        confirmation -- exactly the accounting the binding exists to enforce.
        """
        newest: dict[str, Any] | None = None
        for task in data.get("tasks", {}).values():
            if task.get("project_id") != project_id or task.get("role") != "foreman":
                continue
            gates = task.get("gates") or {}
            if gates.get("bound_task_id") is not None:
                continue
            if not any(self._decided(gates.get(gate_type)) for gate_type in GATE_TYPES):
                continue
            if newest is None or str(task.get("created_at") or "") > str(
                newest.get("created_at") or ""
            ):
                newest = task
        if newest is None:
            return {gate_type: None for gate_type in GATE_TYPES}
        carried = {
            gate_type: copy.deepcopy(newest["gates"].get(gate_type))
            for gate_type in GATE_TYPES
        }
        # Never carried: an unspent pair becomes spendable again on the new
        # task, and it must be the NEW task's binding that records that.
        carried["bound_task_id"] = None
        carried["bound_at"] = None
        return carried

    def _require_gates_confirmed(
        self,
        data: dict[str, Any],
        project_id: str,
        *,
        consume_for_task_id: str | None = None,
    ) -> None:
        """Refuse a state-changing worker task until both gates are settled.

        Helm never reasons about whether the requirement or the solution is
        *right* -- that is the foreman's and the commander's business. It only
        enforces that both decisions were made before anything can change
        project state.

        A confirmed requirement/solution pair authorizes exactly one *new*
        state-changing task, not an open-ended stream of them: once both
        gates are settled, `gates["bound_task_id"]` records which task spent
        that authorization. `consume_for_task_id` is how a caller both checks
        and spends it in the same locked pass -- pass the task this
        confirmation is for (a brand-new task's id in `create_task`, or the
        same task's own id in `continue_task`, which is never a *new*
        authorization). Binding to the same task twice is a no-op, so a
        continuation round on the task that already holds the binding never
        re-prompts; binding to a second, different task while the pair is
        still held by the first is refused until the foreman materially
        changes scope -- proposing either gate again clears the binding along
        with the stale confirmation, see `propose_gate`. Passing `None`
        checks without consuming, for a caller that only needs to know
        whether the gates are currently settled.
        """
        foreman_task = self._live_foreman_task_in(data, project_id)
        if foreman_task is None:
            raise HelmError(
                "no live foreman for this project; a state-changing worker task "
                "needs its project's foreman to propose the requirement and "
                "solution gates and have the commander decide each one first "
                "(helm foreman <project>; helm gate propose/decide) -- or pass "
                "--read-only for investigation-only work that changes nothing"
            )
        gates = dict(foreman_task.get("gates") or {})
        for gate_type in GATE_TYPES:
            gate = gates.get(gate_type)
            if gate is None:
                raise HelmError(
                    f"the {gate_type} gate has not been proposed yet for this "
                    f"project's foreman task {foreman_task['id']}; the foreman "
                    f"must run `helm gate propose {foreman_task['id']} --type "
                    f"{gate_type} --text \"...\"` and wait for the commander's "
                    "decision before a state-changing worker can launch"
                )
            if gate.get("confirmed_at") is None and not gate.get("skipped"):
                raise HelmError(
                    f"the {gate_type} gate for foreman task {foreman_task['id']} "
                    "is still waiting on the commander (helm gate decide "
                    f"{foreman_task['id']} --type {gate_type} --confirm|--skip); "
                    "no state-changing worker can launch until it is decided"
                )
        # A task whose authorization was archived by a later proposal is
        # still authorized: continuation rounds on it must not re-prompt the
        # commander for a decision that was made and spent on this very task.
        if (
            consume_for_task_id is not None
            and consume_for_task_id in (gates.get("spent") or {})
        ):
            return
        bound_task_id = gates.get("bound_task_id")
        if bound_task_id is not None and bound_task_id != consume_for_task_id:
            raise HelmError(
                "the confirmed requirement/solution pair for foreman task "
                f"{foreman_task['id']} already authorized task {bound_task_id}; "
                "it authorizes one new state-changing task only -- propose the "
                f"requirement or solution gate again on {foreman_task['id']} "
                "and have the commander reconfirm it to authorize another"
            )
        if consume_for_task_id is not None and bound_task_id != consume_for_task_id:
            # A confirmed pair is spendable only by the side that proposed it.
            # Without this, the coordinator and the foreman each read "both
            # gates confirmed" as their green light and spawn duplicate
            # workers for the same change -- observed three times in one day.
            pair_owner = gates.get("pair_owner")
            if pair_owner is not None:
                identity = self.caller_identity()
                if identity["role"] != "root" and identity.get("worker_id") != pair_owner:
                    owner_label = (
                        "the root coordinator" if pair_owner == "root"
                        else f"foreman worker {pair_owner}"
                    )
                    raise HelmError(
                        f"the confirmed gate pair on {foreman_task['id']} was "
                        f"proposed by {owner_label}, so this foreman may not spend "
                        "it on a new task -- let the proposer create the task, "
                        "or re-propose the gates yourself and have them "
                        "reconfirmed"
                    )
            gates["bound_task_id"] = consume_for_task_id
            gates["bound_at"] = now()
            foreman_task["gates"] = gates

    def _consume_gates_if_settled(
        self, data: dict[str, Any], project_id: str, task_id: str
    ) -> None:
        """Spend a live confirmed pair on root's own task, when one exists.

        Root is never gated, but a confirmed pair left unspent after root's
        launch is a second green light: the foreman reads it and spawns a
        duplicate worker for the same change. Binding it to root's task makes
        the foreman's later create refuse instead. No foreman or no settled
        pair means nothing to spend, and nothing is required.
        """
        foreman_task = self._live_foreman_task_in(data, project_id)
        if foreman_task is None:
            return
        gates = dict(foreman_task.get("gates") or {})
        if gates.get("bound_task_id") is not None:
            return
        for gate_type in GATE_TYPES:
            gate = gates.get(gate_type)
            if gate is None:
                return
            if gate.get("confirmed_at") is None and not gate.get("skipped"):
                return
        gates["bound_task_id"] = task_id
        gates["bound_at"] = now()
        foreman_task["gates"] = gates

    def propose_gate(self, task_id: str, gate_type: str, text: str) -> dict[str, Any]:
        """Record a foreman's requirement or solution proposal for the commander.

        This is the foreman's half of the gate: it states what it wants
        decided, it never decides it. Proposing `requirement` always resets
        both gates -- a fresh requirement invalidates any solution built on
        the old one. Proposing `solution` requires a settled requirement and
        resets only the solution gate, so a material solution change never
        silently keeps a stale confirmation.
        """
        if gate_type not in GATE_TYPES:
            raise HelmError(f"gate type must be one of {sorted(GATE_TYPES)}")
        text = _safe_text(text).strip()
        if not text:
            raise HelmError("a gate proposal needs --text describing it")
        # Identified before the lock, same as `authority()` elsewhere: reading
        # it from the store this transaction is about to mutate would be
        # circular, and the identity itself never changes within one call.
        identity = self.caller_identity()
        with self.store.locked() as data:
            task = self._task(data, task_id)
            if task.get("role") != "foreman":
                raise HelmError("only a project's foreman task carries confirmation gates")
            if identity["role"] != "root":
                # Strict project isolation: a foreman proposes gates only for
                # the one project it drives. Without this, a foreman task's
                # `role == "foreman"` check alone let any live foreman write
                # (and, via requirement re-proposal, invalidate) another
                # project's gates -- a cross-project write this root-only
                # `decide_gate` boundary does not otherwise catch, since the
                # commander still decides either way.
                caller_worker = data.get("workers", {}).get(identity["worker_id"]) or {}
                caller_task = data.get("tasks", {}).get(caller_worker.get("task_id")) or {}
                if caller_task.get("project_id") != task["project_id"]:
                    raise SafetyError(
                        "a foreman may only propose gates for the project it "
                        f"drives ({caller_task.get('project_id') or 'unknown'}), "
                        f"not {task['project_id']}"
                    )
            proposal = {
                "text": text,
                "proposed_at": now(),
                "confirmed_at": None,
                "skipped": False,
                "note": "",
            }
            gates = dict(task.get("gates") or {})
            # A CONFIRMED PAIR THAT NOTHING HAS SPENT IS A LIVE COMMANDER
            # DECISION, AND OVERWRITING IT USED TO BE SILENT. On 2026-08-23 a
            # confirmed requirement for one round vanished when the next round
            # proposed on the same foreman task; only the foreman noticing kept
            # that round from running ungated.
            #
            # REFUSING IT WOULD BE WRONG, and that was tried first: a foreman
            # re-proposing its own gate before anything spends it is legitimate
            # self-correction, and one did exactly that the same evening --
            # withdrawing a proposal it had thought better of. Blocking that
            # breaks the mechanism in the name of protecting it.
            #
            # So the fix is the silence, not the overwrite. Say what is being
            # discarded, on the record, where the commander reads it.
            at_risk = ("requirement", "solution") if gate_type == "requirement" else ("solution",)
            discarded = [
                name
                for name in at_risk
                if (gates.get(name) or {}).get("confirmed_at")
                and not gates.get("bound_task_id")
            ]
            # A SPENT PAIR IS ARCHIVED, NOT ERASED -- and archiving is what
            # makes the slot parallel. The pair the commander confirmed for
            # task X keeps describing task X forever; a new proposal is about
            # a DIFFERENT piece of work and needs its own slot, so the spent
            # one moves to `spent` (keyed by the task it authorized) and the
            # live slot opens immediately.
            #
            # Before this, a foreman driving two tasks could not propose the
            # second one's gates until the first task finished, because the
            # single slot was still held by a binding nobody could release --
            # observed stalling M3 behind M2 for forty minutes with both
            # workers idle. The authorization accounting is unchanged: one
            # confirmed pair still authorizes exactly one task, and the
            # archive is what proves which.
            bound = gates.get("bound_task_id")
            if bound is not None:
                archive = dict(gates.get("spent") or {})
                archive[bound] = {
                    "requirement": copy.deepcopy(gates.get("requirement")),
                    "solution": copy.deepcopy(gates.get("solution")),
                    "bound_at": gates.get("bound_at"),
                    "pair_owner": gates.get("pair_owner"),
                }
                gates["spent"] = archive
            gates["bound_task_id"] = None
            gates["bound_at"] = None
            # The proposing side owns the pair: only it may later spend the
            # confirmation on a new task, so root and foreman can never both
            # treat one confirmation as their own green light.
            gates["pair_owner"] = (
                "root" if identity["role"] == "root" else identity.get("worker_id")
            )
            if gate_type == "requirement":
                gates["requirement"] = proposal
                gates["solution"] = None
            else:
                requirement = gates.get("requirement")
                if requirement is None or (
                    requirement.get("confirmed_at") is None and not requirement.get("skipped")
                ):
                    raise HelmError(
                        "the requirement gate must be decided before a solution "
                        "can be proposed"
                    )
                gates["solution"] = proposal
            task["gates"] = gates
            project = self._project(data, task["project_id"])
            # Name what this proposal threw away, in the message the commander
            # actually reads. An unspent confirmation is a decision they made
            # and have not yet seen used; it must not disappear quietly.
            announcement = f"Foreman proposed the {gate_type} gate; waiting on the commander"
            if discarded:
                announcement += (
                    f" -- THIS DISCARDED A CONFIRMED, UNSPENT DECISION: "
                    f"{' and '.join(discarded)}. No task had consumed it, so if that "
                    "decision still stood, it has to be made again."
                )
            self._message(
                data, project, task, None, "status",
                announcement,
                {"gate": gate_type, "text": text, "discarded_confirmed": discarded},
            )
            project_id = task["project_id"]
            result = dict(task)
        kind = REQUIREMENT_GATE_KIND if gate_type == "requirement" else SOLUTION_GATE_KIND
        self._close_gate_action_item(project_id, task_id, gate_type, reason="re-proposed")
        if gate_type == "requirement":
            self._close_gate_action_item(project_id, task_id, "solution", reason="reset")
        # The full proposal is already durable in the task's own `gates` field
        # and in the message just recorded above; this line only has to stay
        # under the action-item limit, not carry the whole contract. A
        # realistic requirement/solution text (goal, scope, exclusions,
        # acceptance evidence) regularly runs past SITUATION_LINE_LIMIT, and
        # `record_project_action_item` refuses rather than truncates -- so
        # building it unbounded made the decision silently vanish from
        # `open_action_items()` while the foreman was told it was waiting.
        with contextlib.suppress(HelmError, OSError):
            self.record_project_action_item(
                project_id,
                self._situation_line(
                    f"Decide the {gate_type} gate for foreman task {task_id}: ", text
                ),
                source="foreman",
                task_id=task_id,
                key=f"{task_id}:{gate_type}",
                kind=kind,
            )
        return result

    def decide_gate(
        self, task_id: str, gate_type: str, *, confirm: bool, skip: bool, note: str = ""
    ) -> dict[str, Any]:
        """Record the commander's decision on a proposed gate. Root-only.

        Exactly one of `confirm`/`skip` must be true. Neither a foreman, a
        worker, nor any project or domain text can call this: `self.authority`
        is the same boundary that guards approve/merge/grant.
        """
        if confirm == skip:
            raise HelmError("decide exactly one of --confirm or --skip")
        if gate_type not in GATE_TYPES:
            raise HelmError(f"gate type must be one of {sorted(GATE_TYPES)}")
        self.authority(f"deciding the {gate_type} gate")
        with self.store.locked() as data:
            task = self._task(data, task_id)
            if task.get("role") != "foreman":
                raise HelmError("only a project's foreman task carries confirmation gates")
            gates = dict(task.get("gates") or {})
            gate = gates.get(gate_type)
            if gate is None:
                raise HelmError(
                    f"no {gate_type} gate has been proposed yet on task {task_id}"
                )
            gate = dict(gate)
            gate["confirmed_at"] = None if skip else now()
            gate["skipped"] = bool(skip)
            gate["note"] = _safe_text(note).strip()
            gates[gate_type] = gate
            task["gates"] = gates
            project = self._project(data, task["project_id"])
            verb = "skipped" if skip else "confirmed"
            self._message(
                data, project, task, None, "status",
                f"Commander {verb} the {gate_type} gate",
                {"gate": gate_type, "decision": verb},
            )
            project_id = task["project_id"]
            result = dict(task)
        self._close_gate_action_item(project_id, task_id, gate_type, reason=verb)
        return result

    def _close_gate_action_item(
        self, project_id: str, task_id: str, gate_type: str, *, reason: str
    ) -> None:
        """Close the commander-visible decision an already-decided gate raised.

        A re-proposed gate also reaches here first, since a stale open item
        naming the old text would otherwise sit next to the fresh one under
        the same key.
        """
        marker = f"{task_id}:{gate_type}"
        try:
            with self._status_transaction(project_id) as status:
                for item in status["action_items"]:
                    if item.get("key") == marker and item.get("status", "open") == "open":
                        item["status"] = reason
                        item["resolved_at"] = now()
        except OSError as exc:
            # The gate decision itself already committed in its own earlier
            # transaction, so failing here cannot un-decide it -- raising
            # would report a decision that was in fact recorded. But staying
            # silent is worse than it looks: the commander-visible item stays
            # open, `helm pending` keeps asking for a decision that was
            # already made, and the only symptom is a gate that will not stop
            # nagging. Say so on the one surface the caller is reading.
            print(
                f"warning: the {gate_type} gate on {task_id} was {reason}, but its "
                f"commander-visible item could not be closed ({exc}). It will keep "
                f"showing in `helm pending`; re-run this decide once the status "
                f"record for {project_id} is writable.",
                file=sys.stderr,
            )
