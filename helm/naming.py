"""Readable names for the things the commander reads about.

Helm's records are keyed by generated ids -- `t-zb35d8a63df0`,
`w-z04ead74c431` -- and that is correct for a key: it is stable, unique, and
never needs rewriting. It is wrong for a *report*. Every surface the commander
reads is a list of agents, and an opaque key forces them to resolve each line
before it means anything:

    09:41   6m  w-z04ead74c431  paused on push
    09:38   9m  w-zc4a02d59f85  review round 2

Six of those is unreadable in practice; the reader skims, and a list that is
skimmed is a list that hides the one item that needed answering.

    09:41   6m  TICKET-123  paused on push
    09:38   9m  TICKET-456  review round 2

So a task carries a NAME as well as an id. The id stays the durable key --
records, locks and references keep it, so a name can never orphan state -- and
the name is for addressing and display only.

The rule, in order:

1. **The tracker id, when the task has one.** That is what a human already
   calls this work: it is in the branch, in the pull request, in standup.
   Inventing a second vocabulary for it helps nobody.
2. **A concise title otherwise** -- a few words naming the deliverable rather
   than the activity. `silent-mic-hard-stop`, never `fix-the-bug` or `round-3`:
   a name has to still mean something a week later.
3. **The id**, when a task offers neither. An ugly name beats a wrong one.
"""

from __future__ import annotations

import re
from typing import Any

#: Words a brief opens with that say nothing about its subject. A name built
#: from them ("the-fix-for-the", "please-update-this") is worse than the id it
#: replaced, because it looks meaningful and is not.
_LEAD_NOISE = frozenset(
    "a an and the this that these those of for to in on at with from your"
    " you we i it its is are be do does make made take taken produce"
    " create build fix close write add update decide run go now first"
    " then please just only exactly every all new one two both".split()
)

#: A tracker id: letters, a dash, digits. Deliberately not anchored to any one
#: tracker's prefixes -- a root may use several, and hard-coding them means a
#: new project silently gets no name.
_TICKET = re.compile(r"\b[A-Z][A-Z0-9]{1,9}-\d+\b")

_WORD = re.compile(r"[A-Za-z0-9]+")

#: How many words a derived title may carry. Three is enough to distinguish
#: work in one project and short enough to sit in a column.
_TITLE_WORDS = 3


def ticket_of(task: dict[str, Any] | None) -> str:
    """The task's tracker id, from its own field or its brief's first line.

    The field is authoritative. The brief is a fallback for tasks created
    before `--ticket` was passed, or by a caller that put the id only in the
    prose -- common enough that ignoring it would leave real work unnamed.
    """
    recorded = str((task or {}).get("ticket") or "").strip()
    if recorded:
        return recorded
    brief = str((task or {}).get("brief") or "")
    first_line = brief.splitlines()[0] if brief else ""
    match = _TICKET.search(first_line[:160])
    return match.group(0) if match else ""


def name_from(text: str | None) -> str:
    """A few words naming what some text is about, or "" if none can be found.

    Reads the FIRST LINE only. That is where a request or a brief says what
    the work is; later text is prose, and matching it picks up whatever the
    author happened to mention.

    This is also the sanitizer for a name supplied from outside: whatever
    comes in leaves as lowercase words joined by dashes, so a name can never
    carry anything a terminal or a log line would treat as structure.
    """
    first_line = (text or "").splitlines()[0] if text else ""
    words: list[str] = []
    for raw in _WORD.findall(first_line[:160]):
        word = raw.lower()
        if word in _LEAD_NOISE:
            # Only skip noise while it is still leading. Once a real word has
            # landed, "the" inside a phrase is part of the phrase.
            if not words:
                continue
        if not words and word.isdigit():
            continue
        words.append(word)
        if len(words) == _TITLE_WORDS:
            break
    return "-".join(words)


def title_of(task: dict[str, Any] | None) -> str:
    """A few words naming what the task is about, or "" if none can be found.

    A RECORDED title wins over the brief. For most tasks the two agree -- the
    brief opens by saying what the work is -- but a driver's brief is its role
    document, which opens by saying what a driver is. Deriving from it named
    every driver in every project `project-s-foreman`, which is worse than the
    id it replaced: it looks meaningful, it is identical for all of them, and
    the disambiguator then hands the commander `project-s-foreman-3`. So the
    caller that knows what the work is records it, and this reads it back.
    """
    recorded = name_from(str((task or {}).get("title") or ""))
    if recorded:
        return recorded
    return name_from(str((task or {}).get("brief") or ""))


def task_name(task: dict[str, Any] | None, *, fallback: str = "") -> str:
    """The name to show a human for this task.

    `fallback` is what to use when the task offers no ticket and no usable
    brief -- normally the id, so a line is never nameless.
    """
    return ticket_of(task) or title_of(task) or fallback or str((task or {}).get("id") or "")


def disambiguate(names: dict[str, str]) -> dict[str, str]:
    """Make an id -> name mapping unique, suffixing only what collides.

    Two tasks can honestly share a name: a follow-up round on one ticket, or
    two slices of it. The FIRST keeps the bare name and later ones take `-2`,
    `-3`, so the common case -- one task per ticket -- never grows a suffix
    nobody needed. Order is the caller's, so a stable input gives a stable
    answer.
    """
    seen: dict[str, int] = {}
    out: dict[str, str] = {}
    for task_id, name in names.items():
        count = seen.get(name, 0) + 1
        seen[name] = count
        out[task_id] = name if count == 1 else f"{name}-{count}"
    return out
