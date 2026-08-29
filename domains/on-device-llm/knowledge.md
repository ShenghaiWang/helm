# On-device LLM (LiteRT-LM / Gemma-class models)

Hard-won, measured knowledge for building apps around a small on-device
language model. Everything here was verified on a real device or against the
runtime's own source; nothing is vendor-doc recall.

## The sampler is yours to set, never the file's

A `.litertlm` model file may carry no `sampler_params`. The engine then falls
back silently — typically to greedy decoding — and every reply looks
plausible while being unrepresentative of the model. **Always pass an
explicit SamplerConfig on every path (CPU and GPU alike).** Google's own
reference app does exactly this and never trusts the file. For Gemma-class
chat, their shipped default is topK 64 / topP 0.95 / temperature 1.0.

## Sampled for chat, greedy for extraction — by design

Free conversation takes the sampled config. Structured extraction and
tool-calling force topK 1 (greedy) for the duration of that call and restore
the sampled config on exit. This is not a compromise: determinism is a
feature where the output feeds a parser, and variety is a feature where a
human reads it. Make the split per-lane and explicit; never let both lanes
share one mutable sampler object.

## Constrained output is a pre-sampling grammar mask

ResponseFormat-style constrained decoding masks logits before sampling, so
malformed JSON is unrepresentable and the object gets a clean stop. Two
limits it does not lift: it cannot make the CONTENT right, and it does not
survive the token ceiling — generation cut mid-object is a valid-JSON
prefix, so truncation must be detected separately, and a truncation that
still parses must be counted as a truncation.

## The context ceiling is a combined budget, and its failure modes differ

Know the real ceiling for YOUR file and backend by measuring, not from the
model card (a card may say 32k while the deployed cache is 4096 combined
input+output). Over-budget prompts are REFUSED with an error; over-budget
output is silently truncated and delivered as an ordinary completed message.
Account for the two per-cause, and treat a per-call output cap as the limit
a long answer actually hits first.

## Audio callbacks never touch isolated state

CoreAudio/AVAudioEngine callbacks (taps, interruption and configuration
handlers) run on realtime audio queues. Swift's actor-isolation runtime
check crashes (`_dispatch_assert_queue_fail`) the moment such a callback
synchronously enters @MainActor- or actor-isolated code. Capture data in
the callback, hop executors (`Task { @MainActor in … }` or an explicit
queue send), and audit every closure the record path installs.

## Prose + provenance overlay: the generation surface for documents

A free-prose draft reads better than a structured form precisely because
prose lets the model paper over what the source never established — and a
faithful draft also reproduces the SPEAKER'S own errors (a stated total
that contradicts the stated rate). So generate documents as prose, and lay
an audit over it: every clause marked by provenance — agreed on tape /
gap / standard boilerplate / profile-filled — with entity-name mismatches
and arithmetic inconsistencies surfaced beside the text, never silently
resolved. The structured pass is the auditor, not the author. Gate "final"
on the gaps being seen by a human, not on the prose looking finished.

## Verify the runtime's claims at source

When behavior matters (sampler defaults, template/system-role support,
schema acceptance), read the shipped runtime source or probe on device —
render the template, parse the model container, run the grammar — rather
than trusting docs or a findings summary. Where a reference app and your
measurements disagree, your on-device measurement outranks their docs.

## The author's pre-submit sweep — the classes review keeps finding

Independent review of concurrent Swift app code around a local model found
the same defect classes on every large increment, one layer per round. An
author who sweeps for them BEFORE reporting turns four review rounds into
one or two. Before calling an increment done, check every instance of:

- **Replay-on-subscribe.** Every observation API (AsyncStream builders,
  snapshot feeds) must yield the CURRENT state to a new subscriber before
  streaming future events. A publish that fired before the first subscriber
  existed otherwise vanishes — the freshly-opened-view shape. Test it by
  subscribing AFTER the terminal event, never only before.
- **Subscribe-before-start.** Notification and event subscriptions register
  synchronously BEFORE the machinery they observe starts; a Task spawned
  after start() races the first event.
- **Callback ordering across hops.** Forwarding delegate callbacks in
  independent Tasks reorders them; completion can outrun the last progress.
  Preserve order at the boundary (one serial hop, not N racing ones).
- **Executor hops at framework callbacks.** Audio, URLSession and other
  framework callbacks never touch actor-isolated state synchronously; a
  closure literal formed in an isolated context silently inherits that
  isolation and traps at runtime on the framework's queue.
- **Every failure path reaches the user.** A retry that exhausts, a queue
  that gives up, a background completion nobody observed — each needs a
  visible state and an affordance, not only a log line. The generic loading
  copy hiding a recorded failure is a defect, not a cosmetic.
- **Fresh exact-tip evidence.** The suite payload names the tip it ran at
  and its unmasked exit; evidence for the previous tip is absence, and the
  round spent arguing it is the most expensive no-op in the loop.
