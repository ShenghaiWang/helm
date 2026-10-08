"""What changed on an open pull request since the watch last looked.

Pure functions over what `gh` returns, so the comparison can be tested without
a forge. `PullRequestsMixin` reads the PR, keeps the snapshot this returns on
the task record, and queues one event for the task's lead when the comparison
finds something a lead has to know about.

The snapshot is a fingerprint, not a copy: each check's name and conclusion,
the review decision, the head commit, the open review threads with the id of
their latest comment, and the ids of the comments and reviews already seen.
Excerpts and links are read from the current payload when a change is
reported and never stored, so a busy PR cannot grow the state document.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

#: What the watch asks `gh pr view` for: the merge-state fields the sync
#: records, plus the checks, reviews and comments a lead has to act on.
PR_WATCH_FIELDS = (
    "url,state,reviewDecision,mergeStateStatus,mergeCommit,headRefOid,comments,body,"
    "statusCheckRollup,reviews,latestReviews,author"
)

#: The review threads, which `gh pr view` does not return. Read through
#: GraphQL because only it says whether a thread is resolved.
REVIEW_THREADS_QUERY = """
query($owner: String!, $name: String!, $number: Int!) {
  repository(owner: $owner, name: $name) {
    pullRequest(number: $number) {
      reviewThreads(first: 100) {
        nodes {
          id isResolved path line originalLine
          first: comments(first: 1) { nodes { id author { login } body } }
          last: comments(last: 1) { nodes { id author { login } body } }
        }
      }
    }
  }
}
"""

#: Check conclusions that mean something has to be looked at.
FAILED_CHECKS = frozenset({
    "FAILURE", "CANCELLED", "TIMED_OUT", "ACTION_REQUIRED", "ERROR", "STARTUP_FAILURE",
})
#: Check conclusions that count as passed.
GREEN_CHECKS = frozenset({"SUCCESS", "NEUTRAL", "SKIPPED"})
#: Kinds of change the lead has work to do about. The rest -- green, approved,
#: merged, closed -- are news it reports, not work.
ACTIONABLE_KINDS = frozenset({"failed", "thread", "comment", "changes-requested"})

_MAX_CHECKS = 80
_MAX_THREADS = 100
_MAX_SEEN = 500
_EXCERPT = 160


def excerpt(text: Any) -> str:
    """One line of someone's words, short enough to sit in a list."""
    flat = " ".join(str(text or "").split())
    return flat if len(flat) <= _EXCERPT else flat[: _EXCERPT - 3] + "..."


def login_of(node: Any) -> str:
    author = node.get("author") if isinstance(node, dict) else None
    return str(author.get("login") or "") if isinstance(author, dict) else ""


#: Accounts whose top-level comments are automation, not requests: `gh` names
#: an app account without its `[bot]` suffix, so the commonest is spelled out.
#: Reviews and review threads from any account still count.
_AUTOMATION_LOGINS = frozenset({"github-actions"})


def _is_bot(login: str) -> bool:
    """A deploy preview or a tracker link-back, not a person asking for a change."""
    return login.endswith("[bot]") or login.startswith("app/") or login in _AUTOMATION_LOGINS


def check_states(rollup: Any) -> dict[str, tuple[str, str]]:
    """Each check's name -> (its conclusion, or its status while it runs; its run URL)."""
    states: dict[str, tuple[str, str]] = {}
    for item in rollup if isinstance(rollup, list) else []:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or item.get("context") or "").strip()
        if not name:
            continue
        workflow = str(item.get("workflowName") or "").strip()
        if workflow and workflow != name:
            name = f"{workflow} / {name}"
        status = str(item.get("status") or "").upper()
        conclusion = str(item.get("conclusion") or item.get("state") or "").upper()
        if conclusion and status in {"", "COMPLETED"}:
            value = conclusion
        else:
            value = status or conclusion or "PENDING"
        states[name[:120]] = (value, str(item.get("detailsUrl") or item.get("targetUrl") or ""))
    return dict(sorted(states.items())[:_MAX_CHECKS])


def parse_review_threads(payload: Any) -> list[dict[str, Any]] | None:
    """The threads out of a GraphQL answer; None when it is not one."""
    try:
        nodes = payload["data"]["repository"]["pullRequest"]["reviewThreads"]["nodes"]
    except (KeyError, TypeError):
        return None
    if not isinstance(nodes, list):
        return None

    def comment(node: dict[str, Any], key: str) -> dict[str, Any]:
        found = ((node.get(key) or {}).get("nodes") or [{}])[0] or {}
        return {"id": found.get("id"), "author": login_of(found), "body": found.get("body")}

    return [
        {
            "id": node.get("id"),
            "resolved": bool(node.get("isResolved")),
            "path": node.get("path"),
            "line": node.get("line") or node.get("originalLine"),
            "first": comment(node, "first"),
            "last": comment(node, "last"),
        }
        for node in nodes
        if isinstance(node, dict)
    ]


def pr_watch_changes(
    previous: dict[str, Any] | None,
    payload: dict[str, Any],
    threads: list[dict[str, Any]] | None,
) -> tuple[dict[str, Any], list[str], set[str]]:
    """Compare what the forge says now with what the watch saw last.

    Returns the snapshot to store, the lines worth telling the lead, and the
    kinds of change among them. `previous` None is a PR the watch has not read
    yet: nothing was seen, so a red check or an open thread already there is
    news and a pending check is not. `threads` None means the threads could
    not be read this time; what was known about them is kept as it was.

    Only what someone other than the PR's author did is news: the lead's own
    replies and the author's own comments are the work, not a reason to wake.
    """
    prev = previous or {}
    author = login_of(payload)
    state = str(payload.get("state") or "OPEN").upper()
    head = str(payload.get("headRefOid") or "")
    decision = str(payload.get("reviewDecision") or "").upper()
    lines: list[str] = []
    kinds: set[str] = set()

    def other(login: str) -> bool:
        return bool(login) and login != author

    # A new push starts every check again; what the old head's checks said is
    # no baseline for the new head's.
    prev_checks = dict(prev.get("checks") or {}) if prev.get("head", head) == head else {}
    checks = check_states(payload.get("statusCheckRollup"))
    failed = [
        (name, value, link)
        for name, (value, link) in checks.items()
        if value in FAILED_CHECKS and prev_checks.get(name) != value
    ]
    for name, value, link in failed:
        lines.append(f"CI failed: {name} [{value}]" + (f" {link}" if link else ""))
    if failed:
        kinds.add("failed")
    all_green = bool(checks) and all(value in GREEN_CHECKS for value, _ in checks.values())
    was_green = bool(prev_checks) and all(value in GREEN_CHECKS for value in prev_checks.values())
    if all_green and not was_green:
        lines.append(f"CI green: all {len(checks)} check(s) passed")
        kinds.add("green")

    prev_threads = dict(prev.get("threads") or {})
    if threads is None:
        open_threads = prev_threads
    else:
        open_threads = {}
        for thread in threads:
            if thread.get("resolved") or not thread.get("id"):
                continue
            tid = str(thread["id"])
            first = thread.get("first") or {}
            last = thread.get("last") or first
            open_threads[tid] = str(last.get("id") or first.get("id") or "")
            where = str(thread.get("path") or "")
            if where and thread.get("line"):
                where = f"{where}:{thread['line']}"
            if tid not in prev_threads:
                if other(str(first.get("author") or "")):
                    lines.append(
                        f"New review thread by {first.get('author')} at {where or 'the PR'}: "
                        f"\"{excerpt(first.get('body'))}\""
                    )
                    kinds.add("thread")
            elif prev_threads[tid] != open_threads[tid] and other(str(last.get("author") or "")):
                lines.append(
                    f"New reply by {last.get('author')} on {where or 'a review thread'}: "
                    f"\"{excerpt(last.get('body'))}\""
                )
                kinds.add("thread")
        open_threads = dict(sorted(open_threads.items())[:_MAX_THREADS])

    seen = set(prev.get("seen") or [])
    current_ids: list[str] = []
    for comment in payload.get("comments") or []:
        if not isinstance(comment, dict):
            continue
        cid = str(comment.get("id") or comment.get("url") or "")
        if not cid:
            continue
        current_ids.append(cid)
        login = login_of(comment)
        if cid in seen or not other(login) or _is_bot(login):
            continue
        lines.append(f"New comment by {login}: \"{excerpt(comment.get('body'))}\"")
        kinds.add("comment")
    for review in payload.get("reviews") or []:
        if not isinstance(review, dict):
            continue
        rid = str(review.get("id") or "")
        if not rid:
            continue
        current_ids.append(rid)
        login = login_of(review)
        verdict = str(review.get("state") or "").upper()
        body = str(review.get("body") or "").strip()
        if rid in seen or not other(login):
            continue
        # A bare COMMENTED review is the envelope of its inline threads, which
        # are reported as threads.
        if not body and verdict not in {"CHANGES_REQUESTED", "APPROVED"}:
            continue
        lines.append(
            f"New review by {login} [{verdict or 'COMMENTED'}]"
            + (f": \"{excerpt(body)}\"" if body else "")
        )
        kinds.add("comment")

    prev_decision = str(prev.get("decision") or "")
    if decision != prev_decision and decision in {"CHANGES_REQUESTED", "APPROVED"}:
        lines.append(
            f"Review decision: {decision}" + (f" (was {prev_decision})" if prev_decision else "")
        )
        kinds.add("changes-requested" if decision == "CHANGES_REQUESTED" else "approved")

    if state == "MERGED" and prev.get("state") != "MERGED":
        lines.append("Merged.")
        kinds.add("merged")
    elif state == "CLOSED" and prev.get("state") != "CLOSED":
        lines.append("Closed without merging.")
        kinds.add("closed")

    snapshot = {
        "state": state,
        "decision": decision,
        "head": head,
        "checks": {name: value for name, (value, _link) in checks.items()},
        "threads": open_threads,
        "seen": current_ids[-_MAX_SEEN:],
    }
    return snapshot, lines, kinds


def pr_watch_fingerprint(snapshot: dict[str, Any]) -> str:
    """A short identity for a snapshot: equal fingerprints mean nothing moved."""
    return hashlib.sha256(json.dumps(snapshot, sort_keys=True).encode("utf-8")).hexdigest()[:16]


def watch_error_kind(reason: str) -> str:
    """`missing`, `unauthenticated`, or `unreachable` -- only the last mends itself."""
    lowered = reason.lower()
    if "not installed" in lowered:
        return "missing"
    if any(sign in lowered for sign in ("auth login", "not logged", "authentication", "http 401")):
        return "unauthenticated"
    return "unreachable"
