"""How Helm learns from the commander and from the work, and keeps what it learned.

The learning flow already promotes a fact from a finished task through a
proposal a human approves and applies. What it lacked was a way in for the
richest signal -- the commander's own rulings -- and a moment at which
anybody looks: 57 proposals sat unreviewed. Three additions:

- `teach`: the commander states a rule in their own words and it is applied
  at once, to a shared domain or to one project, with the commander as its
  provenance. Learning from the user, literally.
- `mine`: what recurs across tasks is a rule. Review findings that repeat
  and answers the coordinator gave twice become proposals with the evidence
  attached; a single result's prose does not.
- `triage`: the proposals that wait, one line each, decided in one command,
  and counted in `helm pending` once they have waited a week.
"""

from __future__ import annotations

import datetime as _dt
import re
import time
from collections import defaultdict
from typing import Any

from .. import archive
from ..errors import HelmError, SafetyError
from ..values import _safe_text, _validate_domain_id, new_id, now
from .learning import _learning_fact_key, _learning_polarity_and_core

#: Findings and answers must recur on this many distinct tasks before they
#: are proposed as a rule.
MINE_MIN_TASKS = 2
#: A proposal that has waited this long is counted where the commander looks.
TRIAGE_STALE_DAYS = 7
#: A review finding overlaps an applied fact's core words by this share to
#: count as "learning not followed".
FOLLOW_OVERLAP = 0.5
#: Two findings whose content-word sets overlap this much (Jaccard) are the
#: same point said twice.
SAME_POINT = 0.5

_STOP = {
    "a", "an", "and", "are", "as", "at", "be", "by", "do", "for", "from", "in", "is",
    "it", "of", "on", "or", "that", "the", "this", "to", "was", "with", "should",
    "must", "please", "also", "your", "you", "we", "our", "they", "their", "has",
    "have", "had", "not", "no", "never", "always", "use", "which", "when", "then",
}


def _epoch(stamp: Any) -> float:
    if not isinstance(stamp, str) or not stamp:
        return 0.0
    try:
        return _dt.datetime.fromisoformat(stamp.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return 0.0


def _core_words(text: str) -> list[str]:
    words = [w for w in re.findall(r"[a-z0-9]+", text.lower()) if w not in _STOP and len(w) > 2]
    return words


def _same_point(left: set[str], right: set[str]) -> bool:
    if not left or not right:
        return False
    return len(left & right) / len(left | right) >= SAME_POINT


def _first_sentence(text: str, limit: int = 300) -> str:
    cleaned = " ".join(_safe_text(text).split())
    cleaned = re.sub(r"^(CHANGES-REQUESTED|APPROVED)[:\s-]*", "", cleaned, flags=re.I).strip()
    match = re.match(r"(.+?[.!?])(\s|$)", cleaned)
    sentence = match.group(1) if match else cleaned
    return sentence[:limit].strip()


class KnowledgeMixin:
    # ---------- proposals without a source task ----------

    def _record_proposal_locked(
        self,
        data: dict[str, Any],
        *,
        fact: str,
        domain_id: str,
        project_id: str | None,
        rationale: str,
        confidence: float,
        message_ids: list[str],
        origin: str,
        source_task_id: str | None = None,
    ) -> dict[str, Any] | None:
        """A proposal from a source other than one finished task. None when a
        proposal with the same fact already exists in that domain."""
        key = _learning_fact_key(fact)
        for existing in data.get("learning_proposals", []):
            if existing.get("domain_id") == domain_id and _learning_fact_key(existing.get("proposed_fact", "")) == key:
                return None
        if self._learning_core_override(fact):
            raise SafetyError("learning cannot weaken or override Helm core safety rules")
        proposal = {
            "id": new_id("lp"),
            "domain_id": domain_id,
            "domain": domain_id,
            "domain_selection": origin,
            "project_id": project_id,
            "proposed_fact": fact,
            "fact": fact,
            "rationale": rationale,
            "source_task_id": source_task_id,
            "source_artifact_ids": [],
            "source_message_ids": list(message_ids),
            "source_references": {"messages": [{"id": m} for m in message_ids], "artifacts": []},
            "confidence": round(float(confidence), 2),
            "created_at": now(),
            "status": "proposed",
            "conflicts": [],
            "approval": None,
            "applied_at": None,
            "applied_path": None,
            "origin": origin,
        }
        data.setdefault("learning_proposals", []).append(proposal)
        return proposal

    # ---------- teach: learning from the commander ----------

    def teach(
        self,
        fact: str,
        *,
        domain: str | None = None,
        project_id: str | None = None,
        note: str = "",
        actor: str = "user",
    ) -> dict[str, Any]:
        """Record a rule in the commander's own words and apply it at once.

        A domain fact reaches every project that resolves the domain; a
        project fact reaches that project alone. The proposal is kept, marked
        as the commander's, so the knowledge file's provenance block says who
        said it and when -- the same shape a task-born learning has.
        """
        self.authority("teaching a learning")
        fact = self._clean_learning_text(fact, "fact", 500)
        if not domain and not project_id:
            raise HelmError("say where it applies: --domain <id> (every project of that domain) or --project <id>")
        if domain and project_id:
            raise HelmError("teach one scope at a time: --domain or --project")
        rationale = _safe_text(note).strip() or "Stated by the commander."
        with self.store.locked() as data:
            if project_id:
                project = self._project(data, project_id)
                domain_id = (project.get("domains") or ["project"])[0]
                scope = "project"
            else:
                domain_id = _validate_domain_id(domain)
                project = next(
                    (p for p in data.get("projects", {}).values() if domain_id in (p.get("domains") or [])),
                    None,
                ) or next(iter(data.get("projects", {}).values()), None)
                if project is None:
                    raise HelmError("teaching a domain needs at least one registered project to resolve the domain root from")
                scope = "domain"
            proposal = self._record_proposal_locked(
                data, fact=fact, domain_id=domain_id, project_id=project["id"],
                rationale=rationale, confidence=1.0, message_ids=[], origin="commander",
            )
            if proposal is None:
                raise HelmError("that fact is already proposed or applied in this scope")
            proposal["status"] = "approved"
            proposal["approval"] = {"approved_at": now(), "approved_by": actor, "note": rationale}
            proposal_id = proposal["id"]
        return self.apply_learning_proposal(proposal_id, actor=actor, scope=scope)

    # ---------- mine: learning from what recurs ----------

    def mine_learnings(self, *, days: float = 14.0, dry_run: bool = False) -> dict[str, Any]:
        """Propose rules from review findings and answers that recur across tasks."""
        cutoff = time.time() - days * 86400
        data = self.store.load()
        tasks: dict[str, dict[str, Any]] = dict(data.get("tasks", {}))
        messages: list[dict[str, Any]] = list(data.get("messages", []))
        for task_id in archive.archived_task_ids(self.store.directory, modified_since=cutoff):
            record = archive.read_task(self.store.directory, task_id)
            if record is None:
                continue
            tasks.setdefault(task_id, record["task"])
            messages.extend(record.get("messages", []))
            for reviewer_id in record.get("reviewer_task_ids", []):
                reviewer = archive.read_task(self.store.directory, reviewer_id)
                if reviewer is not None:
                    tasks.setdefault(reviewer_id, reviewer["task"])
                    messages.extend(reviewer.get("messages", []))
        clusters: list[dict[str, Any]] = []
        for message in messages:
            if _epoch(message.get("created_at")) < cutoff:
                continue
            task = tasks.get(message.get("task_id") or "")
            if task is None:
                continue
            kind = message.get("kind")
            text = str(message.get("text") or "")
            if kind == "result" and task.get("role") == "reviewer" and text.lstrip().upper().startswith("CHANGES-REQUESTED"):
                subject = tasks.get(task.get("reviews") or "") or task
                source = "review finding"
            elif kind == "answer" and (message.get("payload") or {}).get("source") not in {"cleanup", "project-remove"}:
                subject = task
                source = "answer"
            else:
                continue
            sentence = _first_sentence(text)
            words = set(_core_words(sentence))
            if len(words) < 3:
                continue
            domain_id = subject.get("domain") or (data.get("projects", {}).get(subject.get("project_id") or "", {}).get("domains") or [None])[0]
            if not domain_id:
                continue
            hit = {
                "message_id": message.get("id"), "task_id": subject.get("id"),
                "project_id": subject.get("project_id"), "domain_id": domain_id,
                "text": sentence, "at": message.get("created_at"),
            }
            for cluster in clusters:
                if cluster["source"] == source and _same_point(cluster["words"], words):
                    cluster["hits"].append(hit)
                    cluster["words"] |= words
                    break
            else:
                clusters.append({"source": source, "words": set(words), "hits": [hit]})
        proposed: list[dict[str, Any]] = []
        considered = 0
        with self.store.locked() as live:
            for cluster in clusters:
                source, hits = cluster["source"], cluster["hits"]
                distinct_tasks = {h["task_id"] for h in hits}
                if len(distinct_tasks) < MINE_MIN_TASKS:
                    continue
                considered += 1
                domains = {h["domain_id"] for h in hits}
                domain_id = hits[-1]["domain_id"] if len(domains) == 1 else None
                if domain_id is None:
                    continue
                latest = max(hits, key=lambda h: h.get("at") or "")
                rationale = (
                    f"The same {source} recurred on {len(distinct_tasks)} tasks "
                    f"({', '.join(sorted(distinct_tasks)[:5])}); a point made that often is a rule."
                )
                confidence = min(0.9, 0.5 + 0.1 * len(distinct_tasks))
                if dry_run:
                    proposed.append({"fact": latest["text"], "domain_id": domain_id, "tasks": sorted(distinct_tasks), "source": source})
                    continue
                proposal = self._record_proposal_locked(
                    live, fact=latest["text"], domain_id=domain_id, project_id=latest["project_id"],
                    rationale=rationale, confidence=confidence,
                    message_ids=[h["message_id"] for h in hits if h.get("message_id")],
                    origin=f"mined: {source}", source_task_id=latest["task_id"],
                )
                if proposal is not None:
                    proposed.append(proposal)
        return {"days": days, "clusters": considered, "proposed": proposed, "dry_run": dry_run}

    # ---------- triage ----------

    def waiting_learnings(self, *, task_id: str | None = None, project_id: str | None = None) -> list[dict[str, Any]]:
        data = self.store.load()
        found = []
        for proposal in data.get("learning_proposals", []):
            if proposal.get("status") != "proposed":
                continue
            if task_id and proposal.get("source_task_id") != task_id:
                continue
            if project_id and proposal.get("project_id") != project_id:
                continue
            found.append(dict(proposal))
        return sorted(found, key=lambda p: p.get("created_at") or "")

    def stale_learnings(self, *, days: float = TRIAGE_STALE_DAYS) -> list[dict[str, Any]]:
        cutoff = time.time() - days * 86400
        return [p for p in self.waiting_learnings() if _epoch(p.get("created_at")) <= cutoff]

    def triage_learnings(
        self,
        *,
        approve: list[str] | None = None,
        reject: list[str] | None = None,
        scope: str = "domain",
        note: str = "",
        actor: str = "user",
    ) -> dict[str, Any]:
        """Decide several proposals at once: approved ones are applied on the spot."""
        applied: list[dict[str, Any]] = []
        rejected: list[dict[str, Any]] = []
        failed: list[dict[str, str]] = []
        for proposal_id in approve or []:
            try:
                self.approve_learning_proposal(proposal_id, note, actor=actor)
                applied.append(self.apply_learning_proposal(proposal_id, actor=actor, scope=scope))
            except (HelmError, SafetyError) as exc:
                failed.append({"proposal_id": proposal_id, "reason": str(exc)[:200]})
        for proposal_id in reject or []:
            try:
                rejected.append(self.reject_learning_proposal(proposal_id, note, actor=actor))
            except (HelmError, SafetyError) as exc:
                failed.append({"proposal_id": proposal_id, "reason": str(exc)[:200]})
        return {"applied": applied, "rejected": rejected, "failed": failed}

    # ---------- measurement ----------

    def applied_learnings(self, domain_id: str | None = None) -> list[dict[str, Any]]:
        data = self.store.load()
        return [
            p for p in data.get("learning_proposals", [])
            if p.get("status") == "applied" and (domain_id is None or p.get("domain_id") == domain_id)
        ]

    def learning_not_followed(self, finding: str, domain_id: str | None) -> list[str]:
        """Applied facts a review finding restates: knowledge the brief carried and the work ignored."""
        _polarity, words = _learning_polarity_and_core(finding)
        if not words:
            return []
        hits = []
        for proposal in self.applied_learnings(domain_id):
            _p, core = _learning_polarity_and_core(proposal.get("proposed_fact", ""))
            if not core:
                continue
            overlap = len(core & words) / len(core)
            if overlap >= FOLLOW_OVERLAP:
                hits.append(proposal["id"])
        return hits

    def knowledge_stats(self) -> dict[str, Any]:
        data = self.store.load()
        proposals = data.get("learning_proposals", [])
        by_status: dict[str, int] = defaultdict(int)
        for p in proposals:
            by_status[str(p.get("status"))] += 1
        origins: dict[str, int] = defaultdict(int)
        for p in proposals:
            origins[str(p.get("origin") or "task")] += 1
        return {
            "proposals": len(proposals),
            "by_status": dict(by_status),
            "by_origin": dict(origins),
            "stale": len(self.stale_learnings()),
        }
