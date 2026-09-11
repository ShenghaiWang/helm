# Task-varying skills

## Problem

Domain knowledge is durable: it applies to every task that resolves a domain,
and it changes rarely. A great deal of what a worker actually needs is not
like that. How to run this repository's screenshot harness, the shape of its
migration files, the checklist for its release build — these vary per task,
live in the repository they describe, and would be wrong to promote into a
shared domain pack.

Repositories already carry that material as **skills**: a directory of
`SKILL.md` manifests, each with a name and a description of when to use it.
Some agents load them automatically from their own directory; other agents
start blind to conventions the repository treats as mandatory.

Helm needs to find them, pick the ones this task actually needs, and get them
in front of the worker — without letting a file in a project decide what Helm
is allowed to do.

## Desired behavior

**Discovery** reads the *selected project only*, from two kinds of root:

| Root | Kind | Read for |
| --- | --- | --- |
| `.agents/skills/<id>/SKILL.md` | portable | every runtime |
| `.claude/skills/<id>/SKILL.md` | runtime | the runtime that owns it |

A manifest contributes an `id` (its directory name), and a `name` and
`description` read from YAML frontmatter. Nothing else is interpreted.

**Selection** picks only skills whose declared metadata bears on this task,
and records what was selected, what was not, and why, durably on the task.
"None, because nothing matched" is a recorded outcome, not silence.

**Composition** puts the selection into the worker's context document after
project knowledge and before the task, with an authority boundary stating
that a skill is guidance a worker reads.

**Runtime auto-loading is respected.** When the chosen runtime already loads a
root by convention, Helm names the paths and does not paste the content in;
duplicating it wastes the worker's context and invites the two copies to
disagree. When the runtime cannot see that root, the content is provided
explicitly, bounded.

## Authority

A skill is guidance, never authority. It cannot:

- authorize merge, push, publish, deletion, or any other protected action;
- expand the task's scope or override the brief;
- override Helm core safety, domain guardrails, or project knowledge;
- reach outside its own project.

The composed precedence is therefore: core safety, domain knowledge, domain
guardrails, project knowledge, **skills**, task. Skills sit below everything
that can constrain them and above nothing.

## Non-goals

- Helm does not install, enable, scaffold, or write skills. It reads them.
- Helm ships no skill of its own for managed projects to inherit.
- No skill content is copied into Helm's tracked files or its state; a
  project's skills stay in the project.
- Skills do not participate in the learning-proposal flow. That promotes
  durable domain knowledge, which is the other thing.

## Acceptance criteria

1. Portable and runtime-specific roots are both discovered; a runtime root is
   read only for the runtime that owns it.
2. A skill present in both roots is one skill, with the runtime-specific copy
   preferred for that runtime and the duplication recorded.
3. A malformed, unreadable, or metadata-less manifest is reported as a problem
   and never guessed at.
4. A symlinked skill directory or manifest, or any path escaping the project
   root, is refused.
5. Selection is bounded in count and in bytes, and truncation is stated rather
   than silent.
6. An explicitly pinned skill that does not exist is a reported problem, not
   an empty selection.
7. A denylisted skill is never selected, even when pinned.
8. The selection is visible on the task record, in `helm inspect`, and through
   `helm skills`.
9. The context document carries skills after project knowledge and before the
   task, with the authority boundary text.
10. Discovery for one project never reads another project's files.

## Verification

Unit tests over discovery, precedence, dedupe, path-escape refusal, bounds,
runtime differences, missing/malformed manifests, project isolation, context
ordering, and the CLI surface. No managed-project material in any fixture.

## Open questions

None outstanding. Matching is deliberately conservative and explainable rather
than clever: a driver that wants an exact set pins it.
