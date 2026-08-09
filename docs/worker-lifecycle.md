# Worker and task lifecycle: the transition contract

This file is the normative specification for one high-risk state machine: how a
worker's protocol messages and its operating-system process exit combine into a
worker status, a task status, an approval hold and the diagnostics kept beside
them. It exists because those two sources of truth arrive independently and, in
the wrong order, used to settle the same task two different ways. It is not an
invitation to specify the rest of Helm this way.

The implementation is `Coordinator._transition_from_message`,
`Coordinator._ingest_worker_event` and `Coordinator._apply_process_exit` in
[`helm/core.py`](../helm/core.py). Those three are the only places worker or
task status moves in response to a worker.

## Two records, not one status

The outcome and the observation are stored separately on the worker, because
they are separate facts that can arrive in either order and can disagree:

- `protocol_outcome` -- `result`, `blocker` or `failure`, and only ever written
  by the worker's own protocol event. `outcome_source` says which record
  settled the worker: `protocol` or `process`.
- `exit_observed`, `process_exit_code`, `process_exited_at` -- what the
  operating system did, written only by exit observation. `process_settled`
  marks the narrower case where that observation, and nothing else, is what
  ended the worker: an asserted stop writes `{"returncode": null,
  "stopped": true}`, which is a decision Helm recorded rather than a return
  code it saw, and is deliberately not an observation.

The distinction is load-bearing rather than cosmetic. The process fallback
*synthesizes* `result` and `failure` messages so a task's history reads the
same either way, so "did the worker report a terminal outcome?" can never be
answered by scanning message kinds -- that would read Helm's own fallback back
as the worker's word. It is answered by `protocol_outcome` alone.

Those synthesized messages carry `"source": "process-fallback"` in their
payload, and they are the one legitimate difference the two orders leave
behind: exit-first keeps the fallback record of the exit it acted on before the
worker's word arrived, and superseding an outcome does not erase the history of
having held it. Convergence is over the worker record, the task, the hold and
the diagnostics -- not over a history that is meant to say what happened.

## The two event sources

**Protocol events** are what the worker says: `status`, `question`, `artifact`,
`approval-needed`, `result`, `blocker`, `failure`. They arrive either as a
direct `helm worker message` push or as a JSON line on the worker's stdout, and
both routes go through the one intake, `_ingest_worker_event`.

**Process exit** is what the operating system observed: the runner's exit
record (`exit.json`, written before the runner exits), or -- for a worker Helm
launched itself that has no exit record and no live pid -- the absence of the
process. It is read by `poll_worker` and applied by `_apply_process_exit`.

`result`, `blocker` and `failure` are the **terminal protocol outcomes**.
Everything else is nonterminal: `status`, `question` and `artifact` report
progress, and `approval-needed` *pauses* rather than ends -- the worker is still
sitting there, and settling it on that message would mark a live agent failed.

## The contract

1. **A terminal protocol outcome is authoritative for the task outcome.**
   `result` means the task completed, `blocker` means it is blocked, `failure`
   means it failed. The worker's word decides; the process merely hosts it, and
   an interactive agent that reports and then keeps its session open is
   finished as far as the task is concerned.

2. **A later process exit is evidence, not a verdict.** Once a terminal
   protocol outcome exists, exit observation records session liveness and the
   return code and changes nothing else. `blocker` then exit 0 stays blocked,
   `failure` then exit 0 stays failed, `result` then a nonzero exit stays
   completed. The worker's own status, `exit_code` and `ended_at` -- all set by
   the terminal message -- are likewise left alone; the return code lands in
   `process_exit_code` beside them.

3. **With no terminal protocol outcome, process exit is the fallback.** Exit 0
   completes the task (and records the `result` message that says explicit
   approval is still required before merge); a nonzero exit, or a runner that
   vanished without writing a completion record, fails it. This is the
   behaviour that keeps a silent or crashed agent from stranding a task, and it
   is unchanged.

4. **An approval hold is never completed by a process event, and never
   abandoned silently.** `approval-needed` is nonterminal: no exit turns it
   into success, and nothing resolves it as though the authorization had been
   used. What an exit *does* do is let go of a hold whose session has ended.
   That is deliberate and matches approval repair: a dead session cannot spend
   an authorization, and `helm approval release` refuses to hand one to a
   worker that cannot receive it. So the hold is explicitly abandoned with its
   reason recorded (an `approval-abandoned` message and a moved hold record)
   and the task becomes `failed` -- cleanable, retryable, its log still the
   evidence. A hold parked behind a dead session with no record of why would be
   residue neither release nor cleanup could touch. The abandonment is durable,
   visible, applied at most once, and identical whichever order the exit and
   the `approval-needed` push were observed in.

5. **Events are idempotent and order-independent.** Applying the same event set
   in either order converges on the same worker status, task status, hold
   state, retained diagnostics and completion behaviour. Exit observation runs
   at most once per worker (`exit_observed`), so repeated polls after a settled
   worker add nothing.

   Order-independence is not achieved by canonicalizing one code path. Two
   distinct permutations are supported explicitly:

   - **Protocol terminal event, then exit.** The exit is folded in as evidence
     per (2); the outcome does not move.
   - **Exit observed first, then the same terminal event delivered late.** A
     push does not lose a race with a poll under the state lock. When a worker
     was settled by *process observation only* (`process_settled` is set and
     `protocol_outcome` is unset), a late `result`, `blocker` or `failure`
     is accepted and becomes the authoritative outcome, overriding the fallback
     worker status, exit code and task status. The already-observed exit is
     retained -- `exit_observed`, `process_exit_code` and `process_exited_at`
     are never rewritten -- and a contradiction produces the same single
     `exit-evidence` diagnostic it would have produced in the other order.

   - **Exit observed first, then a late `approval-needed`.** `approval-needed`
     is nonterminal, but it is a *control* event carrying the durable reason
     the worker paused, and dropping it would lose that reason and diverge from
     the approval-first order. So it is accepted narrowly: the hold is recorded
     and opened, then immediately abandoned against the session that has
     already ended, with the same recorded reason and the same `failed` task
     state the approval-first-then-exit order produces. This preserves
     evidence; it does not reopen a dead worker, and the authorization is never
     answerable.

   A second late terminal push is a no-op: it appends no message, records no
   second outcome and emits no second diagnostic. So is a second late
   `approval-needed`. Ordinary nonterminal messages (`status`, `question`,
   `artifact`) after process settlement stay rejected -- there is no session
   left to progress or to ask. A worker settled any other way (an explicit `helm worker stop`,
   a foreman stand-down, `settle_reported_worker`) also rejects late pushes:
   those are decisions, not observations.

6. **Contradiction stays visible without changing the outcome.** A `result`
   followed by a nonzero exit, or a `blocker`/`failure` followed by exit 0, is
   real diagnostic information: the outcome stands per (2), and the
   disagreement is recorded exactly once as an `exit-evidence` message carrying
   both the outcome kind and the return code. The same holds for a worker that
   contradicts *itself* -- a `failure` after a `result` -- which is kept once as
   a `protocol-conflict` message: first word wins, and the second is evidence.
   A session that is gone with no return code at all is recorded too, as
   evidence with no verdict in it: nothing is concluded from an unknown code.

7. **Everything else is preserved.** The safe process fallback when Herdr is
   unavailable, Herdr pane and space lifecycle, explicit `helm worker stop` and
   foreman stand-down (which are commands, not observations, and settle
   deliberately), cleanup and resource release, approval immutability, and
   project isolation are untouched by this contract.

## Transition table

`P` is the worker's recorded `protocol_outcome`; `--` means no change.

| State before | Event | Worker | Task | Hold |
| --- | --- | --- | --- | --- |
| running, P none | `result` | completed, exit_code 0, P=result | completed | resolved on event |
| running, P none | `blocker` | failed, exit_code 1, P=blocker | blocked | abandoned |
| running, P none | `failure` | failed, exit_code 1, P=failure | failed | abandoned |
| running, P none | `approval-needed` | -- (running) | approval-needed | opened |
| running, P none | `status`/`question`/`artifact` | -- | per requested status only | may resolve |
| running, P none | exit 0 | completed, exit_code 0, source=process | completed (from created/allocated/running) | abandoned |
| running, P none | exit != 0, or no completion record | failed, exit_code 1, source=process | failed | abandoned |
| settled, P set | exit, code contradicts P | -- (exit recorded) | -- | -- (plus one `exit-evidence`) |
| settled, P set | exit, code agrees with P | -- (exit recorded) | -- | -- |
| settled by process, P none | late `result`/`blocker`/`failure` | per the message, P set, source=protocol; exit record kept | per the message, overriding the fallback | abandoned (plus `exit-evidence` on contradiction) |
| settled by process, P none | late `approval-needed` | -- | failed | opened, then abandoned with its reason |
| settled, P set | duplicate terminal push, or the same terminal line read twice | -- | -- | -- |
| settled, P set | a *different* terminal outcome | -- | -- | -- (plus one `protocol-conflict`) |
| settled by process, P set | second late `approval-needed` | -- | -- | -- |
| settled by process | late `status`/`question`/`artifact` | refused | -- | -- |
| settled any other way | any push | refused | -- | -- |
| any | exit already observed | -- | -- | -- |

A worker that is no longer running does not have its log drained again, so a
protocol line written after its terminal message is left in the log rather than
reopening a settled task; the late-delivery path above is the push route, which
is where the lock race actually is. `settle_reported_worker` is the same rule
reached from the other side: it settles a worker on a terminal message it
already delivered when the session is gone or idle, and it invents nothing when
none exists.

## Reopening a worker

A settled worker can be put back to work: the review loop keeps one reviewer
session across the rounds of one task, and approval repair revives a session
provider evidence says is still live. That starts a **new episode**, so
`begin_worker_episode` clears the whole episode record — `protocol_outcome`,
`outcome_source`, `process_settled`, `exit_observed`, the recorded return code
and the once-only diagnostic guards. Without it the second round's verdict
would be folded away as a duplicate of the first, and the next exit would be
ignored as already observed. Every path that returns a worker to `running`
calls it; nothing else may.

The corollary is that **message history is not an episode**. Round one's
`result` stays in the record forever, so any reader that decides liveness by
scanning for a terminal message would call round two finished before it had
said anything -- reporting it as `reported` in `helm watch`, or settling it on
the previous round's verdict. Those readers ask `episode_outcome` instead,
which answers from the persisted `protocol_outcome` (including when it is
None) and falls back to a scan only for a worker recorded before that field
existed. Readers that merely *report* history -- the last result text on a
task, a worker's evidence tail, the failures in a time window -- keep scanning,
because history is exactly what they are for.

## Waiting, and the one caller that waits for something else

`wait_worker` waits for the **assignment**, so it returns as soon as the worker
settles — including on the default `timeout=None`, and including when the
session is deliberately still open. `launch_worker(wait=True)` returns the same
way and detaches the runner exactly as an async launch does, rather than
blocking on the process the contract just said need not exit. The child is
reaped with `waitpid` only once the exit has actually been observed; otherwise
the reap is non-blocking, and `poll_worker` attempts one on every settlement so
a coordinator that never calls `wait_worker` does not accumulate zombies. That
non-blocking reap is also how a *finished* child is told from a live one at
all: `kill(pid, 0)` reports a zombie as alive, so an unreaped runner used to
read as running forever.

Exactly one caller needs the session itself gone: cleanup, which removes the
directory the session sits in. That has always had its own gate,
`_session_still_live`, and it stays the gate — waiting for the assignment is
not evidence about the process.

## Attention is reconciled, not assumed

The durable half of an event — the project's situation line and the commander's
action item — is written with the state lock released, so an
`approval-needed` request can be mid-flight when a poll abandons its hold. Two
different records then go wrong: the poll resolves an action item that does not
exist yet and the request creates it afterwards, or the poll writes
"abandoned" and the delayed request appends "Approval request" as the newest
word about a task that has already failed.

So the request re-reads its own hold once its effects have landed
(`_reconcile_hold_attention`): if the hold is no longer open, the item it just
created is resolved and the abandonment is restated as the latest line. It is
idempotent and order-free — whichever of the two runs last leaves the same
record, and a run where nothing raced does nothing. The exit-first sequential
path reaches the same end by replacing its own request situation with the
abandonment before its effects run.

## Persistence

All of it lives in the one locked state document, so it survives a reload: the
worker's `status`, `exit_code`, `ended_at`, `protocol_outcome`,
`outcome_source`, `process_settled`, `process_exit_code`, `process_exited_at`,
`exit_observed`, `late_hold_recorded` and the once-only guards
`exit_mismatch_recorded` and `protocol_conflict_recorded`; the task's `status`;
the hold records; and every message, including the `exit-evidence` and
`protocol-conflict` diagnostics. A fresh
coordinator reading that document reaches the same conclusions as the one that
wrote it, which is the point -- the conversation is not part of the state
machine.
