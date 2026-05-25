## Parent

`prd/0006-jarvis-v2-context-overhaul.md`

## What to build

Make Jarvis's per-turn context bounded by replacing full-history send with two new providers: `RecentTurnsProvider` (last K user↔Jarvis turn pairs verbatim, K from `ContextPolicy`) and `RollingSummaryProvider` (auto-generated summary of older turns). Add a `Summariser` module with a versioned prompt: when the rolling summary regenerates, it summarises from RAW older turns each time, NEVER from the prior digest, to bound drift. Every persisted rolling summary stores `summariser_version`.

Externalise the hardcoded French phrasing templates currently inline in `orchestrator.py` (around the personality + result-delivery framing) into a versioned prompt-fragments module. The static system block (personality, tool schema reminder) and user-message block stay simple providers.

Switch the active `ContextPolicy` from `legacy_full_history` to a new `bounded` policy that includes the system block, rolling summary, recent turns, and current user message. The STATE block is added in 0050 (this slice operates without one — Jarvis just sees the bounded prompt without per-task awareness).

User-visible payoff lands here: long sessions stop slowing down. A long-session smoke test must show prompt token count plateauing rather than growing linearly.

## Acceptance criteria

- [ ] `RecentTurnsProvider` returns the last K turn pairs verbatim; K is read from `ContextPolicy`.
- [ ] `RollingSummaryProvider` produces a summary block bounded in tokens, regenerated incrementally as the window slides.
- [ ] `Summariser` summarises from RAW older turns; verified by a test that runs N regenerations and asserts each regeneration is performed against original turns, not the previous summary.
- [ ] Every persisted rolling summary stores `summariser_version` and the `(from_turn, to_turn)` range.
- [ ] French phrasing templates moved out of `orchestrator.py` into a versioned prompt-fragments module.
- [ ] `bounded` `ContextPolicy` is the active policy in production code path.
- [ ] Long-session smoke test: synthetic 200-turn conversation shows assembled-prompt token count plateaus (within ±10%) past turn ~30.
- [ ] Golden prompt snapshots updated to the new bounded structure; before/after diff committed alongside.
- [ ] Pure-function tests for `RecentTurnsProvider`, `RollingSummaryProvider`, and `Summariser` (determinism + version stamping).

## Blocked by

`issues/0043-context-entry-foundation.md`
