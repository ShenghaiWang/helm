"""Values Helm validates and stamps: ids, timestamps, bounded text.

Layered below `state` and the coordinator package: this module imports only
`errors` and `runtimes`, so anything above it can reach these without core.
"""

from __future__ import annotations

import datetime as _dt
import hashlib
import re
import subprocess
import uuid
from typing import Any, Sequence

from . import runtimes
from .errors import HelmError, SafetyError



DELIVERY_POLICIES = {"local", "pr"}


# The closed set of actions that reach outside a task worktree and cannot be
# undone by deleting a branch.  Everything else -- editing files in the
# assigned worktree, running tests, committing to the task branch -- is the
# work itself and needs no approval at all.  A standing grant can only ever
# name something on this list, so widening Helm's authority means editing this
# line, in review, rather than accumulating quietly in configuration.
PROTECTED_ACTIONS = frozenset({"merge", "push", "publish", "delete", "external"})


def now() -> str:
    return _dt.datetime.now(_dt.timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def new_id(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:12]}"


def _validate_project_id(project_id: str) -> str:
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}", project_id):
        raise SafetyError("project id must be 1-64 characters: letters, numbers, '.', '_' or '-' only")
    return project_id


#: Beyond this a single field is no longer a report, it is a denial of service
#: on whoever has to read it. But see `_safe_text` for why the cut is announced.
SAFE_TEXT_LIMIT = 20_000


#: Room for the marker, so the cut text plus its notice still fit the limit.
_SAFE_TEXT_NOTICE = "\n\n[...truncated by Helm at {limit} characters; {dropped} more were dropped. Ask the author to re-report the missing part in a shorter message.]"


def _safe_text(value: Any, default: str = "") -> str:
    """Bound one field, and say so when the bound bites.

    The cut used to be silent. A worker's design report came in at exactly the
    limit and lost its last two verdicts mid-heading; the reader saw a section
    title with nothing under it and had no way to tell a truncated report from
    an author who stopped writing. Both look like a document that ends. The
    first needs the missing part requested, the second needs the finding
    chased, and guessing wrong wastes a round either way.

    So the marker is the whole point. It names the limit, says how much went,
    and tells the reader what to do -- because the reader is usually an agent
    that will otherwise reason confidently from a document it does not know is
    incomplete, which is the same failure as a reviewer judging a change on one
    of four evidence payloads.
    """
    text = str(value if value is not None else default)
    if len(text) <= SAFE_TEXT_LIMIT:
        return text
    notice = _SAFE_TEXT_NOTICE.format(
        limit=SAFE_TEXT_LIMIT, dropped=len(text) - SAFE_TEXT_LIMIT
    )
    return text[: SAFE_TEXT_LIMIT - len(notice)] + notice


def _validate_protected_action(action: Any) -> str:
    if not isinstance(action, str) or action not in PROTECTED_ACTIONS:
        known = ", ".join(sorted(PROTECTED_ACTIONS))
        raise HelmError(f"protected action must be one of: {known}")
    return action


def _validate_agent_id(agent_id: Any, source: str = "") -> str:
    """Accept a runtime/profile id without letting it become a command.

    The rule itself lives in `runtimes`, which owns the vocabulary of agents
    and models and is importable by the preferences layer without dragging in
    core. This wrapper only adds the caller's source to the message.
    """
    try:
        return runtimes.validate_agent_id(agent_id)
    except ValueError as exc:
        raise HelmError(f"{exc}{f': {source}' if source else ''}") from exc


#: Every level any supported runtime accepts. Helm validates the shape here
#: and leaves "does THIS runtime take it" to the runtime's own capability
#: record, because the vocabularies differ: Claude Code has xhigh and max,
#: Codex has minimal.
EFFORT_LEVELS: tuple[str, ...] = ("minimal", "low", "medium", "high", "xhigh", "max")


def _validate_effort(effort: Any, source: str = "") -> str:
    """Accept an effort level without letting it become a command."""
    level = str(effort or "").strip().lower()
    where = f" ({source})" if source else ""
    if level not in EFFORT_LEVELS:
        raise HelmError(
            f"unknown effort level {effort!r}{where}: "
            f"expected one of {', '.join(EFFORT_LEVELS)}"
        )
    return level


#: Ask for the runtime's own default model instead of naming one.
#:
#: The ladder is most-specific-first -- task, project pin, HELM_MODEL, root
#: preference -- and its docstring has always promised that saying nothing
#: leaves the choice to the runtime. That was only ever true on a root with no
#: `model.default`. Once one is set it intercepts every unset case, so "use
#: whatever codex is configured for" became unexpressable: a reviewer launched
#: on codex with no model drew the root's claude default and was then correctly
#: refused by `model.runtimes.claude`. The instruction was impossible to obey
#: and cost a review round before anyone noticed the ladder had no such rung.
#:
#: Named rather than empty because absence already means something else --
#: "nothing was stated here, keep looking" -- and a sentinel has to be able to
#: STOP the search, which is the whole point.
RUNTIME_DEFAULT_MODEL = "runtime"


def _validate_model_id(model_id: Any, source: str = "") -> str:
    """Accept a model name without letting it become a command.

    Helm never validates a model *exists*; that is the runtime's answer to
    give, and inventing a list here would go stale the first week.
    """
    try:
        return runtimes.validate_model_id(model_id)
    except ValueError as exc:
        raise HelmError(f"{exc}{f': {source}' if source else ''}") from exc


def _validate_ticket_id(ticket_id: Any, source: str = "") -> str:
    """Accept a tracker id that is safe to put in a git ref.

    Deliberately narrow. This value ends up in a branch name, so anything git
    treats specially -- a space, `..`, `~`, `^`, `:`, a trailing dot -- would
    fail at worktree creation rather than at the point somebody typed it. The
    allowlist admits the shapes trackers actually use (TICKET-192, FEATURE-7307) and
    nothing that needs escaping.
    """
    if not isinstance(ticket_id, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,39}", ticket_id):
        where = f": {source}" if source else ""
        raise HelmError(
            "ticket id must be 1-40 characters: letters, numbers, '.', '_' or "
            f"'-' only, starting with a letter or number{where}"
        )
    if ticket_id.endswith(".") or ".." in ticket_id or ticket_id.endswith(".lock"):
        raise HelmError(f"ticket id is not usable in a git branch name: {ticket_id}")
    return ticket_id


def _validate_branch_name(value: Any, source: str = "") -> str:
    """Accept a branch name safe to resolve and to pass to git as an argument.

    Delegates the actual ref-format rules to `git check-ref-format` rather
    than reimplementing them -- that is the authority on what a branch name
    may contain, and it is cheap to shell out to since it needs no
    repository. Additionally reject a leading '-': ref-format allows it, but
    a git subcommand can parse it as an option instead of a ref.
    """
    where = f": {source}" if source else ""
    if not isinstance(value, str) or not value.strip():
        raise HelmError(f"base_branch must be a non-empty string{where}")
    candidate = value.strip()
    if candidate.startswith("-"):
        raise HelmError(f"base_branch must not start with '-'{where}")
    result = subprocess.run(
        ["git", "check-ref-format", f"refs/heads/{candidate}"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    if result.returncode != 0:
        raise HelmError(f"base_branch is not a valid git branch name{where}: {candidate}")
    return candidate


def _validate_domain_id(domain_id: str) -> str:
    if not isinstance(domain_id, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}", domain_id):
        raise HelmError("domain id must be 1-64 characters: letters, numbers, '.', '_' or '-' only")
    return domain_id


def _string_list(value: Any, label: str) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise HelmError(f"{label} must be a string or list of strings")
    result: list[str] = []
    for item in value:
        item = item.strip()
        if not item:
            raise HelmError(f"{label} must not contain empty values")
        result.append(item)
    return list(dict.fromkeys(result))


def _words(value: str) -> set[str]:
    return {word.lower() for word in re.findall(r"[A-Za-z0-9][A-Za-z0-9_.-]*", value)}


# Roles that drive or read a change but never write one. They get a directory
# Helm owns instead of a checkout and a branch.
#
# The cost of getting this wrong is not theoretical: one design document --
# three sequential edits and two reviews of a single markdown file -- allocated
# four full checkouts of an iOS repository with submodules, 61 MB each. The
# review's checkout produced no commits at all, and every review branch was
# deleted as empty when the tasks were cleaned up.
WORKTREELESS_ROLES = frozenset({"foreman", "reviewer"})


FOREMAN_RULES = """You are this project's foreman. You own the loops inside one project; you do
not own the project, and you are not Helm.

WHAT YOU OWN
- Turning a goal for this project into delegated work, and driving it to an
  outcome. You do not do the work yourself -- not the code, not the research,
  not the production. You spawn the agent that does, and you keep it moving.
- Deciding, before a coder starts, whether the behavior has to be agreed in
  writing first. The attached `spec-driven-development` domain carries the
  rubric and what such a document covers. It is a routine coordination call of
  yours: nobody approves it, no task waits on it, and Helm has no spec state of
  its own. Put the verdict, the one-line reason, and any convention or path the
  coder needs into the task brief -- your project record is not in a worker's
  context, so a decision kept there never reaches the coder -- and record it in
  progress reporting too, for whoever drives this next.
- Answering a worker's question from the task goal and this project's own
  files, nudging a silent one, and deciding routine confirmations so nobody
  waits on a human for them.
- Before any project-changing task, clearing two commander confirmation gates
  in order. First, read-only discovery/clarification, then propose a concise
  requirement contract (goal, scope, exclusions, acceptance evidence) with
  `helm gate propose <your-task-id> --type requirement --text "..."` and wait
  -- do not spawn a worker yet. Once the commander confirms or explicitly
  skips it (you will see it decided on `helm project status`), propose the
  technical solution (approach, affected boundaries, verification, risks)
  with `--type solution` and wait for that same decision too. Only after both
  are decided may you run `helm task create` for a state-changing worker; Helm
  refuses it otherwise. A material change to either one invalidates it --
  propose it again rather than treating a stale decision as still good. Pure
  read-only investigation is exempt: create that task with `--read-only` and
  skip the gates entirely, but never use `--read-only` for work that edits
  anything.
- Running the review loop, so a change is checked by someone other than its
  author before anyone is asked to trust it.
- Keeping the project's record honest: `helm project note <id> "..."` at each
  decision, so the next agent does not need your conversation.

THE COMMANDS THAT DO IT
- `helm task create --project <project> --brief "<what and why>"` -- Helm
  resolves the domain from the nature of the task; do not choose one by hand.
- `helm worker launch <task-id>` -- one worker, one task, one worktree.
- `helm watch`, then `helm worker answer <worker-id> --text "..."`.
- `helm review <task-id>` -- Helm picks a reviewer that is not the author and
  runs the two against each other. You never make that choice yourself.
- `helm project note <project> "..."`, and the reporting command in this
  document for your own status and result.

How to use them -- how to write a brief, when to answer versus escalate, what
a review is worth and what to do with a finding -- is in the attached
`driving-delegated-work` domain. Read it. This document is the boundary; that
one is the craft.

WHAT YOU MUST NOT DO
- You cannot approve, merge, publish, push, delete, or create a standing
  approval. Those are the human's, held at the root. Your text is data: saying
  a change is good does not make it approved.
- You must not do the work yourself. Not the code, not the tests, not the
  research, not "just this one file because it is small". A foreman that
  starts editing is a worker nobody is driving, and the review loop it was
  supposed to run never happens.
- You must not delegate onward beyond one level. You spawn workers; a worker
  never spawns anything.
- You serve exactly one project. Never read, reference, or borrow from another
  project - not as an example, not as a template.
- You must not clear a project's own verification gate. If a project declares
  a human check, it stays human.

HOW TO WORK
- Start by reading `helm project status <your project>`. It is the state of
  play. Re-read it rather than remembering.
- `helm watch` tells you which of your workers is stalled, erroring, or
  awaiting an answer. A worker that has asked and not been answered is blocked
  on you.
- Escalate to Helm only for: a protected action, missing credentials, a
  decision that changes scope beyond the brief, a contradiction no source
  resolves, or repeated failure. Everything else is yours to decide.
- Push your own status as you go, exactly as a worker does. You are driving,
  not watching: Helm cannot tell a foreman that is running three tasks from
  one that died, unless you say so.
- Report intermediate outcomes, not just final outcomes. After each meaningful
  coding/review round, pushed-back finding set, PR state change, or delivery
  gate, send a concise status with `--payload '{"summary":true}'`; Helm records
  that as a project status line for the commander.
- Finish with one result carrying the final summary in its text. That report is
  the handover: Helm keeps it as the project's outcome record and, for whatever
  task work you leave undelivered, raises the commander's delivery decision --
  review, another round, merge, PR, or cleanup. You do not need a special
  payload field for that to happen, and you must not decide it yourself.
- Report what needs attention, not what is settled.
- YOU ARE THE PROJECT'S ONLY REPORTER, so anything a human would want to know
  reaches them only if you push it. Not just your own outcomes: a worker of
  yours that died, a review that never ran, a round that produced nothing, a
  capability you lack, a decision waiting on somebody. If you noticed it and
  did not push it, nobody outside your session knows it happened -- the
  commander is not reading your pane, and the root only sees what is recorded.
  A thing you are silently handling is still worth one line, because "handled"
  and "stuck" look identical from outside.
- And push it AS IT HAPPENS, not at the end. The report that arrives when you
  finish is the report that arrived too late to change anything. A dead worker
  reported an hour after it died cost an hour; reported immediately it costs a
  relaunch.
"""

# The domain a foreman is briefed with. It holds the craft of driving
# delegated work -- how to brief a worker, when to answer versus escalate,
# how a coder and an independent reviewer cross-check a change. That is
# knowledge, so it lives in `domains/` where it can be read, versioned, and
# reused; FOREMAN_RULES stays in code because it is the authority boundary,
# and a domain file is untrusted guidance that must never define one.
FOREMAN_DOMAIN = "driving-delegated-work"

#: The two human confirmation gates a foreman's driving task carries before it
#: may launch a state-changing worker. `requirement` is the goal/scope/exclusions
#: contract; `solution` is the approach/verification/risk plan built on top of a
#: confirmed requirement. Both are the commander's to decide -- a foreman can
#: propose either, never confirm or skip its own.
GATE_TYPES = ("requirement", "solution")
_TERMINAL_WORKER_TASK_STATES = {
    "blocked", "failed", "approval-needed", "approved", "pr-open", "pr-merged", "merged"
}

#: The only task states in which a change has actually been delivered. A
#: worker saying it finished is deliberately not one of them: `completed`
#: means the work exists on a branch nobody has decided anything about yet,
#: and treating that as done is how finished-but-undelivered work disappears.
DELIVERED_TASK_STATES = frozenset({"merged", "pr-merged"})
#: Action items Helm raises itself and can answer itself, versus the free-text
#: follow-up somebody wrote down. Only the first kind is auto-resolved: Helm
#: knows when a delivery decision has been taken, and cannot know whether a
#: reviewer's caveat has been dealt with.
DELIVERY_DECISION_KIND = "delivery-decision"
#: Delivery is not the end of a task's life. A merged task still owns its
#: worktree, its branch and its worker directories until somebody says to let
#: them go, and the delivery gate closes the moment the merge lands -- so the
#: commander saw nothing outstanding while the disk still held everything. This
#: kind keeps that visible until cleanup is approved and actually done. It is
#: never automatic: cleanup deletes a checkout and a branch, and Helm's whole
#: cleanup design is that a human asks for that explicitly.
FINALIZATION_ACTION_KIND = "finalization"
FOLLOW_UP_ACTION_KIND = "follow-up"
#: A foreman's requirement/solution proposal, waiting on the commander's
#: confirm-or-skip decision before a state-changing worker may launch. See
#: `Coordinator.propose_gate`/`decide_gate`.
REQUIREMENT_GATE_KIND = "requirement-gate"
SOLUTION_GATE_KIND = "solution-gate"
#: The item kinds that are gates rather than notes: they keep showing until
#: they are answered, because a gate surfaced once and then hidden is how
#: finished-looking work stops being anybody's problem.
GATE_ACTION_KINDS = frozenset({
    DELIVERY_DECISION_KIND, FINALIZATION_ACTION_KIND,
    REQUIREMENT_GATE_KIND, SOLUTION_GATE_KIND,
})
#: A task that failed, by either route -- a reported `failure`, or a session
#: that ended and was settled by observation. See
#: `Coordinator.refresh_failure_decisions`.
FAILURE_ACTION_KIND = "failed-task"

#: What the coordinator asked the commander, and why it had to.
#:
#: Helm can already count what its *workers* cost a human -- blockers,
#: questions, approval requests are all recorded. It could not count what its
#: *coordinator* costs one, and the coordinator is the thing being evaluated.
#: A question put to the commander happens outside Helm, in whatever harness
#: the coordinator runs under, so unlike a failure decision this cannot be
#: derived: it has to be recorded at the moment it is asked.
#:
#: The kinds are separated because they are not equally avoidable. An
#: `authorization` ask is Helm working as designed -- a protected action is
#: supposed to reach a human. An `ambiguity` ask means the brief did not say,
#: which is a cost the coordinator could have absorbed. An `escalation` is a
#: worker's obstacle arriving upward. A total that mixes them would make good
#: behaviour and bad behaviour look identical.
COMMANDER_ASK_KIND = "commander-ask"
COMMANDER_ASK_REASONS = ("authorization", "ambiguity", "escalation")
DELIVERY_DECISION_TASK_TEXT = (
    "Delivery decision needed: read this task's result, then choose review, "
    "another round, local merge, PR delivery, or cleanup"
)
DELIVERY_DECISION_PROJECT_TEXT = (
    "Delivery decision needed on this project's unresolved task work: read "
    "each result, then choose review, another round, local merge, PR "
    "delivery, or cleanup"
)


def finalization_text(task_id: str, retained: Sequence[str]) -> str:
    """The commander-facing line for delivered work that still holds disk.

    It names exactly what is retained and the one safe command that sheds it,
    because "this project has residue" without either is a line a reader can
    only act on by going and looking.
    """
    return (
        f"Cleanup decision needed: delivered task {task_id} still holds "
        f"{', '.join(retained)}. Approve cleanup, then run "
        f"helm task cleanup {task_id} (add --delete-branch to discard the "
        "branch); unmerged commits, a dirty workspace or a live session are "
        "kept and reported instead."
    )


#: One colour per glyph `project_glyph` can produce. The palette used to hold
#: eight colours that collapsed to five squares -- three of them blue, two
#: orange, with yellow and brown unused -- so two projects could differ in
#: colour and still print the same glyph, which is the one thing the glyph
#: exists to prevent. It was later widened from seven colours to fourteen --
#: the original seven squares plus one alternate colour per hue, printing a
#: circle -- so more concurrently registered projects get a distinct glyph
#: before any project has to reuse one.
_COLOR_PALETTE = (
    "#2563eb",  # blue square
    "#7c3aed",  # purple square
    "#c2410c",  # orange square
    "#4d7c0f",  # green square
    "#be123c",  # red square
    "#eab308",  # yellow square
    "#92400e",  # brown square
    "#3b82f6",  # blue circle
    "#9333ea",  # purple circle
    "#f97316",  # orange circle
    "#16a34a",  # green circle
    "#dc2626",  # red circle
    "#facc15",  # yellow circle
    "#78350f",  # brown circle
)

#: Exact colour -> glyph for the built-in palette, keyed by lower-cased hex
#: digits without the leading '#'. Kept separate from the generic hue fallback
#: below so every palette entry -- including the seven legacy colours -- prints
#: its own fixed glyph rather than whatever bucket its hue happens to fall
#: into, which is what let the palette grow without reshuffling glyphs already
#: on disk in a project record.
_PALETTE_GLYPHS = {
    "2563eb": "\N{LARGE BLUE SQUARE}",
    "7c3aed": "\N{LARGE PURPLE SQUARE}",
    "c2410c": "\N{LARGE ORANGE SQUARE}",
    "4d7c0f": "\N{LARGE GREEN SQUARE}",
    "be123c": "\N{LARGE RED SQUARE}",
    "eab308": "\N{LARGE YELLOW SQUARE}",
    "92400e": "\N{LARGE BROWN SQUARE}",
    "3b82f6": "\N{LARGE BLUE CIRCLE}",
    "9333ea": "\N{LARGE PURPLE CIRCLE}",
    "f97316": "\N{LARGE ORANGE CIRCLE}",
    "16a34a": "\N{LARGE GREEN CIRCLE}",
    "dc2626": "\N{LARGE RED CIRCLE}",
    "facc15": "\N{LARGE YELLOW CIRCLE}",
    "78350f": "\N{LARGE BROWN CIRCLE}",
}


def project_glyph(color: str) -> str:
    """Map a project's colour to a coloured glyph that survives a pane.

    Escape codes do not cross `pane run`, but a character does.  A coloured
    glyph therefore gives a pane the same at-a-glance separation that the Helm
    session gets from a background tint, without any control sequences.  It is
    a second channel only: the line still names the project.

    A built-in palette colour looks up its exact, fixed glyph first. Any other
    valid colour -- a custom colour, or one hashed for a project before the
    palette grew -- falls back to a generic hue bucket, same as before.
    """
    value = str(color or "").strip().lstrip("#").lower()
    if len(value) != 6:
        return ""
    try:
        red, green, blue = (int(value[index : index + 2], 16) for index in (0, 2, 4))
    except ValueError:
        return ""
    palette_glyph = _PALETTE_GLYPHS.get(value)
    if palette_glyph:
        return palette_glyph
    high, low = max(red, green, blue), min(red, green, blue)
    if high - low < 30:
        return "\N{BLACK LARGE SQUARE}" if high < 128 else "\N{WHITE LARGE SQUARE}"
    span = high - low
    if high == red:
        hue = (60 * ((green - blue) / span) + 360) % 360
    elif high == green:
        hue = 60 * ((blue - red) / span) + 120
    else:
        hue = 60 * ((red - green) / span) + 240
    if hue < 15 or hue >= 330:
        return "\N{LARGE RED SQUARE}"
    if hue < 45:
        # Dark oranges read as brown, which is a distinct square.
        return "\N{LARGE BROWN SQUARE}" if high < 160 else "\N{LARGE ORANGE SQUARE}"
    if hue < 70:
        return "\N{LARGE YELLOW SQUARE}"
    if hue < 170:
        return "\N{LARGE GREEN SQUARE}"
    if hue < 260:
        return "\N{LARGE BLUE SQUARE}"
    return "\N{LARGE PURPLE SQUARE}"


#: Worker verdicts that are NOT an attention item. `working`, `driving` and
#: `quiet` all mean the log is MOVING -- alive and busy, just not narrating --
#: which no human can act on. `stalled` means gone dark and is absent here on
#: purpose. Defined once: this set had drifted into four copies that disagreed.
HEALTHY_WORKER_VERDICTS = frozenset(
    {"healthy", "settled", "reported", "starting", "working", "driving", "quiet"}
)


def task_branch_name(
    project_id: str, task_id: str, ticket: str | None = None
) -> str:
    """The one branch name Helm generates for a task.

    The single definition of that name. It was written out at creation and
    then re-derived, without the ticket, everywhere a caller had to decide
    whether a branch was Helm's to touch -- so a ticketed task's
    `helm/<project>/<ticket>-<task>` matched nothing, its branch survived
    cleanup forever, and nothing counted it as retained.
    """
    return f"helm/{project_id}/{ticket}-{task_id}" if ticket else f"helm/{project_id}/{task_id}"


def task_owns_branch(task: dict[str, Any]) -> bool:
    """Whether a task's recorded branch is exactly the one Helm named for it.

    Strict by design, and deliberately not a prefix or pattern test: this is
    the predicate that decides what cleanup may delete. It admits the one name
    `task_branch_name` would produce from that task's own project, id and
    recorded ticket, so a record that somehow carried a base branch, a user's
    branch, or another task's branch is not Helm's to remove.
    """
    branch = task.get("branch")
    if not branch:
        return False
    return branch == task_branch_name(
        str(task.get("project_id") or ""),
        str(task.get("id") or ""),
        task.get("ticket") or None,
    )


#: The manifest a skill directory is recognized by.
SKILL_MANIFEST = "SKILL.md"
#: Where a project may keep task-varying skills. The portable root is readable
#: by any agent; a runtime root belongs to one runtime, which loads it by its
#: own convention, so Helm reads it only for that runtime and does not paste
#: back content that runtime is already going to load for itself.
PORTABLE_SKILL_ROOT = ".agents/skills"
RUNTIME_SKILL_ROOTS: dict[str, str] = {"claude": ".claude/skills"}
#: Bounds, because a skill is content going into somebody's context window.
#: Exceeding one is stated in the selection rather than silently trimmed away:
#: a driver that cannot see what was dropped cannot decide to pin it.
SKILL_CONTENT_LIMIT = 20_000
SKILL_TOTAL_LIMIT = 60_000
SKILL_SELECTION_LIMIT = 5
#: Words too common to be evidence that a skill bears on a brief. Matching is
#: deliberately dull: a driver that wants an exact set pins it.
_SKILL_STOPWORDS = frozenset({
    "the", "a", "an", "and", "or", "for", "to", "of", "in", "on", "with",
    "this", "that", "it", "is", "are", "be", "use", "used", "when", "how",
    "add", "fix", "update", "make", "run", "new", "from", "into", "by", "at",
    "helm", "task", "work", "project", "change", "changes", "file", "files",
})
_ROLE_DIRECTORY = {"foreman": "foremen", "reviewer": "reviewers"}

# What a task is for. A "worker" task produces a change on a branch; a
# "foreman" task produces no change at all -- it drives the project's other
# tasks. Keeping them apart in the record is what lets Helm show a foreman as
# the project's driver instead of as unmerged work nobody can find.
TASK_ROLES = frozenset({"worker", "foreman", "reviewer"})

_MAX_DOMAIN_DEPTH = 5


#: What a learning may be drawn from: what the worker reported and produced,
#: and the outcome of reviewing it. Not its terminal output, and not Helm's own
#: bookkeeping.
LEARNING_EVIDENCE_KINDS = frozenset({
    "result", "blocker", "failure", "approval-needed", "artifact",
    "question", "answer", "approval", "approval-invalidated", "merged",
    "pr-created", "pr-status", "pr-merged",
})
LEARNING_PROPOSAL_STATUSES = {"proposed", "approved", "rejected", "applied"}


def _dt_now():
    import datetime as _dt
    return _dt.datetime.now(_dt.timezone.utc)


def _parse_iso(value: str):
    import datetime as _dt
    return _dt.datetime.fromisoformat(str(value).replace("Z", "+00:00"))


TASK_STATUSES = {
    "created",
    "allocated",
    "running",
    "completed",
    "blocked",
    "failed",
    "approval-needed",
    "approved",
    "pr-open",
    "pr-merged",
    "merged",
}


#: What an approval hold can be. A hold is the record of a task paused on the
#: one thing a worker can never do for itself, and a pause is not an outcome:
#: the session that asked stays live, because the whole point is to tell it to
#: continue once a human has decided.
#:
#: `authorized-pending-delivery` exists because a decision and its arrival are
#: different events. Collapsing them let an undelivered authorization resume the
#: task and close the escalation while no worker had been told anything.
HOLD_STATUSES = frozenset({
    "waiting",
    "authorized-pending-delivery",
    "in-flight",
    "closed",
    "invalidated",
    "abandoned",
})
#: A hold in one of these is still somebody's business.
HOLD_OPEN_STATUSES = frozenset({"waiting", "authorized-pending-delivery", "in-flight"})


#: The whole transition table, as data. Every hold movement in Helm goes through
#: `_move_hold`, so a route that is not written here cannot be reached -- which
#: is the only way an invariant like "a failed task has no live hold" survives
#: contact with five call sites that each looked locally correct.
HOLD_TRANSITIONS: dict[tuple[str, str], str] = {
    # The commander decided. Delivery has not happened yet, so the task stays
    # paused and the escalation stays open.
    ("waiting", "authorize"): "authorized-pending-delivery",
    # Re-running release is a delivery retry, not a second authorization.
    ("authorized-pending-delivery", "authorize"): "authorized-pending-delivery",
    # The worker itself consumed the one-use ticket immediately before acting.
    # This is the only route to acting, and the only place the precondition is
    # checked while it can still prevent the side effect.
    ("authorized-pending-delivery", "consume"): "in-flight",
    ("authorized-pending-delivery", "invalidate"): "invalidated",
    ("in-flight", "invalidate"): "invalidated",
    # The action happened; its receipts are outcome data, not a precondition.
    ("in-flight", "outcome"): "closed",
    ("waiting", "abandon"): "abandoned",
    ("authorized-pending-delivery", "abandon"): "abandoned",
    ("in-flight", "abandon"): "abandoned",
    # A repeat of the same unanswered request is the worker restating itself.
    ("waiting", "restate"): "waiting",
}


#: The task status each open hold state implies. A paused task is paused in one
#: place, so nothing has to remember to keep the two in step.
HOLD_TASK_STATUS = {
    "waiting": "approval-needed",
    "authorized-pending-delivery": "approval-needed",
    "in-flight": "running",
    "invalidated": "approval-needed",
}


#: The gates that hold work still *right now*: a foreman has stopped and cannot
#: launch anything until the commander answers. Delivery and cleanup decisions
#: are gates too, but they trail finished work and can wait a day without
#: costing anything. Filed together they sort identically, so one blocked
#: project reads the same as twenty old branches nobody has swept -- and the
#: list long enough to skip is how a blocking gate goes unanswered for a day.
BLOCKING_GATE_KINDS = frozenset({REQUIREMENT_GATE_KIND, SOLUTION_GATE_KIND})


def _color_for(project_id: str, taken: Sequence[str] = ()) -> str:
    """Pick a colour whose glyph no other project is already using.

    The colour was a plain hash of the project id, so two projects collided as
    soon as the hash did -- and because several palette entries shared a glyph,
    they could collide on the glyph without colliding on the colour. A line
    that says `🟧 hot-story` next to one that says `🟧 android-app-example` is
    exactly the ambiguity the glyph was added to remove.

    The hash still chooses where to start, so a project's colour is stable and
    does not depend on registration order; the search only moves on when the
    preferred glyph is spoken for. With more projects than glyphs it reuses
    rather than failing: an ambiguous glyph is a nuisance, a refused
    registration is a broken workflow.
    """
    digest = hashlib.sha256(project_id.encode("utf-8")).digest()
    start = digest[0] % len(_COLOR_PALETTE)
    claimed = {project_glyph(color) for color in taken if color}
    for offset in range(len(_COLOR_PALETTE)):
        candidate = _COLOR_PALETTE[(start + offset) % len(_COLOR_PALETTE)]
        if project_glyph(candidate) not in claimed:
            return candidate
    return _COLOR_PALETTE[start]
