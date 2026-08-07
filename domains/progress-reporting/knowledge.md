---
id: progress-reporting
applies_to: Reporting progress as work happens, and triaging failures.
use_when:
  - long-running work nobody is watching
  - a failure needs classifying before reacting
selectable: false
---
# Progress reporting and failure triage

Reporting as you go, and classifying a failure before reacting to it.
Small by design: compose it with `{"extends": ["progress-reporting"]}`.

## Report progress rather than expecting anyone to watch

Unattended work is invisible unless it reports. Post a short update at each
milestone — plan ready, each stage transition, done, and **every escalation
immediately**. One line each; the point is the timeline, not the prose.

**Going ~20 minutes with no update while work is supposedly in progress is
itself a signal.** Treat it as a prompt to check whether something is stuck, not
as evidence that things are fine.

## Diagnose failures by class

A failed verification is either a **coding bug** — fix, re-review, re-verify —
or a **plan gap**, in which case fix the plan first and re-issue it. Never let
implementation silently deviate from a plan that turned out wrong; correct the
plan so the record stays true. Treat a CI failure with the same priority as a
failed verification.
