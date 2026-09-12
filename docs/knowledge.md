# Knowledge: domains, learning, skills, and when a change needs a spec

What a worker is told, where it comes from, and how it grows. Helm composes
one bounded context per task, learns from finished work through an explicit
two-step promotion, attaches a project's own skills, and ships the rubric for
deciding when behaviour should be agreed in writing first.

## Automatic bounded domain context

When Helm launches a worker it composes one private context document, in
this strict order:

1. immutable Helm core safety rules;
2. `domains/<domain-id>/knowledge.md`;
3. `domains/<domain-id>/guardrails.md`;
4. `projects/<project-id>/.helm/knowledge.md`;
5. the project's selected skills;
6. the current task and its assigned worktree.

The document carries exactly one project. Each source has a path, an
authority boundary, and an `exists` marker; a missing file is reported as a
missing source, never invented. Each layer can make a choice more specific
and none can make it wider: a domain file, a project file and a worker's own
message are data, and cannot authorize a protected action, widen a brief, or
grant a credential.

**The learned tail is bounded, and says so.** Approved learnings append to
the end of a knowledge file, newest last, and the file never shrinks by
itself. A worker's context carries the authored sections whole and the
newest learnings up to a budget; one line then says how many earlier
learnings were left out and where they remain, and the section records the
count as `omitted_learnings`. `helm doctor` warns when a domain's learned
tail is over the budget: the fix is to fold the older learnings into the
authored sections and delete their blocks, which is how learned knowledge
becomes the domain rather than a growing appendix to it.

**Domain knowledge attaches by itself.** The commander never names a domain,
and after the first task on a project neither does anything else: Helm records
the first domain actually resolved for a project as that project's default,
and every later task inherits it.

```sh
helm domain list                                     # every domain, with applies_to / use_when / not_for
helm project domain <project-id> software-delivery   # set or change the default
helm project domain <project-id>                     # clear it
```

The default lives in Helm's own state, which outranks the project's
`.helm/project.json`, so Helm never writes to the project's repository. An
explicit `--domain` on a task that differs is a one-off; it does not rewrite
the default.

What Helm will **not** do is guess. It never infers a domain from the words
in a brief — "script" once routed a video script to the software domain — and
it does not infer one from the shape of the repository, which would do the
same to a video project that happens to contain a Python file. The evidence
for a default is a judgement already made on that project by something that
read the task and the domain catalogue. One prior decision, reused. The
remaining escape hatch, `--no-domain`, ships a worker with core safety rules
only — no code review, verification, or definition of done — and teaches the
project nothing.

### Reusing knowledge across projects

A task resolves exactly one domain, so shared practice would otherwise have to
be restated in every domain that needs it. Instead a domain declares its
bases, and Helm loads the whole chain — bases first, the selected domain
last, so the most specific guidance is read last:

```sh
mkdir -p domains/backend
cat > domains/backend/domain.json <<'JSON'
{"extends": ["software-delivery"]}
JSON
```

Any task resolving `backend` now inherits `software-delivery`'s knowledge and
guardrails. Composition is depth-limited, rejects a cycle, and rejects a base
that does not exist, so a broken chain fails loudly instead of silently
dropping guidance. The composed context reports the resolved order as
`domain_chain`.

`domains/software-delivery/` ships with the repository as a general base:
lifecycle (requirements, sizing, traceability, circuit breakers, definition
of done), the author/reviewer/verifier roles, and multi-agent coordination.
The shared packs allowlisted in `.gitignore` are tracked; any other domain a
root grows stays local, so private or company-specific knowledge is never
committed by accident. A domain is small and about one topic; compose with
`extends` rather than growing one pack.

An optional pack is two files:

```sh
mkdir -p domains/publishing
cat > domains/publishing/knowledge.md <<'MD'
# Publishing domain
Keep recommendations accurate, audience-safe, and suitable for the requested format.
MD
cat > domains/publishing/guardrails.md <<'MD'
Do not invent analytics, claim unpublished facts, or publish anything without approval.
MD
```

Creating or changing a domain file is itself a scoped change with the same
review boundary as code.

## Learning proposals

This is how Helm gets better at a domain instead of rediscovering it. A
finished task leaves evidence — its result, artifacts, messages, and review
outcome — and that evidence becomes a candidate fact for the domain the task
resolved. Once approved and applied, the fact lives in
`domains/<domain-id>/knowledge.md` and loads into every future task that
resolves that domain, in any project:

```text
task completes → propose (with evidence) → approve → apply → attaches to
every later task in that domain, automatically
```

Learning is deliberately two-step. When a worker reports a `result`, Helm
attempts to create inert candidate proposals from the task's evidence; a
coordinator can also propose one explicitly. Worker output, a domain file, or
a proposal can never approve itself — knowledge that could approve itself
would let one confused worker teach every future one:

```sh
helm learning propose <task-id> --fact "Use captions for artifacts"
helm learning list --status proposed
helm learning inspect <proposal-id>
helm learning edit <proposal-id> --fact "Use captions on artifacts"
helm learning approve <proposal-id> --note "reviewed evidence"
helm learning apply <proposal-id>
helm learning reject <proposal-id> --note "not reusable"   # explicit, and kept as provenance
```

Helm infers a proposal's domain from the task when that mapping is
unambiguous; otherwise pass `--domain`, and a task's selected domain cannot be
replaced with an unrelated one. Proposals keep their source task, artifact,
message, review, confidence, and timestamps. Duplicate facts are reused
rather than duplicated; contradictory facts are surfaced for inspection
instead of silently replacing knowledge. Applying appends an `Approved
learning` block with its provenance to `knowledge.md` — only that file, never
`guardrails.md` — and core safety rules always outrank learned material.

## Helm learns from you, and from what recurs

Task-born proposals are one source of knowledge. Two more matter as much:

- **Your own rulings.** A rule you state is applied at once, in your words,
  with you as its provenance — to every project of a domain or to one
  project:

  ```sh
  helm learning teach "Run the full suite before reporting a result." --domain software-delivery
  helm learning teach "Tests here need the dev database up." --project api --note "learned the hard way"
  ```

- **What recurs.** A review finding made on two different tasks, or an
  answer the coordinator gave twice, is a rule nobody wrote down. `helm
  learning mine [--days N]` clusters review findings and answers by their
  content words across live and archived tasks, and proposes each recurring
  point with the evidence messages attached; a single result's prose is not
  mined. Run it weekly, or let the reflection prompt it.

Nothing is applied without a decision, and the decision has a moment:
`helm learning triage` lists what waits, one line each, and decides several
in one command (`--approve a,b --reject c [--scope project]`); `helm task
cleanup` names a task's waiting proposals; and `helm pending` counts
proposals that have waited more than a week, so they are seen where
everything else that needs you is seen. `helm learning stats` says how the
loop is doing — proposed, applied, rejected, stale, by origin — and the
ledger counts review findings that restate an applied fact as "knowledge
not followed", which is the evidence that a learning was worth having and
the brief that carried it was not read closely enough.

## Task-varying skills

Domain knowledge is durable and shared. A great deal of what a worker needs is
not like that — how this repository runs its screenshot harness, the shape of
its migrations, its release checklist. That material varies per task, belongs
to the repository it describes, and would be wrong to promote into a shared
domain pack.

Repositories already carry it as **skills**: `SKILL.md` manifests, each
declaring a name and a description of when it applies. Helm reads them from
the project it is working on — never from Helm itself, and never from another
project:

| Root | Read for |
| --- | --- |
| `.agents/skills/<id>/SKILL.md` | every runtime |
| `.claude/skills/<id>/SKILL.md` | the runtime that owns it |

```sh
helm skills <project-id>                              # what this project declares
helm skills <project-id> --agent claude               # including that runtime's own root
helm skills <project-id> --brief "add a migration"    # what a brief would select, and why
```

A skill earns its place when its own declared description overlaps the brief;
a driver that wants an exact set pins it in the project's own file, and a
denylist outranks a pin:

```jsonc
// projects/api/.helm/project.json
{"skills": {"pin": ["house-style"], "deny": ["legacy-deploy"]}}
```

What was selected, skipped, and unreadable is recorded on the task, so `helm
inspect` answers "what was this worker actually given". A skill the runtime
already loads from its own directory is named rather than pasted in; anything
the runtime cannot see is provided in full, trimmed to a stated limit. A
missing, malformed, symlinked, or description-less skill is reported, never
guessed at. Prefer a runtime that auto-loads the project's own skill
location; a runtime chosen for other reasons is told the exact `SKILL.md`
paths so it does not start blind.

A skill is guidance a worker reads, never authority it can invoke: it cannot
authorize a protected action, widen the brief, override core safety, or reach
outside its project. Helm ships no skills and never installs one. The full
contract, including non-goals, is in [skills.md](skills.md).

## Deciding when a change needs a spec first

Some changes should have their behaviour agreed in writing before anyone
codes them, and most should not. That judgement is knowledge, so it ships as
the `spec-driven-development` domain rather than as a Helm feature: **Helm
has no spec command, no spec state, and no spec gate**, and no task waits on a
human because of it. The domain is composed into the three places the
decision is acted on:

| Composed into | Reaches | What it does there |
| --- | --- | --- |
| `driving-delegated-work` | a project's foreman | decide at brief time, before a coder starts |
| `software-delivery` | the author of a change | write the document, implement against it |
| `code-review` | the independent reviewer | read the behaviour against the contract |

The rubric asks for a spec when the behaviour is ambiguous, when the change
alters a contract other components depend on, on auth and security
boundaries, where data loss is possible, for billing or publishing, for
user-facing workflows, when review keeps relitigating the same tradeoff, or
when the work already needs multiple rounds. It skips it for narrow,
well-understood, low-risk mechanical changes — spec-gating a typo trains
everyone to skim the spec that mattered. **No behaviour change outranks every
trigger**: a typo in publishing copy is not specced because the area matched
a keyword. The one reversal is doubt — a "rename" that moves a serialized
name, a public symbol, or a config key is a contract change, and gets the
spec.

The foreman decides and **writes the verdict, its one-line reason, and any
convention or path the coder needs into the task brief**, because a worker's
context is its brief plus composed knowledge, and a decision kept only in the
project's progress record never reaches the coder. It is a coordination call:
not a commander approval, and not a task status.

**The spec follows the managed project's conventions, never Helm's.** A
repository with a spec convention keeps it — OpenSpec, Spec Kit and BMAD are
named in the domain purely as examples to recognize, and Helm depends on
none of them. Where no convention exists, the worker writes a short plain
document in the repository's existing documentation location: problem,
desired behaviour, non-goals, acceptance criteria, verification, open
questions and action items, follow-ups. Installing or scaffolding a framework
is a scope decision and never a side effect; it stays possible when adopting
one is itself the brief.

A repository with nowhere obvious to put the document does not get one
invented for it. The worker writes a clearly task-local file in the task
worktree, reports it with `--type artifact --path`, and says the location is
temporary. Keep it through review — it is what the findings refer to — then
capture its decisions and follow-ups in the task result and the project
record, and delete it so the worktree is clean; a worktree must be clean to be
approved, untracked files included. A document worth keeping was never
temporary: commit it as part of the change.

The reviewer is told what the author produced, structurally: the reviewer
brief lists the author task's recorded artifact paths and descriptions, so a
document the repository does not track is still read against the change.
