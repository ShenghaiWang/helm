---
id: agent-messaging
applies_to: Delivering a message from one agent to another so it arrives.
use_when:
  - one agent must hand something to another
selectable: false
---
# Agent-to-agent messaging

Delivering a message between agents so it actually arrives and is acted on.
Small by design: compose it with `{"extends": ["agent-messaging"]}`.

## Messaging is explicit, and takes two commands

Reaching another agent's pane goes through the Herdr CLI. There is no shared
memory and no chat channel, and producing output does not mean another agent
saw it.

```sh
herdr pane send-text <PANE_ID> "<message>"
herdr pane send-keys <PANE_ID> Enter
```

**`send-text` alone only types into the input buffer — it does not submit.**
Enter must be a separate call. Forgetting it leaves the message unsent while the
sender believes it was delivered, which looks identical to the receiver ignoring
it.

This differs from `herdr pane run`, which executes a command in a pane. Use
`run` to print into a pane you own; use `send-text` + `send-keys` to say
something to an agent already living in one.

Each role is given the specific pane IDs it may reach, and does not guess at
others. A role that cannot reach another directly hands off through the
coordinator rather than inventing a channel.

## Notify the party who acts, and the party who tracks

When work changes stage, send two messages: one directly to whoever acts next,
and one to the coordinator so its state stays accurate. Routing everything
through the coordinator adds a hop and a failure point; routing around it
entirely leaves it tracking a stale picture. Concretely: the author tells the
reviewer directly that something is ready — including which branch or worktree
to look at — and tells the coordinator it has moved to "review".
