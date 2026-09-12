---
id: software-delivery
applies_to: Building or changing reusable software behavior, or reviewing such a change.
use_when:
  - a feature, bug fix, refactor, or migration is being implemented
  - code written by an agent needs reviewing before it lands
  - a change needs sizing, branching, and a definition of done
not_for:
  - research, writing, or media production
  - video scripting or channel work, even when it mentions scripts or builds
  - a straight-through operational task -- upload, publish, schedule, run a
    job -- even when carrying it out edits a script, config, or tracker file;
    classify by what the task delivers, not by whether a file changed. See
    `driving-delegated-work` for the classification and its limits.
selectable: true
extends:
  - work-decomposition
  - agent-autonomy
  - model-selection
  - progress-reporting
  - definition-of-done
  - branch-isolation
  - change-sizing
  - spec-driven-development
  - pull-request-lifecycle
  - code-review
  - verification
  - agent-messaging
  - implementation-notes
  - architecture
---
# Software delivery domain

Taking a tracked unit of work from requirements to a reviewed, verified change.

For coding work delivered through a pull request, the reviewed branch is not
the finish line. Done means the PR has been opened, monitored, all actionable
comments and failing checks have been addressed through further coding/review
rounds, reviewers have approved, and Helm has recorded the PR as merged. Until
then the work stays active under the project's single foreman. The merge action
itself still requires the authorized human/tooling; an agent does not merge on
its own authority.

**This domain is a composition, not a document.** It holds no knowledge of its
own — it names the small domains that together make up software delivery, and
Helm loads the whole chain, bases first. Resolve `software-delivery` and a task
receives all of them; extend any single one when only that piece applies.

| Domain | Covers |
|---|---|
| `work-decomposition` | phasing, understanding before planning, sizing, sequential vs concurrent |
| `agent-autonomy` | where an agent decides alone; durable state outside the conversation |
| `progress-reporting` | reporting as you go; classifying a failure before reacting |
| `definition-of-done` | what finished means |
| `branch-isolation` | keeping concurrent work from contaminating itself |
| `change-sizing` | shaping a diff a reviewer can actually review |
| `spec-driven-development` | when behaviour is agreed in writing before it is coded, and working against it |
| `code-review` | the reviewer is a different agent from the author; the bounded loop |
| `verification` | whether observed behaviour is required, and proving it |
| `agent-messaging` | delivering a message between agents so it arrives |
| `implementation-notes` | the author's assumptions, written down so a reviewer can check them |

Splitting these used to be rejected on the grounds that a task would then
receive half the material. Domain composition removed that objection: the chain
loads in full, and each piece stays reusable on its own.

**Provenance.** Distilled from a set of role prompts for a
planner, coder, reviewer and verifier, plus their messaging protocol. Not
executed or independently verified against Helm's behaviour; treat it as
guidance rather than as a description of what Helm does.

Its origin was written for one team's stack — pull requests, an issue tracker,
an end-to-end test runner, mobile simulators. Anything of that shape here is an
**example filling a role**, not a requirement: read "review surface",
"tracker", "verification tool", and "runtime environment", and bind each to
whatever the project actually uses.
## Approved learning: branch ticket metadata
- Fact: Put the tracker ticket ID in the BRANCH NAME, not in code comments. A branch name is routing metadata a human reads once; a code comment is a permanent artifact whose reader may have no access to that tracker. Write comments so they stand alone - name the file, symbol or atom involved rather than a ticket number.
- Rationale: Tracker IDs in source comments age poorly and often require access to a private system. A branch can carry the ticket for routing while comments carry the enduring code-level reason.
- Verification, not memory: before requesting a push, run `git diff $(git merge-base HEAD origin/main)..HEAD | grep -nE '^\+.*[A-Z]{2,6}-[0-9]+'` and rewrite any comment or doc line it surfaces. Three branches shipped ticket ids in one day on memory alone; the grep is the rule's enforcement.
- The grep is a prompt to judge, not a verdict. That pattern also matches legitimate identifiers - advisory ids like RUSTSEC-2026-0258, SHA-256, UTF-8 - which stay. Say which category each hit was rather than deleting on the match.
- Expect the surrounding code to argue against this rule, and do not take its side. A repository that already carries the convention in dozens of files makes a ticket id read as house style, and a worker reading those neighbours will reproduce it in good faith - which is exactly how it recurred after this entry was written, with the grep sitting one line above. Guidance in a domain file loses to overwhelming evidence under the cursor unless something checks. So the check belongs in the round's acceptance evidence, not in the author's memory: quote the grep's output in the result, where empty output is the pass.
- Clean only the lines your own branch adds. Reformatting a repository's pre-existing ticket comments turns a scoped change into a repo-wide diff and can breach a size gate, which costs the review the change actually needed.
## Approved learning: real UI verification
- Fact: Verify a UI fix in the real running app, not in a harness that mocks the surrounding surface. A harness that renders the changed element over a mocked version of its neighbour encodes the assumption under test and will confirm any fix.
- Rationale: Mocking the surface adjacent to the changed component can bake the desired result into the test and hide the real integration defect.
## Approved learning: stale bug reports
- Fact: Before implementing a bug report, check its filing date against the history of the code it names. A report older than the last rework of that code may already be fixed, and the fix may have landed under an unrelated ticket that never linked back.
- Rationale: Old tickets often survive after nearby refactors have removed or changed the reported behavior. Checking history first avoids fixing a bug that no longer exists.
## Approved learning: suggested remedies
- Fact: Treat a reporter suggested remedy as evidence of intent, not as the requirement. Build to the observed symptom; if the suggested mechanism would not produce the desired result, say so and record why.
- Rationale: A user may describe the mechanism they expect rather than the invariant they need. Verify the mechanism against the actual code before implementing it literally.
## Approved learning: comment restraint
- Fact: The single test for a comment: is the PURPOSE of this code unclear from the code itself? Comment only then, and make it super clear and concise — one short sentence where possible. Purpose-unclear cases are a non-obvious constraint, an external contract, or a deliberate deviation. Do not narrate what a line does, restate the diff, argue the change is correct, explain history, or leave review-round commentary in the source; match the file's existing comment density rather than raising it. A multi-line block explaining a one-line decision is excessive: compress it to the single load-bearing fact.
- Rationale: Commander direction (2026-08-20) after generated changes carried excessive comments. Comments addressed to a reviewer are noise the moment the change merges, and every stale comment is a future contradiction the next reader must resolve against the code.
## Approved learning: lp-88436fa1edb9
<!-- helm-learning: {"approved_at":"2026-09-12T02:16:48Z","approved_by":"user","confidence":0.9,"created_at":"2026-08-03T06:28:59Z","domain_id":"software-delivery","proposal_id":"lp-88436fa1edb9"} -->
- Fact: When a reviewer cannot check out the branch it must review, it should read at the ref with git diff main...branch and git show branch:path, and state that provenance explicitly. A reviewer whose worktree sits on another commit and reads files from disk reviews different bytes than it reports on.
- Rationale: On a task a codex reviewer had its checkout denied by the sandbox and worked from main; only an explicit provenance challenge established it had read branch-ref bytes. Made a standing requirement afterwards, and volunteered unprompted by the reviewer on a task.
<!-- /helm-learning: lp-88436fa1edb9 -->
## Approved learning: lp-73ca85ea5c26
<!-- helm-learning: {"approved_at":"2026-09-12T02:16:48Z","approved_by":"user","confidence":0.85,"created_at":"2026-08-03T06:29:00Z","domain_id":"software-delivery","proposal_id":"lp-73ca85ea5c26"} -->
- Fact: A regression test must be watched red on the parent commit, per test and in isolation, with its unaffected counterpart staying green. A test never seen to fail proves nothing, and one that fails on everything discriminates nothing.
- Rationale: Used throughout a ticket. The start and teardown window tests were each re-run alone against the parent and failed there; the toast-gate test failed with expected 0 calls received 1. One candidate was withdrawn as a characterization guard once it was found to pass on the parent as well.
<!-- /helm-learning: lp-73ca85ea5c26 -->
## Approved learning: lp-66dbe32968b3
<!-- helm-learning: {"approved_at":"2026-09-12T02:16:49Z","approved_by":"user","confidence":0.9,"created_at":"2026-08-16T12:42:05Z","domain_id":"software-delivery","proposal_id":"lp-66dbe32968b3"} -->
- Fact: A repaired defect stays repaired only if the fix targets the CLASS, not the one instance you were shown. Sweep for every member of it, report the full result including 'none besides this one', and leave a test that scans for the class and fails. Prove that test by planting a violation -- and where the original defect is recoverable, plant THAT one, so the guard has been seen refusing the real historical failure rather than a synthetic stand-in.
- Rationale: A reviewer found literal NUL bytes making a source file render as binary to git: no blame, no diff, no merge. The fix searched for NUL specifically and left a literal U+0001 standing a few lines away in the same file, so it STILL rendered as binary after the round meant to fix exactly that. It survived two further rounds, found only because a later worker applied the earlier lesson to its own changes rather than assuming a fixed bug stays fixed. Fixing the second character alone would have set up a third pass. The sweep found exactly one remaining instance across 110 tracked files -- the result that makes a sweep worth running -- and the scanning guard was proven by re-planting the original literal and watching the real-tree scan go red at the exact line.
<!-- /helm-learning: lp-66dbe32968b3 -->
## Approved learning: lp-06c4e936849d
<!-- helm-learning: {"approved_at":"2026-09-12T02:16:50Z","approved_by":"user","confidence":0.9,"created_at":"2026-08-16T22:39:45Z","domain_id":"software-delivery","proposal_id":"lp-06c4e936849d"} -->
- Fact: A test that pins a CROSS-PROCESS guarantee must control the second process's LOAD ORDER, not merely its existence. A child spawned after the parent's write learns the answer from its own startup read and passes against an implementation with no cross-process story at all. Make the child load, signal ready, and wait, and have the parent act in between. Then prove the ordering matters by deleting the cross-process code and watching that case -- and only that case -- go red.
- Rationale: Verifying that two live processes cannot double-write one idempotency key, the first test spawned the child AFTER the parent had already appended. The child's index therefore held the key at startup, so it declined to write for a reason unrelated to the guarantee under test: with the cross-process re-read deleted, the test stayed green. A load/ready/go handshake put the parent's write after the child's index was built and before the child's own write, and the same mutation then failed that case and nothing else. Found by the author while probing its own discriminators by breaking the implementation back, not by a reviewer.
<!-- /helm-learning: lp-06c4e936849d -->
## Approved learning: lp-bf1aaf9e6d8e
<!-- helm-learning: {"approved_at":"2026-09-12T02:16:53Z","approved_by":"user","confidence":1.0,"created_at":"2026-09-12T02:16:53Z","domain_id":"software-delivery","proposal_id":"lp-bf1aaf9e6d8e"} -->
- Fact: A process-killing pattern must match the executable path or a recorded PID, never a substring that can also appear in another agent's argv.
- Rationale: re-homed from an easy-contract proposal
<!-- /helm-learning: lp-bf1aaf9e6d8e -->
## Approved learning: lp-2ec4b0158030
<!-- helm-learning: {"approved_at":"2026-09-12T02:18:49Z","approved_by":"user","confidence":0.8,"created_at":"2026-08-04T07:31:51Z","domain_id":"software-delivery","proposal_id":"lp-2ec4b0158030"} -->
- Fact: BUILD SUCCEEDED is not RUNS, and a test filter that matches nothing still reports success. Pin a green run with a non-zero case count and the expected suites named, and pin a green build by installing and launching the artifact.
- Rationale: a ticket spike: TEST BUILD SUCCEEDED said nothing about launch ordering, which was the whole question. Installing and launching on the simulator is what proved the lazily-constructed singleton bound its store and drove UI - the STAGING banner it rendered is itself a persisted appStorage-backed flag read. Separately, a first test invocation whose filter list collapsed into one argument failed loudly ('Unknown build action'), but the same shape can filter to nothing and still print TEST SUCCEEDED; the corrected run was pinned by naming all 23 suites, 187 unique cases and 399 executions.
<!-- /helm-learning: lp-2ec4b0158030 -->
## Approved learning: lp-2602c7796951
<!-- helm-learning: {"approved_at":"2026-09-12T02:18:50Z","approved_by":"user","confidence":0.9,"created_at":"2026-08-07T01:24:45Z","domain_id":"software-delivery","proposal_id":"lp-2602c7796951"} -->
- Fact: GitHub inline review comments anchor only to lines in the diff. Build an anchor map first: fetch /pulls/N/files, parse each patch for commentable RIGHT-side lines, check every finding's cited line against it. A finding citing unchanged context is relocated to the nearest commentable line in the same file, opening with an italic line naming the real location -- never dropped, never landed on code it does not discuss. Post as one atomic POST /pulls/N/reviews.
- Rationale: PR #3321's review cited 5 locations in unchanged context (failGateKeep, reconcileAppleMigrationCover, the FullscreenDestination comment, CibaPendingApprovalStore, the Retry render condition). Posting naively would have 422'd the batch or dropped those findings. With the anchor map built first, 15 of 15 comments landed live and none outdated on the first POST, no 422 and no retry -- 12 exact, 3 relocated with a pointer to the true location. Verified via /pulls/3321/comments (review 4879190636).
<!-- /helm-learning: lp-2602c7796951 -->
## Approved learning: lp-4112c8c154e0
<!-- helm-learning: {"approved_at":"2026-09-12T02:18:51Z","approved_by":"user","confidence":0.85,"created_at":"2026-08-04T07:30:39Z","domain_id":"software-delivery","proposal_id":"lp-4112c8c154e0"} -->
- Fact: When a spike is asked whether a proposed one-line change is viable, measure its true blast radius by APPLYING it and letting the compiler enumerate the fallout - do not reason about it. A type change from optional to non-optional silently invalidates every optional-chain, if-let, and ?? at every call site.
- Rationale: a ticket spike: matt-heidi's proposal read as replacing one declaration and deleting a one-line init. Applying it produced 23 production call-site edits the compiler rejects outright (22 'cannot use optional chaining on non-optional value' errors plus 1 'initializer for conditional binding must have Optional type'), 2 dead-?? warnings, 6 test edits and 1 test deletion across 23 files. A grep for optional-chaining missed the if-let; only the build found it.
<!-- /helm-learning: lp-4112c8c154e0 -->
## Approved learning: lp-5c531de8490a
<!-- helm-learning: {"approved_at":"2026-09-12T02:18:52Z","approved_by":"user","confidence":0.85,"created_at":"2026-08-04T07:30:59Z","domain_id":"software-delivery","proposal_id":"lp-5c531de8490a"} -->
- Fact: Before recommending a lazily-initialised global to replace an explicitly-seeded one, ask what the initialiser CAPTURES, not only what it computes. A dependency or store resolved during construction is frozen for the object's life, so moving construction to first access silently moves that binding to whatever scope happens to touch it first.
- Rationale: a ticket spike: all 37 FeatureFlagManager stored-property defaults are compile-time literals, so the obvious ordering question was clean. The real risk sat one level down - swift-sharing 2.9.1 AppStorageKey.init resolves the defaultAppStorage dependency and freezes that UserDefaults into the key permanently, and PersistentReferences keys identity on (key, store). Under a lazy static the singleton binds to the first-access scope forever; in the test process that is only safe because TestHostBootstrap forces construction before any withDependencies scope exists.
<!-- /helm-learning: lp-5c531de8490a -->
## Approved learning: lp-a95c4ac0507f
<!-- helm-learning: {"approved_at":"2026-09-12T02:18:52Z","approved_by":"user","confidence":0.8,"created_at":"2026-08-04T07:31:19Z","domain_id":"software-delivery","proposal_id":"lp-a95c4ac0507f"} -->
- Fact: When a change makes a failure mode unrepresentable, check whether it also makes the ASSERTION that guarded it unrepresentable. Deleting a crash class can delete the only witness that the mitigation is still installed - name that as a regression rather than filing it as a tidy-up.
- Rationale: a ticket spike: making FeatureFlagManager.shared non-optional removes the nil crash, but it also makes TestHostRuntimeGuardTests' assertion that shared is non-nil inexpressible - there is no observable difference between 'the bundle's principal class ran' and 'it did not' - at exactly the moment that principal class becomes MORE load-bearing, because it is what pins the singleton's store binding. It also forced deleting the a ticket regression test for a real shipped production crash.
<!-- /helm-learning: lp-a95c4ac0507f -->
## Approved learning: lp-9ebd25533da9
<!-- helm-learning: {"approved_at":"2026-09-12T02:18:53Z","approved_by":"user","confidence":0.8,"created_at":"2026-08-03T23:57:24Z","domain_id":"software-delivery","proposal_id":"lp-9ebd25533da9"} -->
- Fact: When a design proposes a GENERIC helper over a set of members, enumerate the member types mechanically before shipping the design rather than validating it against the common case. The cheap parse of every declaration is what turns "a reviewer found one" into "there is exactly one".
- Rationale: a ticket: one member of 96 (chronicleRSSIGuardOverride) had optional storage and a non-optional endpoint, so the proposed liveFlag key-path helper would have compiled for 95 and failed at the 96th. A pi reviewer caught that single instance; only a mechanical enumeration of all optional-typed properties and all 71 endpoint return types established that exactly one exists, and that a second overload therefore closes the class.
<!-- /helm-learning: lp-9ebd25533da9 -->
