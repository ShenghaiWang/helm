"""The learned tail of a knowledge file, and how much of it a worker sees.

`helm learning` appends every approved learning to the end of a domain's
knowledge.md, so the file grows with every approval and never shrinks. The
authored sections are what a domain is; the learned tail is what it has
picked up since, newest last. A worker's context gets the authored text
whole and the newest learnings up to a budget, and is told how many earlier
ones were left out and where they remain -- a bounded context that says its
bound is worth more than an unbounded one nobody can read to the end.
"""

from __future__ import annotations

import re

from .values import LEARNED_KNOWLEDGE_BUDGET_BYTES

LEARNING_HEADING = "## Approved learning: "
_BLOCK_START = re.compile(r"(?=(?:^|\n)## Approved learning: )")


def always_in_full(text: str) -> bool:
    """Does this knowledge file declare that it is never to be indexed?

    A domain whose rubric every role must actually read -- not merely know
    exists -- says so in its own frontmatter with `always_in_full: true`.
    The declaration lives with the knowledge, so the domain's author decides,
    not the budget.
    """
    return re.search(r"^always_in_full:\s*true\s*$", text, re.M | re.I) is not None


def domain_index(domain_id: str, text: str, command: str) -> str:
    """A short stand-in for a knowledge file: what it is for, its headings, and how to read it.

    An index is what a worker gets for a base domain the context budget did
    not cover in full. Every heading is listed, so the worker knows the topic
    is covered somewhere and reads the domain before acting on it, rather
    than not knowing there was guidance at all.
    """
    applies_to = ""
    match = re.search(r"^applies_to:\s*(.+)$", text, re.M)
    if match:
        applies_to = match.group(1).strip()
    headings = [
        line.lstrip("#").strip()
        for line in text.splitlines()
        if line.startswith("## ") or line.startswith("### ")
    ]
    headings = [h for h in headings if not h.startswith("Approved learning:")]
    lines = [f"{domain_id}: {applies_to or 'inherited domain guidance'}"]
    if headings:
        lines.append("Sections: " + "; ".join(headings))
    lines.append(
        "Not included in full, to keep this context small. Before acting on any "
        f"of these topics, read it: {command}"
    )
    return "\n".join(lines) + "\n"


def split_learned(text: str) -> tuple[str, list[str]]:
    """The authored body, and each approved-learning block in file order."""
    match = re.search(r"(?:^|\n)## Approved learning: ", text)
    if match is None:
        return text, []
    authored = text[: match.start()]
    tail = text[match.start():]
    blocks = [block for block in _BLOCK_START.split(tail) if block.strip()]
    return authored, blocks


def bound_learned_knowledge(
    text: str, source: str, budget: int = LEARNED_KNOWLEDGE_BUDGET_BYTES
) -> tuple[str, int]:
    """Keep the authored text and the newest learnings that fit the budget.

    Returns the bounded text and how many earlier learnings were left out.
    The newest learning is always kept, even alone over the budget: a bound
    that dropped the most recent ruling would hide exactly the one thing the
    commander just taught.
    """
    authored, blocks = split_learned(text)
    if not blocks:
        return text, 0
    kept: list[str] = []
    used = 0
    for block in reversed(blocks):
        if kept and used + len(block) > budget:
            break
        kept.append(block)
        used += len(block)
    kept.reverse()
    omitted = len(blocks) - len(kept)
    note = ""
    if omitted:
        note = (
            f"\n\n_{omitted} earlier approved learning(s) left out of this context for "
            f"size; they remain in {source}. Fold what still matters into the "
            "sections above so every worker sees it._\n"
        )
    return authored + "".join(kept) + note, omitted
