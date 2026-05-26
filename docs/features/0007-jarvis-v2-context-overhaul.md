# Jarvis v2 — Context & Orchestration Overhaul

Shipped on 2026-05-26 from PRD `prd/0006-jarvis-v2-context-overhaul.md`.

## What it does

Bob's Jarvis orchestrator moves from MVP to "overkill robust": replies start
streaming within a fraction of a second (progressive TTS), long sessions
stay snappy because context size plateaus rather than growing linearly, and
Jarvis is now task-aware — the user can talk to a running task ("ajoute X",
"annule ça", "remplace par Y") and Jarvis routes to it cleanly. Sub-task
completions arrive with recency-correct phrasing (closing vs reminder),
batched in 300 ms windows. Clicking a running task opens a live reflection
overlay; finished tasks render their `ui_payload` or a clear empty state.
Validation failures degrade as a polite "Désolé, peux-tu reformuler ?"
rather than corrupting history.

## Technical surface

- **New packages** — `bob.context`, `bob.context.providers`, `bob.tools`,
  `bob.sub_agent`, `bob.epoch`, `bob.validation`, `bob.streaming`.
- **New providers** — `SystemBlockProvider`, `UserMessageProvider`,
  `RecentTurnsProvider`, `RollingSummaryProvider`, `StateBlockProvider`,
  `CrossEpochDigestProvider`, `LegacyFullHistoryProvider` (safety net).
- **Jarvis tools** — `say`, `spawn_task`, `addendum_task`, `replan_task`,
  `cancel_task`. Each versioned (`v1.*`) with Pydantic args. Legacy
  `spawn_subtask` / `forward_to_subtask` / `cancel_subtask` kept as
  deprecated aliases.
- **Sub-agent runtime** — `SubAgentRunner` under a shared `asyncio.TaskGroup`
  with cooperative cancel + 2 s grace + hard-kill fallback. `AddendumQueue`
  per task, drained at iteration boundaries. `SubAgentToolRegistry` carries
  `web_search` + `web_fetch` placeholders.
- **Streaming** — `PartialJsonParser` wraps `partial-json-parser`;
  `StreamEmitter` flushes `speech_delta` per parser yield on `say.speech`
  and one `ui_payload` on argument-object close. Frontend pipes
  `speech_delta` into TTS and opens the overlay mid-stream.
- **Validation** — Per-tool `RetryPolicy` table, transient `CallEnvelope`
  retry counter (never persisted). Feedback re-injected under
  `system_validator` role with offending output escaped (prompt-injection
  safety). `accept_partial` rescues "required-valid + garbage optional".
  Exhaustion → hardcoded degrade `say` for Jarvis, forced
  `done(failed, invalid_output)` for sub-agents.
- **Epoch sealing** — `EpochManager` seals when rolling-summary token count
  exceeds threshold; cross-epoch digest regenerated from RAW sealed turns
  (never from prior digest). `RetrievalAPI.recall()` ships as a logging stub
  to keep the read path observable from day one.
- **Events** — `event_bus_v2.emit_event` is the single producer.
  `DebugEvent.task_id` populated from ContextVar. `/ws/task/{task_id}` is a
  filtered subscription with snapshot-then-tail in one WS session.
  `EventRetentionPolicy` bounds the ring buffer by bytes + age.
- **Migrations** — `0004` ContextEntry columns on `jarvis_messages`,
  `0005` `tasks.lineage`, `0006` `rolling_summaries`, `0007` `epoch_id`
  columns + `cross_epoch_digests` table, `0008` task-state literals
  (`spawned` / `awaiting_input` / `superseded`).
- **Frontend** — `TaskOverlay` (running timeline / finished markdown /
  empty state) wired via `useTaskEvents(taskId)`. `useSpeechDelta`
  consumes streaming frames into the progressive TTS pipeline.
  `frontend/src/generated/reason_codes.ts` is the i18n bridge for the
  versioned `ReasonCodeRegistry`.
- **Deleted** — `bob.response_parser` (and its test). The raw-text
  fallback that silently corrupted assistant history is gone.

## Notable decisions

- **Bounded context, no full-history send** — every turn assembled fresh
  from providers; long sessions stop slowing down.
- **All Jarvis emissions are tool calls** — single dispatch path,
  `jarvis.route` event logged on every turn including direct replies.
- **Versioned everything** — `ContextEntry.schema_version`, tool versions
  (`v1.say`), `summariser_version` per persisted rolling summary,
  `ReasonCodeRegistry.schema_version`. Model swaps + behavior tweaks
  shouldn't silently change semantics.
- **`system_validator` role, not `tool` role** — re-injecting validation
  feedback under the LLM's `tool` role would let a misbehaving model
  treat its own bad output as trusted user content. Non-negotiable.
- **Cross-epoch digest re-derived from RAW** — never from the prior
  digest. Bounded drift across multi-week sessions.
- **Single TaskGroup for sub-agents** — Jarvis crashes can't leak
  background coroutines. Cancel cooperative + grace + hard-kill is
  documented in `bob.sub_agent.runner` module docstring.
- **`/ws/task/{id}` is a filter** — not a new topic, not a new store.
  Reflections stay ring-buffer-only; process restart kills sub-agents
  and their thoughts together, since the runs themselves don't survive.
- **Streaming is the only post-merge path** — no feature flag. Short
  stabilisation window with rollback discipline replaces the flag.
- **`assistant_msg` frame kept** — for history replay, proactive
  single-shot pushes, and the degrade path. Streaming covers the live
  turn; `assistant_msg` covers everything else.

## Issues

- `issues/0043-context-entry-foundation.md` — Context entry foundation — commit `f31dbef`
- `issues/0044-tool-registry-versioned.md` — Tool registry versioned — commit `dbbb449`
- `issues/0046-bounded-context-providers.md` — Bounded context providers — commit `6738e91`
- `issues/0045-sub-agent-contract-rewrite.md` — Sub-agent contract rewrite — commit `f78a5ac`
- `issues/0047-unified-say-tool.md` — Unified say tool — commit `05023a0`
- `issues/0051-epoch-sealing-retrieval-stub.md` — Epoch sealing + retrieval stub — commit `05023a0`
- `issues/0052-event-refactor-task-overlay.md` — Event refactor + task overlay — commit `0b5514c`
- `issues/0048-validation-retry-policy.md` — Validation retry policy — commit `8621ef5`
- `issues/0050-state-block-task-lifecycle.md` — STATE block + task lifecycle — commit `8621ef5`
- `issues/0049-streaming-speech-delta.md` — Streaming `speech_delta` — commit `adcb3d5`
