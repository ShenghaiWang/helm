"""Evaluate Helm against what it replaced: replay closed tickets on several arms.

The question Helm exists to answer is whether delegation with a protocol --
foreman, gates, worker, independent review -- earns its cost over a single
agent handed the same ticket, and over delegation without the protocol. That
is decided by measurement, not by prose, and this module is the measuring.

The corpus is a set of closed tickets whose shipped change is known: each has
the base commit the real fix started from and the merge commit it landed as.
A run replays one ticket on one arm from that base and records what came
back; a judge on an independent model scores the candidate against the
shipped diff; a report lays the arms side by side per ticket.

Everything here is generic. The corpus, the runs, the verdicts and the report
live under the ignored `state/evaluation/` directory, because a ticket id and
a base commit are facts about one root's projects, not about Helm.
"""

from __future__ import annotations

import contextlib
import datetime as _dt
import json
import os
import re
import shlex
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

from . import costs, runtimes
from .errors import HelmError
from .launching import worker_environment
from .paths import _write_private_text

ARMS = ("single", "firstmate", "helm")

#: How a judge scores a candidate against the shipped change.
JUDGE_SCORES = {
    0: "wrong: does not address the ticket, or breaks something the shipped change kept",
    1: "partial: addresses part of the ticket, or the right idea with a defect",
    2: "equivalent: solves the ticket as the shipped change did",
    3: "better: solves it and improves on the shipped change in a way a reviewer would accept",
}

#: The kinds a human intervention is counted under, and the message kinds
#: that evidence each. Counting by kind is the commander's decision: a
#: coordinator asking for an authorization and one asking to resolve an
#: ambiguity are different costs.
INTERVENTION_KINDS = ("authorization", "ambiguity", "escalation")

#: The branch an arm repository's primary checkout sits on. Every run resets
#: it to the ticket's base, so the repository reads as the project did on
#: the day the ticket was picked up.
PRIMARY_BRANCH = "main"

#: What every arm is told about where its ticket comes from, in the same
#: words on every arm. A replayed ticket has already shipped, and the
#: shipped change is one lookup away for an agent that knows where to look
#: -- the tracker, the pull request, another branch of the same repository.
#: The first run that found it cherry-picked the real fix and reported it
#: as its own, which measured nothing. So the rule is stated, and then
#: enforced from outside the arm: the arm is never told the real ticket id,
#: its repository is stripped to the history the ticket started from, and a
#: candidate that copied the shipped change is caught when it is collected.
REPLAY_RULE = (
    "This ticket is a replay: it was fixed and shipped elsewhere already, and what "
    "this run measures is your own solution. Work only from the ticket text below and "
    "the code in this checkout. Do not consult an issue tracker, GitHub, pull requests, "
    "other branches, other checkouts or the remote's other refs, and do not cherry-pick, "
    "fetch or copy a commit from anywhere. A run that does any of these is discarded."
)

_ALIAS_PATTERN = re.compile(r"^EVAL-(\d+)$")


def _now() -> str:
    return _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _git(repo: str | Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(repo), *args],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    if result.returncode != 0:
        raise HelmError(f"git {' '.join(args)} in {repo}: {result.stderr.strip()}")
    return result.stdout


def _kill_run_process(pid: Any) -> None:
    """End a detached run: its wrapper leads its own process group, so the
    group is what dies -- the agent and everything it started."""
    if not isinstance(pid, int) or pid <= 0:
        return
    for sig in (signal.SIGTERM, signal.SIGKILL):
        try:
            os.killpg(pid, sig)
        except ProcessLookupError:
            return
        except OSError:
            with contextlib.suppress(OSError):
                os.kill(pid, sig)
        for _ in range(50):
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                return
            time.sleep(0.1)


def _spawn_detached(command: list[str], *, cwd: str, env: dict[str, str]) -> int:
    """Start a command that outlives this process and its whole tree.

    The first single-agent run died without a trace: no exit code, empty
    output. It had been started as a child of the CLI, and when the CLI's
    caller cleaned up its process tree the agent went with it -- the same
    lesson the worker runner learned. So the command is started through a
    bootstrap that forks, prints the child's pid, and exits; the child
    becomes its own session, reparented to init, where nothing that walks
    the caller's tree can reach it.
    """
    bootstrap = (
        "import os, sys\n"
        "pid = os.fork()\n"
        "if pid:\n"
        "    sys.stdout.write(f'{pid}\\n'); sys.stdout.flush(); os._exit(0)\n"
        "os.setsid()\n"
        "devnull = os.open(os.devnull, os.O_RDWR)\n"
        "for fd in (0, 1, 2):\n"
        "    os.dup2(devnull, fd)\n"
        "os.execvp(sys.argv[1], sys.argv[1:])\n"
    )
    process = subprocess.Popen(
        [sys.executable, "-c", bootstrap, *command],
        cwd=cwd, env=env, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL, start_new_session=True, text=True,
    )
    assert process.stdout is not None
    line = process.stdout.readline().strip()
    process.stdout.close()
    process.wait()
    if not line.isdigit():
        raise HelmError("could not start the detached command")
    return int(line)


class Evaluation:
    """The evaluation's own records, under one directory."""

    def __init__(self, directory: Path):
        self.directory = Path(directory)
        self.corpus_file = self.directory / "corpus.json"
        self.runs_dir = self.directory / "runs"

    # ---------- corpus ----------

    def load_corpus(self) -> dict[str, Any]:
        if not self.corpus_file.is_file():
            return {"tickets": [], "checks": [], "settings": {}}
        try:
            data = json.loads(self.corpus_file.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise HelmError(f"corpus unreadable: {exc}") from exc
        data.setdefault("tickets", [])
        data.setdefault("checks", [])
        data.setdefault("settings", {})
        if self._assign_aliases(data):
            self.save_corpus(data)
        return data

    @staticmethod
    def _assign_aliases(data: dict[str, Any]) -> bool:
        """Give every ticket the name the arms know it by; True when any was missing.

        The alias is opaque on purpose. A real ticket id keys the tracker
        that holds the shipped fix, and even its number alone keys a search
        of the pull requests; `EVAL-3` keys nothing.
        """
        used = set()
        for entry in data["tickets"]:
            match = _ALIAS_PATTERN.match(str(entry.get("alias") or ""))
            if match:
                used.add(int(match.group(1)))
        changed = False
        for entry in data["tickets"]:
            if _ALIAS_PATTERN.match(str(entry.get("alias") or "")):
                continue
            number = max(used, default=0) + 1
            used.add(number)
            entry["alias"] = f"EVAL-{number}"
            changed = True
        return changed

    def save_corpus(self, data: dict[str, Any]) -> None:
        self.directory.mkdir(parents=True, exist_ok=True)
        _write_private_text(self.corpus_file, json.dumps(data, indent=2) + "\n")

    def add_ticket(
        self,
        ticket: str,
        *,
        base: str,
        merge: str,
        brief: str,
        title: str = "",
        pr: int | None = None,
    ) -> dict[str, Any]:
        """Register one closed ticket: where its fix started and what shipped."""
        if not ticket.strip():
            raise HelmError("a ticket id is required")
        if not re.fullmatch(r"[0-9a-f]{7,40}", base) or not re.fullmatch(r"[0-9a-f]{7,40}", merge):
            raise HelmError("base and merge must be commit shas")
        if not brief.strip():
            raise HelmError("the brief is empty; the ticket's own description is the brief")
        data = self.load_corpus()
        previous = next((t for t in data["tickets"] if t.get("id") == ticket.strip()), None)
        entry = {
            "id": ticket.strip(),
            "title": title.strip(),
            "pr": pr,
            "base": base,
            "merge": merge,
            "brief": brief.strip(),
            "added_at": _now(),
        }
        if previous and previous.get("alias"):
            entry["alias"] = previous["alias"]
        data["tickets"] = [t for t in data["tickets"] if t.get("id") != entry["id"]] + [entry]
        self._assign_aliases(data)
        self.save_corpus(data)
        return entry

    def alias(self, ticket: str) -> str:
        """The name an arm knows the ticket by. Never its real id."""
        return str(self.ticket(ticket)["alias"])

    def ticket(self, ticket: str) -> dict[str, Any]:
        for entry in self.load_corpus()["tickets"]:
            if entry.get("id") == ticket:
                return entry
        raise HelmError(f"ticket {ticket} is not in the corpus")

    # ---------- runs ----------

    def run_dir(self, ticket: str, arm: str) -> Path:
        if arm not in ARMS:
            raise HelmError(f"unknown arm {arm!r}; arms are {', '.join(ARMS)}")
        return self.runs_dir / ticket / arm

    def run_file(self, ticket: str, arm: str) -> Path:
        return self.run_dir(ticket, arm) / "run.json"

    def load_run(self, ticket: str, arm: str) -> dict[str, Any] | None:
        path = self.run_file(ticket, arm)
        if not path.is_file():
            return None
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise HelmError(f"run record unreadable: {path}: {exc}") from exc

    def save_run(self, record: dict[str, Any]) -> Path:
        path = self.run_file(record["ticket"], record["arm"])
        path.parent.mkdir(parents=True, exist_ok=True)
        record["updated_at"] = _now()
        _write_private_text(path, json.dumps(record, indent=2) + "\n")
        return path

    def new_run(self, ticket: str, arm: str, *, restart: bool = False, **fields: Any) -> dict[str, Any]:
        entry = self.ticket(ticket)
        existing = self.load_run(ticket, arm)
        if existing:
            live = existing.get("status") in ("running", "started")
            if live and not restart:
                raise HelmError(
                    f"{ticket} on {arm} is already running (started {existing.get('started_at')}); "
                    "finish it, or start again with --restart"
                )
            # Kept, not overwritten: an abandoned run is part of the record,
            # and so is a finished one that a fresh run replaces -- a
            # contaminated result is exactly the kind a later reader asks about.
            stamp = str(existing.get("started_at") or "unknown").replace(":", "")
            word = "abandoned" if live else "superseded"
            aside = self.run_dir(ticket, arm) / f"run-{word}-{stamp}.json"
            if live:
                existing["status"] = "abandoned"
            _write_private_text(aside, json.dumps(existing, indent=2) + "\n")
        record = {
            "ticket": ticket,
            "arm": arm,
            "alias": entry["alias"],
            "base": entry["base"],
            "merge": entry["merge"],
            "status": "started",
            "started_at": _now(),
            "ended_at": None,
            "tip": None,
            "task_ids": [],
            "checks": [],
            "judge": None,
            "metrics": {},
            **fields,
        }
        self.save_run(record)
        return record

    def finish_run(self, ticket: str, arm: str, *, status: str, tip: str | None, **fields: Any) -> dict[str, Any]:
        record = self.load_run(ticket, arm)
        if record is None:
            raise HelmError(f"no run recorded for {ticket} on {arm}")
        record.update(fields)
        record["status"] = status
        record["tip"] = tip
        record["ended_at"] = _now()
        self.save_run(record)
        return record

    # ---------- diffs the judge reads ----------

    def write_diffs(self, ticket: str, arm: str, *, repo: str | Path, tip: str) -> dict[str, str]:
        """The candidate's diff and the shipped diff, both from the same base."""
        entry = self.ticket(ticket)
        judge_dir = self.run_dir(ticket, arm) / "judge"
        judge_dir.mkdir(parents=True, exist_ok=True)
        paths = {
            "candidate": str(judge_dir / "candidate.patch"),
            "shipped": str(judge_dir / "shipped.patch"),
            "brief": str(judge_dir / "brief.md"),
        }
        # The candidate's branch does not outlive the next ticket's run in the
        # same repository, so the patch written at collection is what the
        # judge reads once the branch is gone.
        for key, target in (("candidate", tip), ("shipped", entry["merge"])):
            try:
                text = _git(repo, "diff", f"{entry['base']}...{target}")
            except HelmError:
                if Path(paths[key]).is_file():
                    continue
                raise
            _write_private_text(Path(paths[key]), text)
        _write_private_text(
            Path(paths["brief"]),
            f"# {entry['id']}: {entry.get('title', '')}\n\n{entry['brief']}\n",
        )
        return paths

    def judge_brief(self, ticket: str, arm: str, paths: dict[str, str]) -> str:
        """What the judge is asked. Score first, in JSON, so Helm can read it."""
        scale = "\n".join(f"  {score}: {meaning}" for score, meaning in JUDGE_SCORES.items())
        return (
            f"JUDGE, READ-ONLY. You are scoring a candidate change for ticket {ticket} "
            f"(arm: {arm}) against the change that actually shipped. Change nothing.\n\n"
            f"Read, in this order: the ticket at {paths['brief']}; the candidate diff at "
            f"{paths['candidate']}; the shipped diff at {paths['shipped']}. Both diffs are "
            "against the same base commit. The shipped diff is a reference for what the "
            "ticket needed, not an answer key: a candidate that solves the ticket a "
            "different, sound way is equivalent, and one that copies the shipped shape "
            "with a defect is not.\n\n"
            f"Score on this scale:\n{scale}\n\n"
            "Your result message MUST begin with one line of JSON and nothing before it:\n"
            '{"score": <0-3>, "solves_ticket": <true|false>, "defects": ["..."], '
            '"missing": ["..."], "rationale": "<two sentences>"}\n'
            "Then, below it, your findings in prose: what the candidate got right, what "
            "it missed or broke relative to the ticket, and whether a reviewer would "
            "merge it. Judge the diff as a reviewer would; do not run the project."
        )

    @staticmethod
    def parse_verdict(text: str) -> dict[str, Any] | None:
        """The judge's JSON line, or None when the result did not start with one."""
        for line in text.splitlines():
            stripped = line.strip()
            if not stripped:
                continue
            if not stripped.startswith("{"):
                return None
            try:
                verdict = json.loads(stripped)
            except ValueError:
                return None
            if not isinstance(verdict, dict) or "score" not in verdict:
                return None
            try:
                verdict["score"] = int(verdict["score"])
            except (TypeError, ValueError):
                return None
            if verdict["score"] not in JUDGE_SCORES:
                return None
            return verdict
        return None

    # ---------- report ----------

    def report(self) -> str:
        """Every ticket, every arm, side by side. Blank where nothing ran."""
        corpus = self.load_corpus()
        lines = [
            "| ticket | arm | status | judge | checks | interventions | review catches | wall | tokens (in/out/cache) | notes |",
            "|---|---|---|---|---|---|---|---|---|---|",
        ]
        for entry in corpus["tickets"]:
            for arm in ARMS:
                run = self.load_run(entry["id"], arm)
                if run is None:
                    lines.append(f"| {entry['id']} | {arm} | — | | | | | | | |")
                    continue
                notes = "; ".join(
                    [f"CONTAMINATED: {'; '.join(run['contamination'])}"] if run.get("contamination") else []
                    + [str(n.get("text", "")) for n in (run.get("notes") or [])]
                )
                judge = run.get("judge") or {}
                score = judge.get("score")
                metrics = run.get("metrics") or {}
                interventions = metrics.get("interventions") or {}
                by_kind = ", ".join(
                    f"{kind} {interventions.get(kind, 0)}"
                    for kind in INTERVENTION_KINDS
                    if interventions.get(kind)
                ) or "0"
                checks = run.get("checks") or []
                checks_cell = (
                    f"{sum(1 for c in checks if c.get('ok'))}/{len(checks)} pass" if checks else "not run"
                )
                usage = metrics.get("usage") or {}
                tokens = (
                    f"{usage.get('input_tokens', 0)}/{usage.get('output_tokens', 0)}/"
                    f"{usage.get('cache_read_input_tokens', 0)}"
                    if usage else ""
                )
                wall = metrics.get("wall_seconds")
                wall_cell = f"{int(wall) // 60}m" if isinstance(wall, (int, float)) else ""
                lines.append(
                    f"| {entry['id']} | {arm} | {run.get('status')} | "
                    f"{score if score is not None else ''} | {checks_cell} | {by_kind} | "
                    f"{metrics.get('review_catches', '')} | {wall_cell} | {tokens} | {notes} |"
                )
        return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# Running the arms
# ---------------------------------------------------------------------------


#: A task in one of these states has said its last word for the run.
TERMINAL_TASK_STATUSES = frozenset({
    "completed", "blocked", "failed", "approval-needed", "approved",
    "pr-open", "pr-merged", "merged",
})


def _is_base(tip: str | None, base: str | None) -> bool:
    """Whether a tip is the base commit itself -- the corpus may hold the
    base as a short sha, and a rev-parsed tip is always the full one."""
    return bool(tip and base and (tip == base or tip.startswith(base)))


def _stamp_to_epoch(value: Any) -> float | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        return _dt.datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def _ticket_text(ticket: dict[str, Any]) -> str:
    """The ticket as the arms see it: under its alias, never its real id."""
    return f"TICKET {ticket['alias']}:\n{ticket['brief']}"


def single_agent_brief(ticket: dict[str, Any]) -> str:
    """What the lone agent is told: the ticket, and how to leave its work."""
    return (
        f"You are implementing ticket {ticket['alias']}"
        + (f" -- {ticket['title']}" if ticket.get("title") else "")
        + ", alone, in this git worktree, which is checked out at the commit the work "
        "starts from. Read the repository's own contributor guidance first "
        "(AGENTS.md / CLAUDE.md / README) and follow it. Implement the ticket "
        "completely, run the project's own checks for what you touched, and commit "
        "everything on the current branch with a clear message. Do not push, do not "
        "open a pull request, and do not modify anything outside this worktree. If "
        "something is genuinely ambiguous, decide it yourself, say what you decided "
        "and why in your final message, and keep going. When you are finished, end "
        "your final message with the line: DONE.\n\n"
        f"{REPLAY_RULE}\n\n"
        f"{_ticket_text(ticket)}"
    )


def firstmate_brief(ticket: dict[str, Any]) -> str:
    """What the launched worker is told: the ticket, under the replay rule."""
    return (
        f"Implement ticket {ticket['alias']}"
        + (f" ({ticket['title']})" if ticket.get("title") else "")
        + f".\n\n{REPLAY_RULE}\n\n{_ticket_text(ticket)}"
    )


def helm_request(ticket: dict[str, Any], base_branch: str) -> str:
    """The commander's request as routed to a foreman: the ticket, verbatim."""
    return (
        f"Implement ticket {ticket['alias']}"
        + (f" ({ticket['title']})" if ticket.get("title") else "")
        + f". Create the task with --ticket {ticket['alias']} --base {base_branch} --new: that "
        "branch is the exact commit this work starts from, and the task must start from "
        "it, not from the project's base branch. Local delivery; no push, no PR.\n\n"
        f"{REPLAY_RULE} Put that rule, in those words, at the top of the worker's brief.\n\n"
        f"{_ticket_text(ticket)}"
    )


class Runner:
    """Start, collect, judge and score runs, against the coordinator's records.

    Settings live in the corpus file, because they are facts about this
    root: which registered projects host the firstmate and helm arms, the
    repository the single agent works in, the judge's runtime and model, and
    the check commands. Nothing here names a project of its own.
    """

    def __init__(self, coordinator: Any, evaluation: Evaluation):
        self.coordinator = coordinator
        self.evaluation = evaluation

    # ---------- settings ----------

    def settings(self) -> dict[str, Any]:
        return self.evaluation.load_corpus().get("settings") or {}

    def _setting(self, *keys: str) -> Any:
        value: Any = self.settings()
        for key in keys:
            if not isinstance(value, dict):
                return None
            value = value.get(key)
        return value

    #: How long one run may take, in minutes, when the corpus does not say.
    #: An agent that has not finished in two hours is not going to be judged
    #: better for a third, and in practice somebody would have stepped in;
    #: the cap is the same for every arm, so running out of it is a fair
    #: failure rather than a mercy to one side.
    DEFAULT_MAX_MINUTES = 120

    def max_minutes(self) -> float:
        value = self._setting("max_minutes")
        if isinstance(value, (int, float)) and not isinstance(value, bool) and value > 0:
            return float(value)
        return float(self.DEFAULT_MAX_MINUTES)

    def _past_cap(self, record: dict[str, Any]) -> bool:
        started = _stamp_to_epoch(record.get("started_at"))
        return started is not None and time.time() - started > self.max_minutes() * 60

    def repo_for(self, arm: str) -> Path:
        """The git repository that holds an arm's candidate tip."""
        if arm == "single":
            root = self._setting("repo")
        else:
            project_id = self._setting("projects", arm)
            root = self.coordinator.get_project(project_id)["root"] if project_id else None
        if not root:
            raise HelmError(
                f"the corpus settings do not say where the {arm} arm runs; set "
                f"settings.{'repo' if arm == 'single' else 'projects.' + arm} in the corpus file"
            )
        return Path(root)

    def base_branch(self, ticket: str) -> str:
        return f"eval/{self.evaluation.alias(ticket)}-base"

    def work_dir(self, ticket: str, arm: str) -> Path:
        """Where an arm's own checkout goes. Named by the alias: the path is
        the agent's working directory, and a working directory gets read."""
        return self.evaluation.directory / "work" / self.evaluation.alias(ticket) / arm

    def _origin(self, repo: Path) -> Path | None:
        """The arm repository's origin, which must be a local bare repository
        the evaluation owns -- or nothing. Every run strips the origin to the
        ticket's base along with the clone, and a real remote is not the
        evaluation's to strip."""
        try:
            url = _git(repo, "remote", "get-url", "origin").strip()
        except HelmError:
            return None
        path = Path(url[len("file://"):] if url.startswith("file://") else url)
        bare = ""
        if path.is_absolute() and path.is_dir():
            with contextlib.suppress(HelmError):
                bare = _git(path, "rev-parse", "--is-bare-repository").strip()
        if bare != "true":
            raise HelmError(
                f"{repo}: origin {url} is not a local bare repository. An arm repository "
                "is a clone the evaluation owns, and its origin must be one too, because "
                "every run strips both to the history the ticket starts from."
            )
        return path

    def sanitize(self, ticket: str, arm: str) -> dict[str, list[str]]:
        """Strip the arm's repository to the history the ticket starts from.

        A clone of the project carries every branch and tag the project has,
        and after the ticket shipped those reach its fix: `git log --all
        --grep` finds the commit, a release branch carries the cherry-pick,
        the origin's other refs serve it to a fetch. A run in that
        repository is a run with the answer on the shelf. So before a run,
        nothing the arm can list may reach past the ticket's base: tags go,
        remote-tracking refs go, every branch that is not the primary or the
        ticket's own base goes, worktrees of earlier runs go (their records
        and patches are kept), the primary is reset to the base, and the
        origin keeps only the base. What is not the evaluation's to delete
        -- a Helm task branch, which a task record owns until `helm task
        cleanup` -- is refused by name when it reaches past the base, so the
        run does not start with it there.
        """
        entry = self.evaluation.ticket(ticket)
        repo = self.repo_for(arm)
        base = entry["base"]
        keep = {PRIMARY_BRANCH, self.base_branch(ticket)}
        removed: dict[str, list[str]] = {
            "worktrees": [], "tags": [], "branches": [], "remote_refs": [], "origin_refs": [],
        }
        if _git(repo, "status", "--porcelain", "--untracked-files=no").strip():
            raise HelmError(
                f"{repo} has uncommitted changes in its primary checkout; an arm "
                "repository is never edited there, so this is somebody's work -- "
                "resolve it by hand before a run resets the checkout"
            )
        origin = self._origin(repo)
        # Earlier runs' checkouts on eval branches. Helm task worktrees are
        # not touched: their tasks own them.
        primary = repo.resolve()
        for path, branch in self._worktrees(repo):
            if path == primary or not branch.startswith("eval/") or branch in keep:
                continue
            _git(repo, "worktree", "remove", "--force", str(path))
            removed["worktrees"].append(str(path))
        _git(repo, "worktree", "prune")
        for tag in _git(repo, "tag", "--list").split():
            _git(repo, "tag", "-d", tag)
            removed["tags"].append(tag)
        refused: list[str] = []
        for branch in _git(repo, "for-each-ref", "--format=%(refname:short)", "refs/heads").split():
            if branch in keep:
                continue
            if branch.startswith("helm/"):
                if not self._reaches_only(repo, branch, base):
                    refused.append(branch)
                continue
            _git(repo, "branch", "-D", branch)
            removed["branches"].append(branch)
        head = ""
        with contextlib.suppress(HelmError):
            head = _git(repo, "symbolic-ref", "--short", "-q", "HEAD").strip()
        if head == PRIMARY_BRANCH:
            _git(repo, "reset", "-q", "--hard", base)
        else:
            _git(repo, "checkout", "-q", "-B", PRIMARY_BRANCH, base)
        if origin is not None:
            wanted = f"refs/heads/{self.base_branch(ticket)}"
            for ref in _git(origin, "for-each-ref", "--format=%(refname)").split():
                if ref == wanted:
                    continue
                _git(origin, "update-ref", "--no-deref", "-d", ref)
                removed["origin_refs"].append(ref)
        for ref in _git(repo, "for-each-ref", "--format=%(refname)", "refs/remotes").split():
            if ref == f"refs/remotes/origin/{self.base_branch(ticket)}":
                continue
            # `--no-deref`: origin/HEAD is a symbolic ref, and deleting through
            # it would delete its target instead of it.
            _git(repo, "update-ref", "--no-deref", "-d", ref)
            removed["remote_refs"].append(ref)
        if refused:
            data = self.coordinator.store.load()
            owners = {
                task.get("branch"): task_id
                for task_id, task in data.get("tasks", {}).items() if task.get("branch")
            }
            named = ", ".join(
                f"{branch} (task {owners[branch]})" if branch in owners else branch
                for branch in refused
            )
            raise HelmError(
                f"{repo}: these Helm task branches reach past the ticket's base and are "
                f"not the evaluation's to delete: {named}. Clean their tasks up first "
                "(helm task cleanup <task> --delete-branch, with the commander's "
                "approval), then start the run again."
            )
        return removed

    @staticmethod
    def _worktrees(repo: Path) -> list[tuple[Path, str]]:
        entries: list[tuple[Path, str]] = []
        path: Path | None = None
        branch = ""
        for line in _git(repo, "worktree", "list", "--porcelain").splitlines() + [""]:
            if line.startswith("worktree "):
                path = Path(line[len("worktree "):]).resolve()
                branch = ""
            elif line.startswith("branch refs/heads/"):
                branch = line[len("branch refs/heads/"):]
            elif not line and path is not None:
                entries.append((path, branch))
                path = None
        return entries

    @staticmethod
    def _reaches_only(repo: Path, ref: str, base: str) -> bool:
        """True when nothing on `ref` lies outside the base's own history."""
        result = subprocess.run(
            ["git", "-C", str(repo), "merge-base", "--is-ancestor", ref, base],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False,
        )
        return result.returncode == 0

    def prepare_base(self, ticket: str, arm: str) -> str:
        """Point the arm's repository at the commit the shipped fix started
        from, and at nothing past it."""
        entry = self.evaluation.ticket(ticket)
        repo = self.repo_for(arm)
        for sha in (entry["base"], entry["merge"]):
            try:
                _git(repo, "cat-file", "-e", f"{sha}^{{commit}}")
            except HelmError as exc:
                raise HelmError(
                    f"{repo} does not have {sha}; fetch the project's history there first"
                ) from exc
        self.sanitize(ticket, arm)
        branch = self.base_branch(ticket)
        _git(repo, "branch", "-f", branch, entry["base"])
        # Helm will not start a task from an unverified local tip: a base
        # branch needs an upstream that agrees with it. So when the arm's
        # repository has an origin, the base is published there and tracked
        # -- the origin is the evaluation's own bare repository, which
        # `sanitize` has just emptied of everything else.
        if self._origin(repo) is None:
            return branch
        _git(repo, "push", "-q", "--force", "origin", f"{entry['base']}:refs/heads/{branch}")
        _git(repo, "fetch", "-q", "--prune", "origin")
        _git(repo, "branch", f"--set-upstream-to=origin/{branch}", branch)
        return branch

    # ---------- single agent ----------

    def start_single(
        self, ticket: str, *, model: str | None = None, restart: bool = False,
        effort: str | None = None,
    ) -> dict[str, Any]:
        entry = self.evaluation.ticket(ticket)
        repo = self.repo_for("single")
        self.prepare_base(ticket, "single")
        run_dir = self.evaluation.run_dir(ticket, "single")
        run_dir.mkdir(parents=True, exist_ok=True)
        worktree = self.work_dir(ticket, "single")
        branch = f"eval/{entry['alias']}/single"
        if worktree.exists():
            _git(repo, "worktree", "remove", "--force", str(worktree))
        worktree.parent.mkdir(parents=True, exist_ok=True)
        _git(repo, "worktree", "add", "-B", branch, str(worktree), entry["base"])
        runtime = runtimes.builtin_runtime("claude")
        if runtime is None:
            raise HelmError("the single-agent arm runs on claude, which is not a known runtime")
        model = model or self._setting("single", "model") or None
        effort = effort or self._setting("effort") or None
        # `--print` is the non-interactive mode and the prompt is positional;
        # `-p` is the same flag, not a prompt option. Effort is stated when
        # the corpus states one, so every arm thinks as hard as the others:
        # the runtime's own default is low, and a comparison where one arm
        # got that silently is not a comparison.
        command = [
            "claude", "--print", "--output-format", "json",
            "--permission-mode", "bypassPermissions",
        ]
        if model:
            command += ["--model", model]
        if effort:
            command += ["--effort", str(effort)]
        command.append(single_agent_brief(entry))
        env = worker_environment()
        env.update(runtime.environment(os.environ))
        out = run_dir / "output.json"
        err = run_dir / "stderr.log"
        exit_file = run_dir / "exit"
        # The run directory is the ticket's, so a previous run's exit code
        # is still there -- and it read as this run finishing the moment it
        # started. Nothing of the previous run survives here but its record.
        for stale in (out, err, exit_file):
            stale.unlink(missing_ok=True)
        # A shell wrapper leaves the exit code where a later `status` can read
        # it, since this CLI invocation does not stay to wait for the agent.
        script = (
            f"{shlex.join(command)} > {shlex.quote(str(out))} 2> {shlex.quote(str(err))}; "
            f"echo $? > {shlex.quote(str(exit_file))}"
        )
        pid = _spawn_detached(["sh", "-c", script], cwd=str(worktree), env=env)
        record = self.evaluation.new_run(
            ticket, "single", status="running", pid=pid, worktree=str(worktree),
            branch=branch, model=model, effort=effort, output=str(out), exit_file=str(exit_file),
            restart=restart,
        )
        return record

    def _collect_single(self, record: dict[str, Any]) -> dict[str, Any]:
        exit_file = Path(record.get("exit_file") or "")
        if not exit_file.is_file():
            return record
        worktree = Path(record["worktree"])
        try:
            code = int(exit_file.read_text().strip() or "1")
        except ValueError:
            code = 1
        payload: dict[str, Any] = {}
        try:
            payload = json.loads(Path(record["output"]).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            payload = {}
        tip = None
        with contextlib.suppress(HelmError):
            tip = _git(worktree, "rev-parse", "HEAD").strip()
        if _is_base(tip, record.get("base")):
            tip = None  # nothing committed is no candidate
        usage_fields = {
            "input_tokens": 0, "output_tokens": 0,
            "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0,
        }
        reported = payload.get("usage") if isinstance(payload, dict) else None
        if isinstance(reported, dict):
            for field in usage_fields:
                if isinstance(reported.get(field), (int, float)):
                    usage_fields[field] = int(reported[field])
        session_id = payload.get("session_id") if isinstance(payload, dict) else None
        transcript = None
        if isinstance(session_id, str):
            found = costs.find_transcripts(worktree, session_id=session_id)
            if found:
                transcript = costs.transcript_usage(found[0])
                if not any(usage_fields.values()):
                    for field in usage_fields:
                        usage_fields[field] = transcript[field]
        started = _stamp_to_epoch(record.get("started_at")) or time.time()
        duration_ms = payload.get("duration_ms") if isinstance(payload, dict) else None
        wall = (duration_ms / 1000.0) if isinstance(duration_ms, (int, float)) else time.time() - started
        result_text = payload.get("result") if isinstance(payload, dict) else None
        metrics = {
            "wall_seconds": wall,
            "usage": usage_fields,
            "cost_usd": payload.get("total_cost_usd") if isinstance(payload, dict) else None,
            "turns": payload.get("num_turns") if isinstance(payload, dict) else None,
            # A lone agent cannot ask anyone; every decision it made is in its
            # final message for the judge and the commander to read.
            "interventions": {kind: 0 for kind in INTERVENTION_KINDS},
            "review_catches": 0,
            "declared_done": bool(isinstance(result_text, str) and result_text.rstrip().endswith("DONE.")),
        }
        status = "completed" if code == 0 and tip else "failed"
        contamination: list[str] = []
        if tip:
            transcripts = costs.find_transcripts(worktree, session_id=session_id) if session_id else []
            contamination = self._contamination(record, worktree, tip, transcripts)
            self.evaluation.write_diffs(record["ticket"], "single", repo=worktree, tip=tip)
        return self.evaluation.finish_run(
            record["ticket"], "single", status="contaminated" if contamination else status,
            tip=tip, exit_code=code, session_id=session_id, metrics=metrics,
            contamination=contamination,
            result_text=result_text if isinstance(result_text, str) else None,
        )

    def _contamination(
        self, record: dict[str, Any], repo: str | Path, tip: str, transcripts: list[Path]
    ) -> list[str]:
        """Evidence that a candidate saw the shipped change; empty when clean.

        The arm was told an alias, so the real id in its commits or its
        transcript can only have come from looking; the shipped commit and
        pull request likewise. A cherry-pick trailer, or a diff identical to
        what shipped, is the copy itself.
        """
        entry = self.evaluation.ticket(record["ticket"])
        base, merge = entry["base"], entry["merge"]
        marks = [(merge[:10], f"the shipped commit {merge[:10]}")]
        if entry.get("pr"):
            marks.append((f"#{entry['pr']}", f"the shipped pull request #{entry['pr']}"))
        if record.get("alias"):
            marks.append((entry["id"], f"the real ticket id {entry['id']}, which the arm was never told"))
        reasons: list[str] = []
        messages = ""
        with contextlib.suppress(HelmError):
            messages = _git(repo, "log", "--format=%B", f"{base}..{tip}")
        if "cherry picked from commit" in messages:
            reasons.append("a candidate commit is a cherry-pick")
        for needle, what in marks:
            if needle in messages:
                reasons.append(f"a candidate commit message names {what}")
        with contextlib.suppress(HelmError):
            candidate = _git(repo, "diff", f"{base}...{tip}")
            if candidate.strip() and candidate == _git(repo, "diff", f"{base}...{merge}"):
                reasons.append("the candidate diff is identical to the shipped diff")
        for path in transcripts:
            try:
                text = Path(path).read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            for needle, what in marks:
                if needle in text:
                    reasons.append(f"the session transcript names {what}")
        return list(dict.fromkeys(reasons))

    # ---------- firstmate: delegation without the protocol ----------

    def start_firstmate(
        self, ticket: str, *, adapter: Any, model: str | None = None, restart: bool = False,
        effort: str | None = None,
    ) -> dict[str, Any]:
        entry = self.evaluation.ticket(ticket)
        project_id = self._setting("projects", "firstmate")
        if not project_id:
            raise HelmError("settings.projects.firstmate names no registered project")
        branch = self.prepare_base(ticket, "firstmate")
        task = self.coordinator.create_task(
            project_id,
            firstmate_brief(entry),
            ticket=entry["alias"],
            base=branch,
            agent="claude",
            model=model or self._setting("firstmate", "model") or None,
            effort=effort or self._setting("effort") or None,
            delivery_policy="local",
            # A measured run is a deliberate fresh line of work, whatever an
            # earlier run left in its worktree.
            new=True,
        )
        record = self.evaluation.new_run(
            ticket, "firstmate", status="running", task_ids=[task["id"]], project_id=project_id,
            restart=restart,
        )
        adapter.launch_task(task["id"], None, wait=False, agent="claude")
        return record

    # ---------- helm: the whole protocol ----------

    def start_helm(self, ticket: str, *, route: Any, restart: bool = False) -> dict[str, Any]:
        """`route` is the caller's routing function: root-only, so it is handed in."""
        entry = self.evaluation.ticket(ticket)
        project_id = self._setting("projects", "helm")
        if not project_id:
            raise HelmError("settings.projects.helm names no registered project")
        branch = self.prepare_base(ticket, "helm")
        record = self.evaluation.new_run(
            ticket, "helm", status="running", project_id=project_id, restart=restart
        )
        route(project_id, helm_request(entry, branch))
        return record

    # ---------- collecting ----------

    def _run_tasks(self, record: dict[str, Any]) -> list[dict[str, Any]]:
        """The tasks a run produced: recorded ids, plus any task for the ticket
        created in the run's project after the run started (the helm arm's
        foreman creates its own)."""
        data = self.coordinator.store.load()
        started = _stamp_to_epoch(record.get("started_at")) or 0.0
        wanted = set(record.get("task_ids") or [])
        names = {record["ticket"], record.get("alias")}
        found = []
        for task_id, task in data.get("tasks", {}).items():
            if task_id in wanted:
                found.append(task)
                continue
            if (
                task.get("project_id") == record.get("project_id")
                and task.get("role") == "worker"
                and not task.get("read_only")
                and task.get("ticket") in names
                and (_stamp_to_epoch(task.get("created_at")) or 0.0) >= started - 5
            ):
                found.append(task)
        return found

    def _reviews_of(self, task_ids: set[str]) -> list[dict[str, Any]]:
        """The review rounds a run's tasks received -- the ones that ran.

        A reviewer task that never launched (a refused runtime, a pane that
        did not start) stays `created` forever; it is not a round, and
        waiting for it to finish would wait forever.
        """
        data = self.coordinator.store.load()
        launched = {worker.get("task_id") for worker in data.get("workers", {}).values()}
        return [
            task for task in data.get("tasks", {}).values()
            if task.get("role") == "reviewer" and task.get("reviews") in task_ids
            and (task["id"] in launched or task.get("status") not in ("created", "allocated"))
        ]

    def _interventions(self, project_id: str, since: float, until: float) -> dict[str, int]:
        """Human decisions the run cost, by kind, from the records that prove them."""
        data = self.coordinator.store.load()
        counts = {kind: 0 for kind in INTERVENTION_KINDS}
        for message in data.get("messages", []):
            when = _stamp_to_epoch(message.get("created_at"))
            if when is None or when < since or when > until:
                continue
            if message.get("project_id") != project_id:
                continue
            kind = message.get("kind")
            if kind == "commander-ask":
                reason = (message.get("payload") or {}).get("reason")
                if reason in counts:
                    counts[reason] += 1
            elif kind == "approval":
                counts["authorization"] += 1
        for task in data.get("tasks", {}).values():
            if task.get("project_id") != project_id:
                continue
            for gate in (task.get("gates") or {}).values():
                if not isinstance(gate, dict):
                    continue
                when = _stamp_to_epoch(gate.get("confirmed_at"))
                if when is not None and since <= when <= until:
                    counts["authorization"] += 1
        return counts

    def _collect_delegated(self, record: dict[str, Any]) -> dict[str, Any]:
        tasks = self._run_tasks(record)
        if not tasks:
            return record
        if any(task.get("status") not in TERMINAL_TASK_STATUSES for task in tasks):
            return record
        task_ids = {task["id"] for task in tasks}
        reviews = self._reviews_of(task_ids)
        if any(review.get("status") not in TERMINAL_TASK_STATUSES for review in reviews):
            return record
        latest = max(tasks, key=lambda task: str(task.get("created_at") or ""))
        repo = self.repo_for(record["arm"])
        tip = None
        with contextlib.suppress(HelmError):
            tip = _git(repo, "rev-parse", latest["branch"]).strip()
        if _is_base(tip, record.get("base")):
            tip = None
        data = self.coordinator.store.load()
        ended = 0.0
        for message in data.get("messages", []):
            if message.get("task_id") in task_ids or message.get("task_id") in {r["id"] for r in reviews}:
                ended = max(ended, _stamp_to_epoch(message.get("created_at")) or 0.0)
        started = _stamp_to_epoch(record.get("started_at")) or time.time()
        ended = ended or time.time()
        catches = 0
        for review in reviews:
            for message in data.get("messages", []):
                if message.get("task_id") == review["id"] and message.get("kind") == "result":
                    if str(message.get("text") or "").lstrip().upper().startswith("CHANGES-REQUESTED"):
                        catches += 1
        usage_total = costs.sum_usage([
            entry for task in tasks for entry in self.coordinator.task_usage(task["id"])["workers"]
        ])
        metrics = {
            "wall_seconds": max(0.0, ended - started),
            "usage": {field: usage_total[field] for field in costs.USAGE_FIELDS},
            "turns": usage_total["turns"],
            "interventions": self._interventions(record["project_id"], started - 5, ended + 5),
            "review_rounds": len(reviews),
            "review_catches": catches,
            "questions": sum(
                1 for message in data.get("messages", [])
                if message.get("task_id") in task_ids and message.get("kind") == "question"
            ),
        }
        succeeded = latest.get("status") in {"completed", "approved", "merged", "pr-open", "pr-merged"}
        contamination: list[str] = []
        if tip:
            transcripts = [
                path
                for worker in data.get("workers", {}).values()
                if worker.get("task_id") in task_ids
                for path in costs.worker_transcripts(worker)
            ]
            contamination = self._contamination(record, repo, tip, transcripts)
            self.evaluation.write_diffs(record["ticket"], record["arm"], repo=repo, tip=tip)
        status = "completed" if succeeded and tip else "failed"
        return self.evaluation.finish_run(
            record["ticket"], record["arm"], status="contaminated" if contamination else status,
            tip=tip, task_ids=sorted(task_ids), review_task_ids=[r["id"] for r in reviews],
            task_status=latest.get("status"), metrics=metrics, contamination=contamination,
        )

    def collect(self, ticket: str, arm: str, *, adapter: Any = None) -> dict[str, Any] | None:
        record = self.evaluation.load_run(ticket, arm)
        if record is None or record.get("status") not in ("started", "running"):
            return record
        if arm == "single":
            collected = self._collect_single(record)
        else:
            collected = self._collect_delegated(record)
        if collected.get("status") in ("started", "running") and self._past_cap(collected):
            return self._time_out(collected, adapter=adapter)
        return collected

    def _time_out(self, record: dict[str, Any], *, adapter: Any) -> dict[str, Any]:
        """Stop a run that has outlived the cap, and record it as timed out.

        Whatever it committed is still its candidate; a run with no commit
        has none. The sessions are ended the way `helm worker stop` ends
        them, and for the helm arm the project's foreman goes too, because
        a foreman left running would launch the next round of the run that
        was just stopped.
        """
        cap = self.max_minutes()
        started = _stamp_to_epoch(record.get("started_at")) or time.time()
        tip = None
        usage = {field: 0 for field in costs.USAGE_FIELDS}
        turns = 0
        if record["arm"] == "single":
            _kill_run_process(record.get("pid"))
            worktree = Path(record.get("worktree") or "")
            with contextlib.suppress(HelmError):
                tip = _git(worktree, "rev-parse", "HEAD").strip()
            for path in costs.find_transcripts(worktree, since=started - 60):
                found = costs.transcript_usage(path)
                turns += found["turns"]
                for field in costs.USAGE_FIELDS:
                    usage[field] += found[field]
            interventions = {kind: 0 for kind in INTERVENTION_KINDS}
        else:
            data = self.coordinator.store.load()
            tasks = self._run_tasks(record)
            task_ids = {task["id"] for task in tasks}
            if record["arm"] == "helm":
                task_ids |= {
                    task_id for task_id, task in data.get("tasks", {}).items()
                    if task.get("project_id") == record.get("project_id") and task.get("role") == "foreman"
                }
            stop = adapter.stop_worker if adapter is not None else self.coordinator.stop_worker
            for worker in data.get("workers", {}).values():
                if worker.get("task_id") in task_ids and worker.get("status") == "running":
                    with contextlib.suppress(HelmError, OSError):
                        stop(worker["id"], f"evaluation: past the {cap:g}-minute cap")
            if tasks:
                latest = max(tasks, key=lambda task: str(task.get("created_at") or ""))
                with contextlib.suppress(HelmError):
                    tip = _git(self.repo_for(record["arm"]), "rev-parse", latest["branch"]).strip()
                total = costs.sum_usage([
                    entry for task in tasks for entry in self.coordinator.task_usage(task["id"])["workers"]
                ])
                usage = {field: total[field] for field in costs.USAGE_FIELDS}
                turns = total["turns"]
            interventions = self._interventions(record.get("project_id"), started - 5, time.time() + 5)
        if _is_base(tip, record.get("base")):
            tip = None
        metrics = {
            "wall_seconds": time.time() - started,
            "usage": usage,
            "turns": turns,
            "interventions": interventions,
            "review_catches": 0,
        }
        notes = list(record.get("notes") or [])
        notes.append({"at": _now(), "text": f"stopped at the {cap:g}-minute cap"})
        return self.evaluation.finish_run(
            record["ticket"], record["arm"], status="timed-out", tip=tip, timed_out=True,
            metrics=metrics, notes=notes,
        )

    # ---------- checks ----------

    def checkout_for(self, record: dict[str, Any]) -> Path | None:
        """Where the candidate can be checked: the arm's own checkout, while it exists."""
        if record["arm"] == "single":
            path = Path(record.get("worktree") or "")
            return path if path.is_dir() else None
        for task in self._run_tasks(record):
            path = Path(task.get("workspace") or "")
            if path.is_dir():
                return path
        return None

    def run_checks(self, ticket: str, arm: str, *, timeout: float = 1800.0) -> list[dict[str, Any]]:
        record = self.evaluation.load_run(ticket, arm)
        if record is None or not record.get("tip"):
            raise HelmError(f"{ticket} on {arm} has no candidate tip to check")
        commands = self._setting("checks") or []
        checkout = self.checkout_for(record)
        if checkout is None:
            raise HelmError(f"{ticket} on {arm}: the candidate's checkout is gone; checks need it")
        results = []
        log_dir = self.evaluation.run_dir(ticket, arm) / "checks"
        log_dir.mkdir(parents=True, exist_ok=True)
        for index, template in enumerate(commands):
            # A check usually wants to know where the change starts and ends
            # -- to run only the tests the diff touches -- so both are filled
            # in by name. Plain replacement, not str.format: shell braces stay.
            command = (
                str(template).replace("{base}", record["base"]).replace("{tip}", record["tip"])
            )
            started = time.monotonic()
            log = log_dir / f"check-{index}.log"
            try:
                with log.open("w", encoding="utf-8") as handle:
                    completed = subprocess.run(
                        ["sh", "-c", command], cwd=str(checkout), stdout=handle,
                        stderr=subprocess.STDOUT, timeout=timeout, check=False,
                    )
                code: int | None = completed.returncode
            except subprocess.TimeoutExpired:
                code = None
            results.append({
                "command": command, "ok": code == 0, "exit_code": code,
                "seconds": round(time.monotonic() - started, 1), "log": str(log),
            })
        record["checks"] = results
        self.evaluation.save_run(record)
        return results

    # ---------- judging ----------

    def start_judge(self, ticket: str, arm: str, *, adapter: Any, anyway: bool = False) -> dict[str, Any]:
        record = self.evaluation.load_run(ticket, arm)
        if record is None or not record.get("tip"):
            raise HelmError(f"{ticket} on {arm} has no candidate tip to judge")
        if record.get("contamination") and not anyway:
            raise HelmError(
                f"{ticket} on {arm} is contaminated -- {'; '.join(record['contamination'])} -- "
                "so its score would measure nothing. Run it again, or judge it anyway "
                "with --anyway if the evidence is wrong; the record keeps the finding either way."
            )
        project_id = self._setting("judge", "project") or self._setting("projects", "helm")
        if not project_id:
            raise HelmError("settings.judge.project (or projects.helm) names no project for the judge")
        paths = self.evaluation.write_diffs(ticket, arm, repo=self.repo_for(arm), tip=record["tip"])
        agent = self._setting("judge", "agent") or "cursor"
        model = self._setting("judge", "model") or None
        task = self.coordinator.create_task(
            project_id, self.evaluation.judge_brief(ticket, arm, paths),
            ticket=ticket, read_only=True, agent=agent, model=model,
        )
        record["judge_task_id"] = task["id"]
        record["judge"] = None
        self.evaluation.save_run(record)
        adapter.launch_task(task["id"], None, wait=False, agent=agent)
        return record

    def collect_judge(self, ticket: str, arm: str) -> dict[str, Any] | None:
        record = self.evaluation.load_run(ticket, arm)
        if record is None or not record.get("judge_task_id") or record.get("judge"):
            return record
        data = self.coordinator.store.load()
        task = data.get("tasks", {}).get(record["judge_task_id"])
        if task is None:
            return record
        result = next(
            (m for m in reversed(data.get("messages", []))
             if m.get("task_id") == task["id"] and m.get("kind") == "result"),
            None,
        )
        verdict = Evaluation.parse_verdict(str(result.get("text") or "")) if result else None
        source = "result"
        if verdict is None:
            # A judge that wrote its score in its own pane and never ran the
            # reporting command has still judged. The line asked for is a
            # JSON object with a digit score, which the brief's own template
            # (`"score": <0-3>`) can never match, so reading it from the pane
            # is safe where reading prose was not.
            verdict = self._verdict_from_pane(task["id"])
            source = "pane"
        if verdict is None:
            if task.get("status") not in TERMINAL_TASK_STATUSES:
                return record
            verdict = {"score": None, "unparsed": True, "task_status": task.get("status")}
        verdict["recovered_from"] = source
        record["judge"] = verdict
        record["judge_text"] = str(result.get("text") or "") if result else ""
        self.evaluation.save_run(record)
        return record

    def _verdict_from_pane(self, task_id: str) -> dict[str, Any] | None:
        data = self.coordinator.store.load()
        for worker in data.get("workers", {}).values():
            if worker.get("task_id") != task_id:
                continue
            try:
                text = Path(worker.get("log_file") or "").read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            clean = re.sub(r"\x1b\[[0-9;?]*[A-Za-z]", "", text)
            for match in re.finditer(r'\{"score":\s*\d.*?\}', clean, re.S):
                verdict = Evaluation.parse_verdict(match.group(0))
                if verdict is not None:
                    return verdict
        return None
