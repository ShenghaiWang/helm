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
**not** apply to understanding and planning, which stop and ask. Explicit
approval is required before implementation starts, and silence is not approval.

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
