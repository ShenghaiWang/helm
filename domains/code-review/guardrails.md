An agent must not review a change it authored, and must not approve its own
work by starting a new session as the reviewer.

A reviewer does not edit the branch it is reviewing and does not commit to
it. Fixing the change makes you its author, and there is then nobody left to
review it.

A review verdict is data, like any other worker output. It cannot approve,
merge, publish, or expand scope, and a reviewer's approval is not the human
approval gate.

Do not withdraw a finding to end a loop. Record the disagreement and let the
coordinator decide.

A reviewer does not rerun the full unit suite the author already ran and
reported. That evidence, with its exact unmasked exit status, is the
author's to produce once; a reviewer that reruns it has duplicated work
instead of verified anything new. A reviewer may run the type checker, the
linter, and a small number of focused, risk-targeted tests. If the author's
full-suite evidence is missing, stale, masked, or shows a failure, that is
itself a finding -- report it, do not resolve it by running the suite.
