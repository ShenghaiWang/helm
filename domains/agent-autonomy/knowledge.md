---
id: agent-autonomy
applies_to: What an agent may settle alone, and keeping state outside the conversation.
use_when:
  - an agent must decide without asking
  - state must survive the session
not_for:
  - single-step tasks
selectable: false
---
# Agent autonomy and durable state

Where an agent may decide alone, and why its state must outlive its conversation.
Small by design: compose it with `{"extends": ["agent-autonomy"]}`.

## Autonomy has a boundary, and it sits after planning

"Keep going until done or blocked" applies from implementation onward. It does
**not** apply to understanding and planning, which stop and ask: a goal that is
ambiguous, underspecified, or self-contradictory is a question, and silence is
not an answer to it.

Once the goal is settled, the assigned task and its brief are the authority to
implement. Work inside the assigned worktree — editing, building, testing,
committing to the task branch — proceeds from that assignment and needs no
further approval. Waiting for one is not caution; it is how a delegated agent
stalls in a session nobody is reading, which looks identical to having died.

What still stops for a human is narrow, and it does not move: a protected
action (merge, push, publish, delete, any other destructive or external
action), a credential the agent does not have, and anything outside the scope
the brief describes. Silence is not approval for any of those.

## Keep durable state outside the conversation

Maintain a state file as the single source of truth rather than relying on
conversation memory, and re-read it whenever you are unsure what has already
happened. Track per piece: id, stage, branch, change number(s), verification
attempts so far, and what it is blocked on.

## Keep durable state outside the conversation

Maintain a state file as the single source of truth rather than relying on
conversation memory, and re-read it whenever you are unsure what has already
happened. Track per piece: id, stage, branch, change number(s), verification
attempts so far, and what it is blocked on.

## A refusing model looks exactly like a working one

An agent that cannot serve its configured model does not crash. It accepts the
launch, prints its banner, receives every instruction, and answers each one
with a line like *"There's an issue with the selected model (X). It may not
exist or you may not have access to it."* The session stays alive, so a
liveness probe reports it healthy and a silence check reports it **stalled** —
the same verdict a worker earns while thinking hard about a difficult file.

Measured in one day: two workers burned forty minutes each and a third
forty-five, all producing zero file changes, while the record said they were
running.

**The commonest cause is a runtime/model mismatch, and it is created by the
launch, not by the model.** Pinning `--agent claude` while the root's model
default is a cursor-only id asks Claude Code for a model it has never had.
Effort has the same shape: cursor cannot be told one, so a task carrying
`--effort high` refuses to launch on it at all. When a runtime is named
explicitly, everything else in that launch has to be made consistent with it.

**And the failure can impersonate a result.** A reviewer that never ran echoed
its own brief back with the model error appended; the brief contained the word
CHANGES-REQUESTED, and that was recorded as a verdict. A review that never
happened is more dangerous than no review, because work gets sent back to chase
findings that do not exist.

So: **treat a model-access error in any agent output as a FAILED LAUNCH,
whatever else the output contains** — before reading it as a status, a result,
or a verdict. And when a worker has been quiet, check its log for that string
before concluding it is thinking.
