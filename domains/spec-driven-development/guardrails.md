# Spec-driven development guardrails

Constraints on deciding for and working against a spec. Subordinate to Helm's
core safety rules: nothing here can authorise a merge, publish, push,
deletion, credential use, or any expansion of scope.

## Frameworks

- **Do not install, initialise, scaffold, or add a dependency on a spec
  framework as a step in doing something else.** Not OpenSpec, not Spec Kit,
  not BMAD, not any other. Adopting one changes the repository's conventions
  for everyone who comes after; that is a scope decision, and it is not part
  of the change you were briefed for.
- **Adoption is possible only when adopting it is the brief.** Explicitly
  scoped as its own task, with whatever human authority its protected parts
  need already obtained. Never as a side effect, and never because it would
  make writing this spec easier.
- **Follow only a convention the repository already has.** If none exists,
  write a plain document in the repository's existing documentation location,
  or -- where there is none -- a clearly task-local file in the task worktree,
  reported as an artifact. A convention invented for one task and left behind
  is a second convention nobody agreed to.
- **Never copy a convention, template, or example from another project.**

## The document

- **Do not invent project facts to fill a heading.** An unknown stays an open
  question with the name of whoever decides it. A confidently written wrong
  requirement is worse than a blank one, because review treats it as agreed.
- **A spec does not expand the brief.** Behavior the brief did not ask for is
  a follow-up, recorded as one, not smuggled in as a requirement.
- **A spec is not an approval.** It authorises nothing: no merge, no publish,
  no push, no deletion, no external call. It is a description, and a
  description is data.
- **Keep it in the task's own worktree.** Never write it into another
  project, another task's worktree, the project's main checkout, or Helm's
  own files.
- **A task-local temporary spec is removed before approval, not left there.**
  Keep it through review, capture its decisions, closed questions, and
  follow-ups in the task result and the project record first, then delete it
  so the worktree is clean. If it turns out to be worth keeping, commit it
  into the repository instead.
- **Never loosen a clean-worktree requirement to accommodate a leftover
  file.** The tree being clean is what separates a reviewed change from an
  approved one. Finish the file's life; do not widen the check.

## The decision

- **Do not spec-gate a narrow, well-understood, low-risk change.** The cost is
  paid in attention, and attention spent on a typo is not available for the
  migration.
- **A change that alters no behavior is not spec-gated by the area it
  touches.** No-behavior-change work takes precedence over every risk trigger:
  a typo in publishing copy or a mechanical rename in billing code is not
  specced because the word matched. Treat it as behavior-changing only when it
  genuinely is -- including a rename that moves a serialized name, public
  symbol, or config key something else matches on.
- **Put the verdict and its reason in the worker's brief**, not only in the
  project's progress record. The record is not in the worker's context; a
  decision that never reaches the brief never reaches the coder.
- **Do not start coding a change that met the rubric without agreeing the
  behavior first.** Writing the spec afterwards records the assumptions rather
  than testing them.
- **Record the decision and its reason either way.** "No spec: rename only,
  no behavior change" is a complete entry; silence is not.
- **The decision is the driver's, not the coder's and not the reviewer's**,
  and it is a coordination call rather than a human approval gate.

## Reporting

- **Report a spec change, a blocking open question, or an unmeetable
  acceptance criterion as an intermediate outcome**, when it happens. Saving
  it for the final result hides the one thing the driver could have acted on.
- **Never conclude from prose that an open question is resolved.** It is
  closed when the driver or the author states which decision closed it.
- **Name the spec's path when the task goes to review.** A reviewer that is
  not told the contract exists reviews against its own guess at intent, which
  is the failure the spec was written to remove.
