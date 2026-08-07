---
id: change-sizing
applies_to: Shaping a diff a reviewer can actually review.
use_when:
  - a change is growing too large to review
selectable: false
---
# Sizing a change for review

Shaping a diff so a reviewer can actually review it.
Small by design: compose it with `{"extends": ["change-sizing"]}`.

### Sizing a change for review

Check the diff before opening anything: `git diff <base>...<branch> --stat`.
Target roughly 500 changed lines. A larger piece splits into a sub-stack at
natural boundaries — data layer, then interface, then tests — each still leaving
the product working and buildable, chained exactly like the outer stack. One
piece may therefore become several small linked reviews, which is expected. If a
change genuinely cannot be split, say so rather than forcing an artificial split
that leaves the tree broken partway through.

Keep dependent test automation in the same change as the behaviour it covers,
following the team's documented conventions rather than inventing new ones.

Opening the change and driving it to merged is a separate topic: compose
`{"extends": ["pull-request-lifecycle"]}` for that.
