---
id: definition-of-done
applies_to: Agreeing what finished means.
use_when:
  - completion is being claimed or disputed
selectable: false
---
# Definition of done

What finished means, so it is not renegotiated per task.
Small by design: compose it with `{"extends": ["definition-of-done"]}`.

## Definition of done

Change open for review, verification evidence attached, implementation notes
linked, every traceability item verified, whatever label or checklist the
repository's own process requires applied, all review comments resolved
(automated and human), and CI green. For local delivery, read "CI green" as
the project's declared checks green at the tip.

---
