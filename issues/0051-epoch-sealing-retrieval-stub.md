## Parent

`prd/0006-jarvis-v2-context-overhaul.md`

## What to build

Make conversation memory survive arbitrarily long sessions by sealing the rolling summary into epochs and exposing a retrieval read path from day one.

Add `EpochManager` with a single deterministic trigger: seal when the rolling summary token count exceeds the threshold from `EpochPolicy`. No idle-gap trigger (would be clock-dependent and untestable). On seal, the current rolling summary is frozen + stamped with `summariser_version`, a new epoch starts, and a cross-epoch digest is regenerated from RAW sealed turns (NOT from prior digests — bounded drift).

Add `epoch_id` on every `ContextEntry`. Sealed epochs stay in SQLite, never auto-injected. Active context per turn = current epoch's recent turns + current epoch's rolling summary + cross-epoch digest.

Expose `RetrievalAPI.recall(query) -> list[ContextEntry]` as a stub returning an empty list in v1. Every call site logs a structured event so the read path is observable from day one — without an active read path, sealed-epoch logic rots silently. Real retrieval implementation is out of scope here.

`EpochPolicy` centralises: token threshold, summariser model id, summariser prompt version, max digest size.

## Acceptance criteria

- [ ] `EpochManager` seals when rolling summary tokens > threshold; no idle trigger.
- [ ] `epoch_id` column added to `ContextEntry`s; migration backfills existing rows to `epoch_id = 0`.
- [ ] Sealed epoch persists current rolling summary + `summariser_version` + `(from_turn, to_turn)` range.
- [ ] Cross-epoch digest regenerated from RAW sealed turns on every new seal — verified by a test running 3 sequential seals and asserting each digest is derived from raw, not the prior digest.
- [ ] `EpochPolicy` centralises threshold, summariser model id, prompt version, max digest size.
- [ ] Active context assembly includes cross-epoch digest within bounded size; sealed epochs are NOT auto-injected.
- [ ] `RetrievalAPI.recall(query)` exists, returns `[]`, logs structured `retrieval.recall_called` event with query metadata.
- [ ] Contract test: callers of `recall()` handle empty results without crashing.
- [ ] Long-session synthetic test: 500-turn conversation triggers ≥ 3 seals; assembled-prompt size stays bounded; cross-epoch digest stays under its cap.
- [ ] Golden prompt snapshots updated for cross-epoch-digest-aware prompts.

## Blocked by

`issues/0046-bounded-context-providers.md`
