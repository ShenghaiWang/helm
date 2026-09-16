# The task lead

**Status: shipped.** The lifetime change and the rename both landed. Everything
below is behaviour, not plan. What works today:

- A confirmed gate pair belongs to the **lead that proposed it**, not to the
  project, so two leads in one project hold independent pairs.
- `helm route --new` and `helm lead --new` appoint a task lead for a separate
  unit of work beside the project's existing one.
- Every lead carries a **name** — its tracker id, or a few words of the
  request it was appointed for — and every report addresses it by that name.
- A worker's report reaches the lead that **started** it, not whichever one
  the project lookup answers with.
- A request routed to one lead is not cleared by a different lead's reply.
- A request that **names** work — `route --ticket TICKET-42`, or a tracker id in
  the text — reaches the lead already doing it, rather than queueing behind
  whichever lead answers first or starting one that has never heard of it.
- The watchdog asks whether **each worker** has a live lead, not whether the
  project has one, and appoints one for orphaned work.
- The word is **task lead** everywhere a commander reads: `helm lead`, the
  appointment and routing lines, the Herdr tab, the attention list, the role
  document a lead is started with. `helm foreman` and `--no-foreman` keep
  working for one release.

Two decisions, recorded rather than pending:

- **`--new` stays opt-in.** Appointing a lead per request was the obvious next
  step and is the wrong default: a name now routes a follow-up to the lead
  already doing that work, which removes most of the queueing without paying
  for an agent per unrecognised message. Flipping it is a one-line change if
  the queue ever bites again.
- **Two spellings stay, on purpose.** The task record's `role` is still
  `"foreman"` and a project still declines one with `"foreman": false`. Both
  are values on existing records, so renaming them is a migration rather than a
  rename, and it buys nothing: no commander reads either.

## What changed

A project used to get one **foreman**: a long-lived agent that turned goals into
tasks, drove each one, and reported. It is now a **task lead** — one per unit of
work, created with it and ending with it — that owns a single deliverable end to
end.

```
Helm (coordinator)     gates, approvals, and the commander
  └─ task lead              one task, dies with it
       ├─ author worker     writes the change
       └─ reviewer          independent, different model
```

## Why

Three failures followed from one driver outliving the work it drove, and all
three were structural rather than bad luck.

**A confirmed gate pair binds to the driver's task row.** Replace that driver —
because it completed, failed, or was killed with its session — and the
commander's decision was silently discarded. When the lead *is* the work, the
binding cannot outlive or under-live its subject.

**One driver per project is one queue with one consumer.** Several independent
units of work behind a single driver serialise: proposals, answers and
escalations all funnel through one agent, and a driver blocked on any one of
them blocks the rest. In one observed day a project's driver spent twenty-two
minutes blocked with zero workers running while four independent units of work
waited.

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
turns and the coordinator's. The old project driver did not solve this either — its
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
  09:41   6m  w-z04ead74c431  paused on push
  09:38   9m  w-zc4a02d59f85  review round 2
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

## How much work may run at once

Helm should decide its own parallelism rather than leave it to whoever writes
the brief. One root ran several units of work concurrently, each verifying
locally at the same time, and took the machine out of memory: the session died,
every agent with it, and finished work sat uncommitted in worktrees.

**Measure the right thing.** The obvious model — cap the number of running
agents — is wrong, and measurably so. Sampled on a live root: **five agents,
0.8 GB resident between them, about 0.16 GB each.** At that price a machine can
hold dozens. What actually consumed the memory was what those agents *ran*: a
test runner spawning a worker per core, a compiler building a large native
crate, a type-checker over a monorepo. Each of those is gigabytes; the agent
holding its handle is rounding error.

So the unit to meter is the **heavy operation**, not the agent. A capacity model
built on agent count would have reported a healthy root minutes before it died.

**The shape:**

- **A named class of heavy operations** — full test suite, type-check, native
  build. A lead declares it is about to run one and waits for a slot.
- **Slots computed from headroom, not from a constant.** Available memory
  divided by a measured per-operation cost, floored at one and capped so a
  single heavy run can never take the last of the machine. Cost is *measured*
  from previous runs, not guessed: a root's own history is better evidence than
  any default shipped in the repository.
- **A floor that is never spent.** Below it Helm refuses to start another heavy
  operation and says what is holding the slots. Refusing is cheap — the work
  waits. Allowing the overrun is not: it kills every agent and risks uncommitted
  work.
- **Everything else stays parallel.** Editing, reading, resolving conflicts,
  drafting replies, waiting on CI — none of it is metered, because none of it
  costs anything measurable.

**Prefer not running it at all.** Where CI already runs the same check, pushing
and reading the result is both cheaper and better evidence than a local run: it
is the signal reviewers will see. A project may declare that in its own
knowledge, and then the heavy-operation slot is needed only for what CI cannot
answer — most usefully, proving a new test fails before its fix.

**Report it.** `helm capacity` (or a section of `helm doctor`) should print
total and available memory, load, the measured per-operation cost, how many
slots that yields, and what currently holds them. A limit nobody can see gets
worked around.

**A limit has to say whose operation it is.** The first version of this rule
banned whole-project operations "whatever the justification", and a repository
whose pre-commit hook runs a scopeless type-check made that rule forbid `git
commit` itself. Both leads stopped and asked rather than improvise, which was
right — but the only paths the rule left them were bypassing a safety gate or
leaving work uncommitted in a worktree, and uncommitted work in a worktree is
exactly what dies when the machine goes down. So the limit is about operations
*Helm chooses to run*. A repository's own gate is its price for committing, and
refusing it means refusing to work there. What to do when the machine is
starved is wait.

**And the operation that actually does it, measured.** A scopeless
`tsc --noEmit` in one task worktree reached **8.3 GB resident** and drove the
load average past 90 on a machine with 0.06 GB free. One agent, one command.
It finished, which was luck. This is the case for metering in one number: no
count of agents predicts it, because the agent that launched it was holding
about 0.2 GB at the time — the other 8.1 GB was the operation.

**Measured again, on a live root running three leads.** Eleven agent processes
held 2.6 GB between them while the machine showed zero free memory and a load
average of 13.9. Neither number came from Helm: two transcription processes the
commander was running held 2.3 GB and about eight cores. An agent-count model
would have reported a healthy root; a memory-headroom model would have refused
to start anything, correctly, and for a reason that had nothing to do with the
agents. Both facts belong in the same report — what Helm is running, and what
the machine has left after everything else on it.

## The bottleneck inventory

Observed in one day on one root, ordered by what each actually cost. The list
exists so "Helm should be faster" becomes something that can be worked through.

**Structural — these are what the task lead is for:**

| Bottleneck | What it cost |
| --- | --- |
| One driver per project is one queue with one consumer | a project blocked with zero workers running while independent work waited |
| Gates bind to a driver row that outlives or under-lives the work | commander decisions silently discarded on replacement |
| The coordinator as the serialization point | one confirmation round-trip per task, for tasks whose scope never changed |
| Context lost on every replacement | repeated full re-briefs, because nothing but the coordinator's own context held the state |

**Mechanical — each is independently fixable and none needs the redesign:**

| Bottleneck | What it cost |
| --- | --- |
| A task working on a branch it did not allocate cannot be launched, continued or rounded | several tasks bricked with their work intact on disk; two manual interventions to free them |
| Status reports an inference as fact — "authorized but undelivered", "the session is alive", "its output reports failure" | a verification round-trip per report, because the flag was wrong more often than right |
| A worker and its driver cannot see each other's approval requests | duplicate authorizations released for one action |
| A refusal exits zero | a caller gating on exit status proceeds as though the thing ran |
| The attention watch fires seconds after a message that is still queued | constant noise on the one channel the commander is told to trust |
| A session teardown fails every task it owned | reopens and branch repairs before any agent could touch the work again |

**The one that outlasts the others.** Once the plumbing is fixed, the binding
constraint is the *record*. Re-briefing happened because nothing else carried
what was going on. A record good enough for a fresh coordinator to take over
mid-stream makes a lead's death cost nothing; without it, every crash still
costs a re-brief however fast the machinery gets.

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
