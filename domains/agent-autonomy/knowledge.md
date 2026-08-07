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
