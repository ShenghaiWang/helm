# Evaluating Helm

Helm's claim — that delegation with a protocol earns its cost — is a claim
about outcomes, so the repository carries a way to measure it rather than
argue it. `helm eval` replays closed tickets on three arms from the commit
the real fix started from and scores each against what shipped:

- **single**: one `claude -p` session in a worktree, handed the ticket, no
  Helm;
- **firstmate**: delegation without the protocol — one launched worker in an
  isolated worktree with the brief and knowledge, no foreman, no gates, no
  independent review (a project with `"foreman": false`);
- **helm**: the whole protocol, routed to a foreman.

```sh
helm eval add T-12 --base <sha> --merge <sha> --brief-file t12.md --title "..."
helm eval settings settings.json   # projects per arm, the single arm's repo, judge, checks, cap
helm eval run T-12 --arm single    # or firstmate, or helm; one arm at a time
helm eval status                   # collects runs whose sessions have finished
helm eval checks T-12 --arm helm   # the corpus check commands, in the candidate's checkout
helm eval judge T-12 --arm helm    # a read-only judge on an independent model
helm eval note T-12 --arm helm --text "laptop slept during the run"
helm eval report                   # ticket × arm, side by side
```

## A replay is closed-book, and the harness makes it so

The brief is the ticket's own description; the shipped change is never shown
to an arm. But a replayed ticket has already shipped, and the fix is one
lookup away for an agent that knows where to look — the tracker, the pull
request, another branch of the same repository. The first arm to find it
cherry-picked the real fix and reported it as its own. So the rule is stated,
and then enforced from outside the arm:

- the arm knows the ticket only by an opaque alias (`EVAL-3`) — in the brief,
  the branch names and its own checkout path — because the real id keys the
  tracker that holds the fix and its number keys a search of the pull
  requests;
- every run first strips the arm's repository — a clone the evaluation owns,
  with a local bare origin — to the history the ticket starts from: tags,
  other branches, remote-tracking refs and earlier runs' checkouts go, the
  primary branch is reset to the base, and the origin keeps only the base. A
  Helm task branch that reaches past the base is refused by name until its
  task is cleaned up. `helm eval sanitize <ticket> --arm <arm>` does this by
  hand and says what it removed;
- the brief states the rule in the same words on every arm, and the foreman
  is told to put it in the worker's brief;
- collection reads the candidate's commits, diff and transcript for the
  shipped commit, the pull request, the real id or a cherry-pick trailer. A
  run that shows any of those is recorded as `contaminated`, and `helm eval
  judge` refuses it unless told `--anyway`.

## What a run records

A run is collected from Helm's own records: the candidate tip, wall time,
tokens (`helm task cost`), human interventions **by kind** — `helm ask`
records, gate decisions, approvals — review rounds and the catches among
them. Every run has the same wall-clock cap (`settings.max_minutes`, 120 by
default): past it, the next `helm eval status` ends the run's sessions the
way `helm worker stop` does — a helm run's foreman included — and records it
as `timed-out`, with whatever it had committed as its candidate. The judge's
patches are written at collection, so they outlive the branch, and a
replaced run's record is kept aside rather than overwritten.

Check commands may name `{base}` and `{tip}`. The judge scores 0 (wrong) to 3
(better than shipped) with a rationale, as the first JSON line of its result;
the shipped diff is a reference for what the ticket needed, not an answer
key. Corpus, runs, verdicts and the report live in the ignored
`state/evaluation/`, because a ticket id is a fact about one root's projects.

Run arms one at a time. Two arms on one laptop compete for memory and
attention, and a run whose machine slept is a run whose wall time means
nothing — note it with `helm eval note` rather than letting the number stand.
