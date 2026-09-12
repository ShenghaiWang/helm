"""Proposing a domain learning, deciding it, and applying it.

A mixin over `CoordinatorBase`, moved out of `core` unchanged. It resolves
every cross-call through `self` at runtime and imports nothing from
`helm.core`.
"""

from __future__ import annotations

import contextlib
import json
import os
import re

from pathlib import Path
from typing import Any, Sequence

from ..errors import HelmError, SafetyError
from ..paths import canonical, inside
from ..values import (
    LEARNING_EVIDENCE_KINDS,
    LEARNING_PROPOSAL_STATUSES,
    _safe_text,
    _validate_domain_id,
    new_id,
    now,
)


def _learning_fact_key(value: str) -> str:
    """Normalize a proposed fact for duplicate detection without rewriting it."""
    return " ".join(re.findall(r"[a-z0-9]+", value.lower())).strip()


def _learning_polarity_and_core(value: str) -> tuple[int, set[str]]:
    words = _learning_fact_key(value).split()
    negative = bool(set(words) & {"no", "not", "never", "avoid", "without", "dont", "don't"})
    stop = {
        "a", "an", "and", "are", "be", "do", "for", "in", "is", "of", "on",
        "should", "the", "to", "with", "use", "always", "must", "never", "not", "no",
        "avoid", "without", "dont", "don't",
    }
    return (-1 if negative else 1), {word for word in words if word not in stop}


_REPORT_PREFIXES = (
    "approved", "approve", "changes-requested", "pass", "fail", "verdict", "done", "delivered",
    "review complete", "re-review", "pr #", "row ", "merged", "pushed", "completed", "fixed",
)


def _reads_as_report(text: str) -> bool:
    """A verdict, a delivery note or an evidence summary is not a fact.

    A fact says what to do next time. A report says what happened this
    time: it opens with a verdict word, cites a PR, a URL, a path or an exit
    code, or is a paragraph rather than a sentence.
    """
    stripped = " ".join(text.split())
    lowered = stripped.lower().lstrip("#*- ")
    if any(lowered.startswith(prefix) for prefix in _REPORT_PREFIXES):
        return True
    if re.search(r"https?://|/users/|state/|\bexit \d|\bpr #\d|\btip [0-9a-f]{7}|\b[0-9a-f]{40}\b", lowered):
        return True
    return len(stripped) > 400


def _learning_facts_conflict(left: str, right: str) -> bool:
    """Find obvious opposing rules while avoiding broad semantic guesses."""
    if _learning_fact_key(left) == _learning_fact_key(right):
        return False
    left_polarity, left_core = _learning_polarity_and_core(left)
    right_polarity, right_core = _learning_polarity_and_core(right)
    if left_polarity == right_polarity or not left_core or not right_core:
        return False
    overlap = len(left_core & right_core)
    return overlap >= 1 and overlap / max(len(left_core), len(right_core)) > 0.5


#: Helm's own generated identifiers, by prefix, and what to call one in prose.
_IDENTIFIER_WORDS = {
    "t": "a task", "w": "a worker", "m": "a message", "i": "an item",
    "s": "a status entry", "a": "an artifact", "g": "a grant",
}
_HELM_IDENTIFIER = re.compile(r"\b([twmisag])-[0-9a-f]{8,}\b")
#: A tracker ticket such as ABC-123: one root's history, not knowledge.
_TICKET = re.compile(r"\b[A-Z][A-Z0-9]{1,9}-\d{1,6}\b")


def scrub_identifiers(text: str) -> str:
    """Replace one root's identifiers with the kind of thing they named.

    A learning is worth keeping because it generalises; the ids in its
    rationale are the evidence, and the evidence stays in state. Written into
    a tracked domain file they would make Helm's shipped knowledge carry a
    managed root's task, message and ticket history.
    """
    scrubbed = _HELM_IDENTIFIER.sub(lambda m: _IDENTIFIER_WORDS.get(m.group(1), "an item"), text or "")
    return _TICKET.sub("a ticket", scrubbed)


class LearningMixin:
    # ---------- learning proposals ----------

    @staticmethod
    def _learning_actor_allowed(
        proposal: dict[str, Any], actor: str, operation: str
    ) -> str:
        """Keep worker data and source identities out of promotion commands."""
        actor = _safe_text(actor).strip()
        if not actor:
            raise SafetyError(f"{operation} requires an explicit user or coordinator actor")
        source_workers = {
            str(worker_id)
            for worker_id in proposal.get("source_references", {}).get("worker_ids", [])
        }
        if actor.lower() in {"worker", "worker-result", "proposal", "domain-file", "automated"}:
            raise SafetyError(f"{operation} cannot be performed by worker output or proposal data")
        if actor in source_workers or actor.removeprefix("worker:") in source_workers:
            raise SafetyError(f"{operation} cannot be self-approved by the source worker")
        return actor

    def _learning_domain_file(
        self,
        project: dict[str, Any],
        domain_id: str,
        *,
        create: bool = False,
    ) -> Path:
        domain_id = _validate_domain_id(domain_id)
        domain_root = self._domain_root(project)
        if domain_root is None:
            raise HelmError("a Helm root is required to apply learning to domain knowledge")
        if domain_root.is_symlink():
            raise SafetyError(f"Helm domains directory must not be a symlink: {domain_root}")
        if create:
            domain_root.mkdir(parents=True, exist_ok=True)
        safe_root = self._safe_configuration_path(
            domain_root, domain_root.parent, "Helm domains directory"
        )
        domain_dir = safe_root / domain_id
        if domain_dir.is_symlink():
            raise SafetyError(f"domain directory must not be a symlink: {domain_dir}")
        if create:
            domain_dir.mkdir(parents=True, exist_ok=True)
        safe_dir = self._safe_configuration_path(domain_dir, safe_root, "domain directory")
        knowledge = safe_dir / "knowledge.md"
        if knowledge.is_symlink():
            raise SafetyError(f"domain knowledge file must not be a symlink: {knowledge}")
        if knowledge.exists() and not knowledge.is_file():
            raise SafetyError(f"domain knowledge path is not a file: {knowledge}")
        return self._safe_configuration_path(knowledge, safe_root, "domain knowledge file")

    @staticmethod
    def _clean_learning_text(value: Any, label: str, limit: int) -> str:
        text = " ".join(_safe_text(value).split()).strip()
        if not text:
            raise HelmError(f"learning {label} is required")
        if len(text) > limit:
            raise HelmError(f"learning {label} must be at most {limit} characters")
        return text

    @staticmethod
    def _learning_confidence(value: Any) -> float:
        if value is None:
            return 0.6
        if isinstance(value, bool):
            raise HelmError("learning confidence must be a number from 0 to 1")
        try:
            confidence = float(value)
        except (TypeError, ValueError) as exc:
            raise HelmError("learning confidence must be a number from 0 to 1") from exc
        if not 0 <= confidence <= 1:
            raise HelmError("learning confidence must be a number from 0 to 1")
        return round(confidence, 4)

    @staticmethod
    def _learning_core_override(value: str) -> bool:
        return bool(re.search(
            r"\b(?:ignore|override|bypass|disable|weaken|skip|without)\b"
            r".{0,80}\b(?:helm|safety|guardrail|approval|credential|secret|publish|push|merge|"
            r"destructive|isolation)\b",
            value,
            re.IGNORECASE,
        ))

    @staticmethod
    def _learning_artifact_reference(
        artifact: dict[str, Any], task: dict[str, Any]
    ) -> dict[str, Any]:
        if artifact.get("task_id") != task["id"] or artifact.get("project_id") != task["project_id"]:
            raise SafetyError("learning source artifact belongs to a different task or project")
        raw_path = artifact.get("path")
        if not isinstance(raw_path, str) or not raw_path or Path(raw_path).is_absolute():
            raise SafetyError("learning source artifact has an invalid path")
        relative = Path(raw_path)
        if ".." in relative.parts:
            raise SafetyError("learning source artifact escapes the task workspace")
        workspace = canonical(task["workspace"])
        candidate = canonical(workspace / relative)
        if not inside(candidate, workspace):
            raise SafetyError("learning source artifact escapes the task workspace")
        recorded_workspace = artifact.get("workspace")
        if recorded_workspace and canonical(recorded_workspace) != workspace:
            raise SafetyError("learning source artifact is outside the task workspace")
        return {
            "id": artifact["id"],
            "path": relative.as_posix(),
            "description": artifact.get("description", ""),
            "kind": artifact.get("kind", "file"),
            "worker_id": artifact.get("worker_id"),
        }

    def _learning_sources_locked(
        self,
        data: dict[str, Any],
        task: dict[str, Any],
        *,
        artifact_ids: Sequence[str] | None = None,
        message_ids: Sequence[str] | None = None,
    ) -> dict[str, Any]:
        task_worker_ids = {
            worker["id"] for worker in self._task_workers(data, task["id"])
        }
        task_artifacts = [
            artifact for artifact in data.get("artifacts", [])
            if artifact.get("task_id") == task["id"]
        ]
        requested_artifacts = list(artifact_ids) if artifact_ids is not None else [
            artifact["id"] for artifact in task_artifacts
        ]
        artifact_by_id = {artifact.get("id"): artifact for artifact in task_artifacts}
        artifacts: list[dict[str, Any]] = []
        for artifact_id in requested_artifacts:
            artifact = artifact_by_id.get(artifact_id)
            if artifact is None:
                raise SafetyError(f"learning source artifact is not attached to task {task['id']}: {artifact_id}")
            if artifact.get("worker_id") not in task_worker_ids:
                raise SafetyError("learning source artifact was not produced by this task's worker")
            artifacts.append(self._learning_artifact_reference(artifact, task))

        task_messages = [
            message for message in data.get("messages", [])
            if message.get("task_id") == task["id"]
        ]
        # Defaulting to every message on the task meant a proposal cited the
        # worker's entire terminal scrollback as its provenance -- 1.65 MB of
        # references each, for twenty proposals, none of it evidence of
        # anything. What a learning is drawn from is what the worker reported
        # and produced, so that is what is cited when nothing is named.
        requested_messages = list(message_ids) if message_ids is not None else [
            message["id"] for message in task_messages
            if message.get("kind") in LEARNING_EVIDENCE_KINDS
        ]
        message_by_id = {message.get("id"): message for message in task_messages}
        messages: list[dict[str, Any]] = []
        for message_id in requested_messages:
            message = message_by_id.get(message_id)
            if message is None:
                raise SafetyError(f"learning source message is not attached to task {task['id']}: {message_id}")
            if message.get("project_id") != task["project_id"]:
                raise SafetyError("learning source message belongs to a different project")
            if message.get("worker_id") is not None and message.get("worker_id") not in task_worker_ids:
                raise SafetyError("learning source message was not produced by this task's worker")
            messages.append({
                "id": message["id"],
                "kind": message.get("kind"),
                "worker_id": message.get("worker_id"),
            })
        worker_ids = sorted({
            str(worker_id)
            for worker_id in [
                *(message.get("worker_id") for message in task_messages),
                *(artifact.get("worker_id") for artifact in task_artifacts),
            ]
            if worker_id
        })
        review_messages = [
            message["id"] for message in task_messages
            if message.get("kind") in {"approval", "approval-invalidated", "merged"}
        ]
        return {
            "task": {
                "id": task["id"],
                "project_id": task["project_id"],
                "status": task["status"],
                "brief": task["brief"],
            },
            "artifacts": artifacts,
            "messages": messages,
            "review": {
                "approval": task.get("approval"),
                "message_ids": review_messages,
            },
            "worker_ids": worker_ids,
        }

    def _learning_domain_conflicts_locked(
        self,
        data: dict[str, Any],
        project: dict[str, Any],
        domain_id: str,
        fact: str,
        *,
        exclude_proposal_id: str | None = None,
    ) -> list[dict[str, Any]]:
        conflicts: list[dict[str, Any]] = []
        knowledge_path = self._learning_domain_file(project, domain_id)
        if knowledge_path.exists():
            content = self._read_knowledge(
                knowledge_path, self._domain_root(project) or knowledge_path.parent
            )[0]
            for line_number, line in enumerate(content.splitlines(), 1):
                candidate = line.strip().lstrip("- ")
                if candidate.startswith("Fact:"):
                    candidate = candidate[5:].strip()
                if not candidate or candidate.startswith("#") or candidate.startswith("<!--"):
                    continue
                if _learning_fact_key(candidate) == _learning_fact_key(fact):
                    conflicts.append({
                        "type": "duplicate-domain-knowledge",
                        "source": str(knowledge_path),
                        "line": line_number,
                        "text": candidate[:500],
                    })
                elif _learning_facts_conflict(candidate, fact):
                    conflicts.append({
                        "type": "contradictory-domain-knowledge",
                        "source": str(knowledge_path),
                        "line": line_number,
                        "text": candidate[:500],
                    })
        for other in data.get("learning_proposals", []):
            if other.get("id") == exclude_proposal_id:
                continue
            if other.get("domain_id") != domain_id or other.get("status") == "rejected":
                continue
            other_fact = other.get("proposed_fact", "")
            if _learning_fact_key(other_fact) == _learning_fact_key(fact):
                conflicts.append({
                    "type": "duplicate-learning-proposal",
                    "proposal_id": other.get("id"),
                    "status": other.get("status"),
                    "text": other_fact,
                })
            elif _learning_facts_conflict(other_fact, fact):
                conflicts.append({
                    "type": "contradictory-learning-proposal",
                    "proposal_id": other.get("id"),
                    "status": other.get("status"),
                    "text": other_fact,
                })
        return conflicts

    def _resolve_learning_domain_locked(
        self,
        data: dict[str, Any],
        task: dict[str, Any],
        project: dict[str, Any],
        explicit: str | None,
    ) -> tuple[str, str]:
        task_domain = task.get("domain")
        if explicit is not None:
            selected = _validate_domain_id(explicit)
            if task_domain and selected != task_domain:
                raise SafetyError(
                    f"learning domain {selected} does not match the task domain {task_domain}"
                )
            if not task_domain:
                known = set(self._project_domains(project)) | set(self._known_domain_ids(project))
                if known and selected not in known:
                    raise SafetyError(
                        f"learning domain {selected} is not associated with task {task['id']}"
                    )
            return selected, "explicit learning domain"
        if task_domain:
            return _validate_domain_id(task_domain), task.get("domain_selection", "task domain")
        selected, reason = self.resolve_domain(project, task["brief"])
        if selected is None:
            known = set(self._project_domains(project)) | set(self._known_domain_ids(project))
            if len(known) == 1:
                return next(iter(known)), "single available domain"
            raise HelmError(
                f"completed task {task['id']} has no unambiguous domain; pass --domain <domain-id>"
            )
        return selected, reason

    def create_learning_proposal(
        self,
        task_id: str,
        proposed_fact: str | None = None,
        *,
        fact: str | None = None,
        rationale: str | None = None,
        confidence: float | int | str | None = None,
        domain: str | None = None,
        artifact_ids: Sequence[str] | None = None,
        message_ids: Sequence[str] | None = None,
    ) -> dict[str, Any]:
        """Persist one evidence-backed learning proposal; never promote it."""
        if proposed_fact is not None and fact is not None:
            raise HelmError("provide either proposed_fact or fact, not both")
        proposed_fact = proposed_fact if proposed_fact is not None else fact
        fact = self._clean_learning_text(proposed_fact, "fact", 500)
        if self._learning_core_override(fact):
            raise SafetyError("learning cannot weaken or override Helm core safety rules")
        rationale_text = self._clean_learning_text(
            rationale or "Extracted from the completed task result and recorded evidence.",
            "rationale",
            2_000,
        )
        if self._learning_core_override(rationale_text):
            raise SafetyError("learning cannot weaken or override Helm core safety rules")
        confidence_value = self._learning_confidence(confidence)
        with self.store.locked() as data:
            task = self._task(data, task_id)
            project = self._project(data, task["project_id"])
            if task["status"] not in {"completed", "approved", "merged"}:
                raise SafetyError(
                    f"learning proposals require a successfully completed task, got {task['status']}"
                )
            self._require_terminal_worker(data, task, "learning proposal", require_completed=True)
            selected_domain, domain_reason = self._resolve_learning_domain_locked(
                data, task, project, domain
            )
            sources = self._learning_sources_locked(
                data, task, artifact_ids=artifact_ids, message_ids=message_ids
            )
            for proposal in data.get("learning_proposals", []):
                if (
                    proposal.get("domain_id") == selected_domain
                    and _learning_fact_key(proposal.get("proposed_fact", ""))
                    == _learning_fact_key(fact)
                ):
                    return proposal
            conflicts = self._learning_domain_conflicts_locked(
                data, project, selected_domain, fact
            )
            if any(conflict["type"] == "duplicate-domain-knowledge" for conflict in conflicts):
                raise HelmError(
                    f"learning duplicates existing knowledge in domain {selected_domain}; no proposal created"
                )
            proposal = {
                "id": new_id("lp"),
                "domain_id": selected_domain,
                "domain": selected_domain,
                "domain_selection": domain_reason,
                "project_id": project["id"],
                "proposed_fact": fact,
                "fact": fact,
                "rationale": rationale_text,
                "source_task_id": task["id"],
                "source_artifact_ids": [artifact["id"] for artifact in sources["artifacts"]],
                "source_message_ids": [message["id"] for message in sources["messages"]],
                "source_references": sources,
                "confidence": confidence_value,
                "created_at": now(),
                "status": "proposed",
                "conflicts": conflicts,
                "approval": None,
                "applied_at": None,
                "applied_path": None,
            }
            data["learning_proposals"].append(proposal)
            return proposal

    def generate_learning_proposals(
        self,
        task_id: str,
        *,
        domain: str | None = None,
        fact: str | None = None,
        rationale: str | None = None,
        confidence: float | int | str | None = None,
        artifact_ids: Sequence[str] | None = None,
        message_ids: Sequence[str] | None = None,
    ) -> list[dict[str, Any]]:
        """Extract candidate facts from results/artifacts, leaving review explicit."""
        data = self.store.load()
        task = self._task(data, task_id)
        if task["status"] not in {"completed", "approved", "merged"}:
            raise SafetyError(
                f"learning proposals require a successfully completed task, got {task['status']}"
            )
        messages = [message for message in data.get("messages", []) if message.get("task_id") == task_id]
        artifacts = [artifact for artifact in data.get("artifacts", []) if artifact.get("task_id") == task_id]
        candidates: list[str] = []
        if fact is not None:
            candidates = [fact]
        elif task.get("role") == "reviewer":
            # A reviewer's result is a verdict about one change, never a
            # fact about the domain. Forty-four of the first fifty-seven
            # proposals on one root were verdicts.
            raise HelmError(
                f"task {task_id} is a review; its verdict is not a learning. Provide --fact for a rule it revealed"
            )
        else:
            generic = {
                _learning_fact_key("worker completed; explicit approval is still required before merge"),
                _learning_fact_key("worker completed"),
            }
            candidates.extend(
                message["text"] for message in messages
                if message.get("kind") == "result"
                and _learning_fact_key(message.get("text", "")) not in generic
                and message.get("text", "").strip()
            )
            candidates.extend(
                artifact["description"] for artifact in artifacts
                if artifact.get("description", "").strip()
            )
            # Keep extraction bounded and deterministic. Review information is
            # retained in provenance/rationale rather than turned into a rule.
            candidates = [c for c in dict.fromkeys(candidates) if not _reads_as_report(c)][:10]
        if not candidates:
            raise HelmError(
                f"task {task_id} has no concise result or artifact description; provide --fact"
            )
        if rationale is None:
            review = "review outcome recorded" if task.get("approval") else "worker result recorded"
            rationale = f"Candidate extracted from the task's result: {review}."
        return [
            self.create_learning_proposal(
                task_id,
                candidate,
                rationale=rationale,
                confidence=confidence,
                domain=domain,
                artifact_ids=artifact_ids,
                message_ids=message_ids,
            )
            for candidate in candidates
        ]

    # Friendly API aliases for callers that use shorter proposal verbs.
    propose_learning = create_learning_proposal
    suggest_learning = generate_learning_proposals

    def list_learning_proposals(
        self,
        *,
        domain: str | None = None,
        status: str | None = None,
        task_id: str | None = None,
    ) -> list[dict[str, Any]]:
        if status is not None and status not in LEARNING_PROPOSAL_STATUSES:
            raise HelmError(f"unknown learning proposal status: {status}")
        data = self.store.load()
        proposals = [
            proposal for proposal in data.get("learning_proposals", [])
            if (domain is None or proposal.get("domain_id") == domain)
            and (status is None or proposal.get("status") == status)
            and (task_id is None or proposal.get("source_task_id") == task_id)
        ]
        return sorted(proposals, key=lambda proposal: proposal.get("created_at", ""), reverse=True)

    def inspect_learning_proposal(self, proposal_id: str) -> dict[str, Any]:
        data = self.store.load()
        for proposal in data.get("learning_proposals", []):
            if proposal.get("id") == proposal_id:
                return proposal
        raise HelmError(f"unknown learning proposal: {proposal_id}")

    get_learning_proposal = inspect_learning_proposal

    def edit_learning_proposal(
        self,
        proposal_id: str,
        *,
        proposed_fact: str | None = None,
        fact: str | None = None,
        rationale: str | None = None,
        confidence: float | int | str | None = None,
    ) -> dict[str, Any]:
        with self.store.locked() as data:
            proposal = next(
                (item for item in data.get("learning_proposals", []) if item.get("id") == proposal_id),
                None,
            )
            if proposal is None:
                raise HelmError(f"unknown learning proposal: {proposal_id}")
            if proposal.get("status") != "proposed":
                raise SafetyError("only proposed learning can be edited")
            if proposed_fact is not None and fact is not None:
                raise HelmError("provide either proposed_fact or fact, not both")
            proposed_fact = proposed_fact if proposed_fact is not None else fact
            fact = proposal["proposed_fact"] if proposed_fact is None else self._clean_learning_text(
                proposed_fact, "fact", 500
            )
            if self._learning_core_override(fact):
                raise SafetyError("learning cannot weaken or override Helm core safety rules")
            rationale_text = proposal["rationale"] if rationale is None else self._clean_learning_text(
                rationale, "rationale", 2_000
            )
            if self._learning_core_override(rationale_text):
                raise SafetyError("learning cannot weaken or override Helm core safety rules")
            confidence_value = proposal["confidence"] if confidence is None else self._learning_confidence(confidence)
            project = self._learning_project(data, proposal)
            proposal["proposed_fact"] = fact
            proposal["fact"] = fact
            proposal["rationale"] = rationale_text
            proposal["confidence"] = confidence_value
            proposal["conflicts"] = self._learning_domain_conflicts_locked(
                data, project, proposal["domain_id"], fact, exclude_proposal_id=proposal_id
            )
            proposal["edited_at"] = now()
            return proposal

    def approve_learning_proposal(
        self,
        proposal_id: str,
        note: str = "",
        *,
        actor: str = "user",
    ) -> dict[str, Any]:
        self.authority("approving a learning")
        with self.store.locked() as data:
            proposal = next(
                (item for item in data.get("learning_proposals", []) if item.get("id") == proposal_id),
                None,
            )
            if proposal is None:
                raise HelmError(f"unknown learning proposal: {proposal_id}")
            approved_by = self._learning_actor_allowed(proposal, actor, "learning approval")
            if proposal.get("status") != "proposed":
                raise SafetyError(f"learning proposal is already {proposal.get('status')}")
            project = self._learning_project(data, proposal)
            conflicts = self._learning_domain_conflicts_locked(
                data, project, proposal["domain_id"], proposal["proposed_fact"],
                exclude_proposal_id=proposal_id,
            )
            proposal["conflicts"] = conflicts
            if conflicts:
                raise SafetyError(
                    "learning proposal has conflicts; inspect and edit or reject it before approval"
                )
            proposal["status"] = "approved"
            proposal["approval"] = {
                "approved_at": now(),
                "approved_by": approved_by,
                "note": _safe_text(note),
            }
            return proposal

    def reject_learning_proposal(
        self,
        proposal_id: str,
        note: str = "",
        *,
        actor: str = "user",
    ) -> dict[str, Any]:
        self.authority("rejecting a learning")
        with self.store.locked() as data:
            proposal = next(
                (item for item in data.get("learning_proposals", []) if item.get("id") == proposal_id),
                None,
            )
            if proposal is None:
                raise HelmError(f"unknown learning proposal: {proposal_id}")
            rejected_by = self._learning_actor_allowed(proposal, actor, "learning rejection")
            if proposal.get("status") != "proposed":
                raise SafetyError(f"learning proposal is already {proposal.get('status')}")
            proposal["status"] = "rejected"
            proposal["rejection"] = {
                "rejected_at": now(),
                "rejected_by": rejected_by,
                "note": _safe_text(note),
            }
            return proposal

    @staticmethod
    def _learning_block(proposal: dict[str, Any]) -> str:
        """The block written into a knowledge file: the fact, and nothing real.

        A domain file is tracked product content, so the task, message and
        artifact ids that evidence a proposal stay on the proposal record in
        state, and the fact and rationale are scrubbed of any identifier that
        would name one root's work. Only the proposal id rides along, because
        it is what ties the block back to its provenance.
        """
        provenance = {
            "proposal_id": proposal["id"],
            "domain_id": proposal["domain_id"],
            "confidence": proposal.get("confidence"),
            "created_at": proposal["created_at"],
            "approved_at": proposal.get("approval", {}).get("approved_at"),
            "approved_by": proposal.get("approval", {}).get("approved_by"),
        }
        metadata = json.dumps(provenance, sort_keys=True, separators=(",", ":"))
        return (
            f"\n\n## Approved learning: {proposal['id']}\n"
            f"<!-- helm-learning: {metadata} -->\n"
            f"- Fact: {scrub_identifiers(proposal['proposed_fact'])}\n"
            f"- Rationale: {scrub_identifiers(proposal['rationale'])}\n"
            f"<!-- /helm-learning: {proposal['id']} -->\n"
        )

    def _append_learning_block(self, path: Path, block: str) -> None:
        if path.is_symlink():
            raise SafetyError(f"domain knowledge file must not be a symlink: {path}")
        parent = path.parent
        if parent.is_symlink():
            raise SafetyError(f"domain knowledge directory must not be a symlink: {parent}")
        existing = path.read_text(encoding="utf-8", errors="replace") if path.exists() else ""
        if existing and existing.endswith("\n"):
            block = block.lstrip("\n")
        elif existing:
            block = block.lstrip("\n")
        flags = os.O_WRONLY | os.O_CREAT | os.O_APPEND
        nofollow = getattr(os, "O_NOFOLLOW", 0)
        try:
            fd = os.open(path, flags | nofollow, 0o600)
        except FileExistsError as exc:
            raise SafetyError(f"domain knowledge file changed during apply: {path}") from exc
        try:
            with os.fdopen(fd, "a", encoding="utf-8") as stream:
                stream.write(block)
                stream.flush()
                os.fsync(stream.fileno())
            os.chmod(path, 0o600)
        except OSError:
            with contextlib.suppress(OSError):
                os.close(fd)
            raise

    def _learning_project(self, data: dict[str, Any], proposal: dict[str, Any]) -> dict[str, Any]:
        """The proposal's project, live or forgotten.

        A proposal outlives its project: the project can be removed once its
        work is over, and its learnings are exactly what should survive it.
        A forgotten project's record is read from the archive; failing that,
        a stand-in carries the id and the root's own directory, which is all
        a domain-scoped apply needs.
        """
        project_id = proposal.get("project_id")
        live = data.get("projects", {}).get(project_id or "")
        if live is not None:
            return live
        from .. import archive as _archive

        path = _archive.project_file(self.store.directory, project_id or "")
        with contextlib.suppress(OSError, ValueError):
            record = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(record, dict) and isinstance(record.get("project"), dict):
                return {**record["project"], "forgotten": True}
        configured = self.store.configured_root()
        return {
            "id": project_id or "forgotten",
            "root": str(configured or self.store.directory.parent),
            "forgotten": True,
        }

    def _learning_project_file(self, project: dict[str, Any], *, create: bool = False) -> Path:
        if project.get("forgotten"):
            raise HelmError(
                f"project {project.get('id')} has been removed; apply this learning to its domain, "
                "not to a project file nothing reads"
            )
        """The project's own knowledge file, the one nothing ever wrote.

        The composed context has always had a slot for per-project knowledge
        and the learning flow could only ever write to a domain, so the slot
        stayed empty forever and everything a project taught was either lost or
        forced into a domain where it did not belong.

        Project knowledge is additive and never narrows a task: a task still
        resolves its own domain, which may be a different one from the
        project's default, and it reads the project's file on top of that
        domain's chain rather than instead of it.
        """
        root = canonical(project["root"])
        settings = root / ".helm"
        if settings.is_symlink():
            raise SafetyError(f"project .helm directory must not be a symlink: {settings}")
        if create:
            settings.mkdir(parents=True, exist_ok=True)
        return settings / "knowledge.md"

    def apply_learning_proposal(
        self,
        proposal_id: str,
        *,
        actor: str = "user",
        scope: str = "domain",
    ) -> dict[str, Any]:
        self.authority("applying a learning")
        with self.store.locked() as data:
            proposal = next(
                (item for item in data.get("learning_proposals", []) if item.get("id") == proposal_id),
                None,
            )
            if proposal is None:
                raise HelmError(f"unknown learning proposal: {proposal_id}")
            applied_by = self._learning_actor_allowed(proposal, actor, "learning application")
            if proposal.get("status") == "applied":
                return proposal
            if proposal.get("status") != "approved":
                raise SafetyError("applying learning requires explicit proposal approval")
            if scope not in {"domain", "project"}:
                raise HelmError("learning scope must be 'domain' or 'project'")
            project = self._learning_project(data, proposal)
            if scope == "project":
                # Facts true of this project and no other belong here rather
                # than in a domain, where they would be taught to every
                # unrelated project that resolves the same domain.
                path = self._learning_project_file(project, create=True)
            else:
                conflicts = self._learning_domain_conflicts_locked(
                    data, project, proposal["domain_id"], proposal["proposed_fact"],
                    exclude_proposal_id=proposal_id,
                )
                proposal["conflicts"] = conflicts
                if conflicts:
                    raise SafetyError(
                        "approved learning conflicts with current knowledge or another proposal; re-review is required"
                    )
                path = self._learning_domain_file(project, proposal["domain_id"], create=True)
            existing = path.read_text(encoding="utf-8", errors="replace") if path.exists() else ""
            if proposal["id"] not in existing:
                self._append_learning_block(path, self._learning_block(proposal))
            proposal["status"] = "applied"
            proposal["applied_at"] = now()
            proposal["applied_by"] = applied_by
            proposal["applied_path"] = str(path)
            return proposal

    approve_learning = approve_learning_proposal
    reject_learning = reject_learning_proposal
    apply_learning = apply_learning_proposal
