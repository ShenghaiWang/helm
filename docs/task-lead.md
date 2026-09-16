# The task lead

**Status: proposed.** This is a written plan, not shipped behaviour. Nothing in
`helm/` implements it yet.

## What changes

Today a project gets one **foreman**: a long-lived agent that turns goals into
tasks, drives each one, and reports. This replaces it with a **task lead** — one
per task, created with the task and ending with it — that owns a single
deliverable end to end.

```
Helm (coordinator)     gates, approvals, and the commander
  └─ task lead              one task, dies with it
       ├─ author worker     writes the change
       └─ reviewer          independent, different model
```

## Why

Three failures follow from the foreman outliving the work it drives, and all
three are structural rather than bad luck.

**A confirmed gate pair binds to the foreman's task row.** Replace the foreman —
because it completed, failed, or was killed with its session — and the
commander's decision is silently discarded. When the lead *is* the task, the
binding cannot outlive or under-live its subject.

**One foreman is one queue with one consumer.** Several independent units of
work behind a single driver serialise: proposals, answers and escalations all
funnel through one agent, and a driver blocked on any one of them blocks the
rest. In one observed day a project's foreman spent twenty-two minutes blocked
with zero workers running while four independent units of work waited.

**A long-lived agent that does not live long is the worst case.** It is paid for
as continuity and does not deliver it: each replacement loses its context and
costs a full re-brief from the coordinator. Observed: six appointments for one
project in a single day.

A lead that ends when its task ends is *supposed* to end. Nothing is lost,
because there was never continuity to lose.

## What the task lead owns

Everything needed to deliver its one task:

- Turning the brief into work: deciding whether the behaviour needs agreement in
  writing first, and what the author needs to know.
- Spawning and driving the author worker; answering its questions from the task
  goal and the composed context rather than passing them upward.
- Running the review loop so the change is checked by an agent that is not its
  author, and sending rounds back until the reviewer is satisfied.
- Delivery: the commit, the push receipt, the reply to each review comment, the
  resolution of each thread, and the cleanup of what the task held.
- Reporting the outcome so it survives the agent.

It does *not* do the work itself. Review independence requires an author and a
reviewer that are not the same agent, and something has to hold the loop across
rounds; that is the lead's justification for existing at all.

## What stays with the coordinator

**The gates.** The lead proposes a requirement contract and a technical
solution; only the root decides them.

**Every protected action.** Push, merge, publish, delete and other
destructive or outward-facing actions are released by the root, against the
exact snapshot they were requested for. Owning delivery is not owning
authorisation — the two are deliberately separate, and the separation has
caught real damage: a push that looked routine and would have discarded a
reviewed commit on a branch the project did not own.

**The commander.** A task lead escalates to the coordinator, never to the
commander directly. Without that rule, every lead decides independently that its
own question deserves human attention, and the chain that protects the
commander's attention dissolves the moment there is more than one.

**The cross-task view.** Noticing that two units of work fail for one shared
reason, or that the cheapest one should go first, is judgement across tasks that
no single-task agent can see. It belongs to the coordinator reading the
project's status record. If the record cannot support it, that is a defect in
the record, not an argument for a long-lived agent.

## What this loses, stated rather than hidden

With no project-level agent, nothing watches a project between the commander's
turns and the coordinator's. The foreman did not really solve this either — its
reports sat in the status record until someone relayed them — but dropping the
layer makes the gap explicit. The plan does not close it. The honest answer is a
scheduled check that delivers outside the conversation, which is a separate
piece of work and should not be smuggled in here.

## Naming

**Task lead**, not task owner. "Owner" already means something in the
surrounding world — path ownership files that govern who must review a change,
and owner fields on feature-flag records — so "the owner escalated" reads
ambiguously. "Lead" describes the job: it leads a team of two.

### Every lead carries a readable name

A lead is addressed by a **name**, not only by its generated id.

- **When the task carries a tracker id, that is the name.** `--ticket TICKET-123`
  makes the lead `TICKET-123`. The tracker id is what a human already uses for
  this work, in the branch, in the pull request, in standup; the lead should not
  invent a second vocabulary for it.
- **When there is no tracker id, the lead gets a concise title** derived from
  the brief — a few words, kebab-case, naming the deliverable rather than the
  activity: `silent-mic-hard-stop`, not `fix-the-bug` or `round-3`.

**Why this is not cosmetic.** Every surface the commander reads is a list of
agents: the attention list, status, watch, the coordinator's own relays. Opaque
identifiers force the reader to resolve each one before the line means anything,
and a report of six such lines is unreadable in practice. A name makes the line
answer "what about X?" on its own:

```
  09:41   6m  TICKET-123    paused on push
  09:38   9m  TICKET-456    review round 2
```

instead of

```
  09:41   6m  w-904ead74c431  paused on push
  09:38   9m  w-2c4a02d59f85  review round 2
```

**The generated id remains the durable key.** Names are for addressing and
display; records, locks and references keep the id, so a rename or a collision
can never orphan state.

**Collisions resolve without inventing a second name.** Two leads on one tracker
id — a follow-up round, or two slices of one ticket — are distinguished by a
suffix (`TICKET-123`, `TICKET-123-2`), and the disambiguator is only added when
a second one exists. Names are unique within a project, not globally: two
projects may each have a lead named for their own tracker.

## Migration

The rename is not the interesting part but it is the bulk of the diff: roughly
seven hundred references in `helm/` and ten documents. Sequence:

1. Land the lifetime change first, keeping the existing name, so the behavioural
   change is reviewable on its own.
2. Rename in a second commit that changes no behaviour, so a reviewer can read
   it as a pure substitution.
3. Keep the old CLI verb as an alias for one release.

## Open questions

- **Cost.** One lead per task is more agents than one per project. Whether that
  is cheaper overall depends on how much re-briefing it removes; worth measuring
  on real tasks rather than asserting.
- **Where batching belongs.** A confirmed gate pair authorizes exactly one
  state-changing task. Under a per-task lead, later rounds on the same task are
  continuations and already exempt, which removes about half the confirmations
  without any new mechanism. What remains is one decision per task, and whether
  those should be confirmable together is a question best answered after this
  lands rather than designed against the model it replaces.
- **Depth stays at one.** Leads delegate; workers never do.
