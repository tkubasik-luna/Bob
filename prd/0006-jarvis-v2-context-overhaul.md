# 0006 — Jarvis v2: Context & Orchestration Overhaul

## Problem Statement

Today Bob's Jarvis orchestrator works as an MVP but suffers from latency and predictability issues as conversations grow. From the user's perspective:

- Replies feel slower over time because every turn re-sends the full conversation history to the local LLM (LM Studio). After a long session, prompts become large enough that the model takes noticeably longer to start speaking.
- The user cannot trust that Jarvis will correctly route a follow-up message to an existing in-flight task (e.g. cancel it, enrich it with new info). Today task identity matching is fuzzy and there is no first-class way for the user to refine a task in progress.
- When Jarvis replies, the user must wait for the entire JSON response to be produced before any speech is heard. There is no progressive TTS.
- When a sub-task completes long after the user's original request, Jarvis sometimes delivers the result with no reminder of the original ask, making the result feel disconnected.
- Validation failures (malformed LLM JSON, unknown tool names, unknown task ids) occasionally surface as "Jarvis went weird" — silent corruption of conversation state with no clean degrade path.
- The user cannot reliably inspect what a running sub-task is "thinking" about; reflections are scattered across a debug view rather than a focused per-task overlay.

The user wants the assistant to feel fast, predictable, and robust regardless of how long the session has been running or how many tasks are juggled.

## Solution

Move Jarvis from MVP to an "overkill robust" architecture with the following user-facing changes:

- Jarvis always replies within a tightly bounded latency budget, regardless of how long the session has run. Conversation context is built fresh each turn from a small set of layered blocks rather than re-sending all history.
- The user hears Jarvis speak almost immediately — the first words start being synthesized while the LLM is still generating the rest of the answer (streaming TTS).
- The user can refine an existing task by simply talking to Jarvis ("ajoute X", "annule ça", "remplace par Y"). Jarvis explicitly picks the right task from a compact list of active tasks it always has in context.
- When a sub-task finishes, Jarvis paraphrases the result with a tone that matches recency: a short closing phrase if the user is still on topic, or a reminder phrase ("Tu m'avais demandé X, voilà…") if the user moved on. Multiple finishes in quick succession are batched into one utterance.
- Clicking a running task opens an overlay showing the AI's live reflections as they happen. Clicking a finished task opens its UI synthesis overlay (or an empty-state overlay if no UI was produced).
- The user never sees Jarvis output a malformed response: an unrecoverable validation failure surfaces as a polite clarification ("Désolé, peux-tu reformuler ?"), not a broken state.
- Conversation memory survives long sessions: older turns are sealed and summarised in the background, so context size stays roughly constant even after weeks of use.

## User Stories

1. As a Bob user, I want Jarvis to start speaking within a fraction of a second after I finish my message, so that the assistant feels fast and responsive.
2. As a Bob user, I want my long-running conversations to feel just as snappy on day 30 as on day 1, so that I am not punished for using Bob heavily.
3. As a Bob user, I want Jarvis to handle simple questions with an immediate spoken reply, so that trivial exchanges do not feel ceremonial.
4. As a Bob user, I want Jarvis to spawn a research sub-task when my request needs deeper work, and tell me it has done so, so that I know help is on the way.
5. As a Bob user, I want to give a follow-up instruction about a task that is still in progress, so that I can refine or cancel it without restarting.
6. As a Bob user, I want to cancel a task with a natural phrase ("annule ça"), so that I do not have to manage tasks through a UI.
7. As a Bob user, I want to replace a task's goal mid-flight, so that I can correct misunderstandings without losing the related conversational thread.
8. As a Bob user, I want Jarvis to tell me clearly when it cannot take on more tasks (queue full), so that I am not left wondering whether my request was accepted.
9. As a Bob user, I want sub-task results to arrive with a tone that matches whether I still have that topic top-of-mind, so that the assistant feels human, not robotic.
10. As a Bob user, I want multiple sub-task completions that happen close together to be summarised in one utterance, so that Jarvis does not interrupt itself repeatedly.
11. As a Bob user, I want a UI overlay that shows me what a running task is reasoning about right now, so that I can inspect the assistant's thinking.
12. As a Bob user, I want a UI overlay that shows me the final synthesis of a finished task (markdown content, etc.), so that I can review what was produced.
13. As a Bob user, I want clicking a finished task with no UI payload to show a clear empty state, so that I am not confused by a blank overlay.
14. As a Bob user, I want the assistant to never present me with a broken or malformed reply, so that I trust what it tells me.
15. As a Bob user, I want the assistant to politely ask me to reformulate when it fails twice in a row, so that I can move forward without seeing internal errors.
16. As a Bob user, I want my old conversation context to persist across long time gaps (cross-epoch memory), so that Jarvis still remembers high-level things we discussed weeks ago.
17. As a Bob user, I want the assistant's speech to be naturally chunked for TTS (sentence-by-sentence), so that audio output sounds smooth, not stuttering.
18. As a Bob user, I want a UI payload (Markdown overlay) to appear only after Jarvis has spoken the corresponding phrase, so that visual and spoken delivery feel coherent.
19. As a developer, I want every Jarvis turn to be auditable via a structured routing event, so that I can debug "why did Jarvis chat instead of spawning a task?" without grepping prose.
20. As a developer, I want tool schemas to be versioned, so that swapping the local LLM model does not silently break Jarvis's behavior.
21. As a developer, I want golden prompt snapshot tests in CI, so that regressions in the assembled prompt are caught at PR time.
22. As a developer, I want a single source of truth for telemetry events (debug + per-task overlay subscriptions), so that I do not maintain two drifting event channels.
23. As a developer, I want the addendum/replan/cancel flow to be implemented as distinct typed tools, so that I can grep, version, and evolve each intent separately.
24. As a developer, I want sub-agent execution to be wrapped in structured concurrency, so that crashing Jarvis does not leak background tasks.
25. As a developer, I want cancellation to be cooperative with a documented grace timeout and hard-kill fallback, so that "cancel" is not a foot-gun for future contributors.
26. As a developer, I want validation feedback to the LLM to use a dedicated sentinel role (not the tool role), so that bad LLM output cannot be re-injected as trusted content (prompt-injection safety).
27. As a developer, I want a per-tool retry policy table, so that I can tune retries differently for "say" vs "spawn_task" without touching code.
28. As a developer, I want sub-agent costs (tokens, latency) reported on completion, so that I can build adaptive caps and observability over time.
29. As a developer, I want a retrieval API stub over sealed epochs from day one, so that adding real RAG later does not require carving out a new read path.
30. As a developer, I want a documented and testable migration path from MVP to v2, staged in 9 incremental steps, so that the running app never breaks.
31. As a developer, I want to kill the existing `response_parser` raw-text fallback explicitly, so that silent assistant-history corruption is no longer possible.
32. As a Bob user, I want Jarvis to clearly say which task it is acting on when it acknowledges enrichment ("J'ajoute ça à ta recherche sur X"), so that I never doubt which task got the update.
33. As a Bob user, I want hard limits on concurrent tasks (3 running, 5 queued) so the system stays responsive even if I queue up many requests.
34. As a Bob user, I want the assistant to remember which task results have already been delivered, so that the same result is not announced twice.
35. As a developer, I want a deterministic recency rule (computed at turn-assembly time) so that "active" vs "stale" task phrasing is reproducible in tests, not flaky.

## Implementation Decisions

### Architecture overview

Replace the current monolithic Jarvis turn (full-history send → free-form JSON reply) with a layered, provider-based pipeline. Each Jarvis turn:

1. The orchestrator asks an **ContextAssembler** to build the prompt from registered **ContextProviders**, configured by a **ContextPolicy**.
2. The LLM is invoked in **streaming tool-call mode**. Every Jarvis emission is a tool call from a closed, versioned set: `say`, `spawn_task`, `addendum_task`, `replan_task`, `cancel_task`. There is no free-form structured-JSON path.
3. Streaming tool-call argument tokens are parsed with a **PartialJsonParser**. When the `speech` field of a `say` call accumulates, deltas are pushed to the client over WebSocket and to TTS in real time.
4. Once the tool call completes, the orchestrator dispatches it through a **ToolDispatcher** backed by a **ToolRegistry**.
5. Sub-agent tasks run as background asyncio coroutines under a single **TaskGroup** managed by `task_scheduler`. They emit semantic events through a unified **EventBus**.
6. Sub-agent completion produces a synthetic `task_completed` **ContextEntry** which the assembler picks up at the next assembly (recency computed at assembly time).

### Modules

**New packages:**

- **`context/`** — `ContextEntry` dataclass (`id`, `kind`, `source`, `token_estimate`, `pinned`, `created_at`, `provider_id`, `payload`, `schema_version`); `ContextPolicy` config object (token budgets, recent-turns window, eviction policy id, state cap, etc.); `ContextProvider` protocol; concrete providers: `SystemBlockProvider`, `StateBlockProvider`, `RollingSummaryProvider`, `RecentTurnsProvider`, `UserMessageProvider`; `ContextAssembler` (pure composition); `RecencyPolicy` struct (`active`/`stale` decision); `EvictionStrategy` injectable.
- **`epoch/`** — `EpochManager` (token-threshold seal, deterministic, no idle trigger); `Summariser` (versioned prompt, summarises from RAW sealed turns, never from prior digest, stores `summariser_version` per epoch); `RetrievalAPI` stub (returns empty list v1, logs call sites).
- **`tools/`** — `ToolRegistry` (versioned `ToolDefinition` entries); `ToolDispatcher`; tool implementations: `SayTool`, `SpawnTaskTool`, `AddendumTaskTool` (info-only, no restart), `ReplanTaskTool` (cancel + respawn with `lineage`), `CancelTaskTool`; tool input/output schemas Pydantic-validated.
- **`sub_agent/`** — `SubAgentRunner` (wrapped in `TaskGroup`, cancellation checkpoints at iteration + tool-call boundaries, 2 s grace then hard-kill); versioned `SubAgentAction` schema with `progress(thought)`, `tool_call(name, args)`, `done(result_summary, ui_payload?, status, reason_code, cost)`; `SubAgentPolicy` config (max iterations, wall-clock, token caps, per-task-type overrides); `AddendumQueue` (per-task `asyncio.Queue`, drained only at iteration boundaries); `SubAgentToolRegistry` (`web_search`, `web_fetch` v1; registry extensible).
- **`streaming/`** — `PartialJsonParser` (wrapper over `partial-json-parser` or equivalent battle-tested library, no hand-rolled tolerant parser); `StreamEmitter` (emits `speech_delta` WS frames per parser yield of `speech` value; emits `ui_payload` frame once on argument-object close).
- **`validation/`** — per-tool `RetryPolicy` table (`max_retries`, `degrade_action`, `accept_partial`); `ReasonCodeRegistry` (versioned, shared with frontend i18n); `system_validator` role usage (validation feedback never injected as `tool` role; bad LLM output escaped before re-injection — prompt-injection safety); transient `CallEnvelope` carrying retry counter in-memory (not persisted to `ContextEntry`); `on_validation_exhausted` handler interface per actor.
- **`events/`** — unified `DebugEvent` with new `task_id` field; single producer (collapse `ws_events.emit` + `emit_debug` into one); filtered `/ws/task/{task_id}` subscription over the same ring buffer; snapshot-then-tail protocol within a single WS session (first frame = snapshot, subsequent = deltas).

**Modified modules:**

- `orchestrator.py` — gut and re-shape: turn processing now composes `ContextProvider`s, calls LLM in streaming tool-call mode, dispatches through `ToolRegistry`. Hardcoded French phrasing templates moved to externalised, versioned prompt fragments. Existing `_reply_with_structured_response` path removed.
- `jarvis_store.py` — migrated to store `ContextEntry` rows. Stage 1.5 shim migration script.
- `response_parser.py` — raw-text fallback **explicitly removed**. Validation failures route through `on_validation_exhausted` only.
- `task_scheduler.py` — sub-agents launched under a single `TaskGroup`; addendum injection routed through `AddendumQueue`; concurrency cap (3 running) and queue cap (5 pending) enforced; overflow surfaces as tool error to Jarvis.
- `ws_router.py` — emits `speech_delta` and `ui_payload` frames; serves filtered `/ws/task/{id}` subscriptions; snapshot-then-tail on overlay open.
- `llm_client.py` — streaming tool-call arguments support; abstract `Tokenizer` interface (no LM Studio lock-in).
- Frontend — consumes `speech_delta` for progressive TTS + sphere text; subscribes to `/ws/task/{id}` when overlay opens; handles snapshot-then-tail; empty-state overlay when finished task has no `ui_payload`.

### Key contracts

**ContextEntry** (versioned dataclass):
- `id`, `kind` (`user_turn` | `assistant_turn` | `task_completed` | `system_note` | …), `source`, `token_estimate`, `pinned: bool`, `created_at`, `provider_id`, `payload: dict`, `schema_version`.

**STATE block entry** (per active task):
- `id`, `title` (≤8 words / char-capped), `state` (`spawned` | `running` | `awaiting_input` | `done` | `failed`), `last_update_1liner` (≤120 chars), `delivered_at_turn: int | None`, `last_event_id` (provenance), `lineage: list[task_id]`. Active set = (not-done) ∪ (done within last N user turns AND `delivered_at_turn` set), where N is from `StatePolicy`. `age_min` always recomputed, never persisted. Eviction order (configurable): delivered-done → failed → awaiting → never running. Hard caps: entry count + per-field chars; token budget asserted in tests.

**Tool surface (Jarvis-side)**:
- `say(speech: str, ui: object | null)`
- `spawn_task(title: str, goal: str)`
- `addendum_task(task_id: str, info: str)` (no restart)
- `replan_task(task_id: str, new_goal: str)` (cancel + respawn; new task carries `lineage = [old_id, …]`; old task marked `superseded`)
- `cancel_task(task_id: str)`

All tools have versioned schemas (e.g. `v1.spawn_task`) and Pydantic input validation.

**Sub-agent action surface**:
- `progress(thought: str)`
- `tool_call(name: str, args: object)`
- `done(result_summary: str, ui_payload: object | null, status: complete|degraded|failed|cancelled|timeout, reason_code: str, cost: object)`
- (`ask_user` deferred to v2.)

**Recency rule (deterministic, computed at turn-assembly)**:
- `active` if `task_id` referenced in last K user↔Jarvis turn pairs (K from `RecencyPolicy`, default 3), else `stale`. Phrasing is decided by the prompt from this signal, not by hardcoded strings in dispatcher code.

**Validation retry**:
- Streaming partial-JSON tolerates mid-stream truncation; final validate at EOS.
- Schema-invalid → retry budget from per-tool `RetryPolicy`; feedback message injected with `role=system_validator`, output escaped.
- Retry counter on transient `CallEnvelope`, not persisted.
- Exhaustion → `on_validation_exhausted` per actor: Jarvis emits hardcoded `say("Désolé, peux-tu reformuler ?")` + logs `jarvis.validation_failed`; sub-agent emits forced `done(status=failed, reason_code=invalid_output)`.

**Persistence / epoch**:
- Single logical thread (`thread_id` reserved in schema for future multi-thread).
- `epoch_id` on every `ContextEntry`.
- New epoch sealed when rolling summary token count exceeds threshold (token-budget trigger only — no idle trigger).
- Cross-epoch digest regenerated from RAW sealed turns when a new epoch seals, stamped with `summariser_version`.
- Sealed epochs queryable via `RetrievalAPI.recall(query)` (stub v1).

**Streaming**:
- Unified `say` tool: all replies are tool calls.
- Backend uses streaming tool-call argument parsing via battle-tested partial-JSON library.
- `speech_delta` frames flushed as `say.speech` accumulates; `ui_payload` frame emitted once on argument-object close.
- No feature flag — streaming is the only path post-migration.

**Concurrency**:
- 3 running / 5 queued caps (configurable via `SchedulerPolicy`). Overflow returns tool error to Jarvis → clarifying speech.
- FIFO; `priority` field reserved in schema, unused v1.
- All sub-agents under one `TaskGroup`. Cancellation cooperative + 2 s grace + hard-kill fallback. Documented in `task_scheduler` docstring.

**Events**:
- Single producer: `DebugEvent` with `task_id` field.
- `/ws/task/{id}` is a *filter* over the existing debug ring buffer, not a new topic.
- Snapshot-then-tail within one WS session; no HTTP-then-WS race.
- Retention bounded by bytes + age via `EventRetentionPolicy`.
- Cross-process restart: in-progress sub-agents die; reflection events ephemeral (ring buffer only); `task_completed` survives via existing `tasks`/`task_messages` tables.

**Result delivery**:
- Sub-agent `done` materialises a `task_completed` `ContextEntry` with `result_summary` + inline `ui_payload`.
- A debounced (~300 ms) pending-completions queue can batch multiple completions into one Jarvis turn ("Voilà tes 3 trucs").
- Jarvis prompt instructed to use `active`/`stale` recency signal to choose phrasing.

### Migration order

Ship in 9 incremental, independently rollback-safe stages. Golden prompt tests + a contract-test harness replay every stage. Feature flags only when a stage requires dual-running paths, removed within two sprints of stage completion.

1. **Foundation**: `ContextEntry` (with full field set: `source`, `token_estimate`, `pinned`, `created_at`, `provider_id`), `ContextProvider`, `ContextPolicy`. Legacy pass-through policy. Stage 1.5 shim migrates `JarvisStore` rows to `ContextEntry`.
2. **Tool registry pattern + `lineage` field on tasks.**
3. **Sub-agent contract rewrite**: versioned `SubAgentAction`, `SubAgentPolicy`, semantic events, `TaskGroup`, cancellation checkpoints.
4. **Bounded context**: `RecentTurnsProvider` + `RollingSummaryProvider`, externalise hardcoded French templates, `summariser_version` storage.
5. **Unified `say` tool**: drop free-form JSON path.
6. **Validation policy**: per-tool `RetryPolicy`, `system_validator` role, `ReasonCodeRegistry`, `accept_partial`. Kill `response_parser` raw-text fallback explicitly.
7. **Streaming partial-JSON** on tool args; `speech_delta` WS frames.
8. **Epoch + retrieval stub**: `EpochManager` token-threshold sealing, `recall()` stub.
9. **Event refactor**: unify `ws_events.emit` + `emit_debug`; `DebugEvent.task_id`; filtered `/ws/task/{id}` subscription; snapshot-then-tail.

## Testing Decisions

### What makes a good test

- Test external behavior, not implementation details. A test should describe what the user, the LLM, or a downstream subscriber observes — never internal call counts, internal state shape, or private method invocations.
- Pure deep modules (`ContextAssembler`, `EpochManager`, `PartialJsonParser`, `EvictionStrategy`, `RetryPolicy`, `Summariser`, `RecencyPolicy`) should be tested as functions: given inputs and policy, assert the output. No mocks, no fakes.
- Integration tests should run the orchestrator end-to-end against a deterministic fake LLM client that replays scripted streaming responses (including malformed/partial JSON for validation-path tests).
- Golden prompt tests (snapshot tests) lock in the assembled prompt under fixed inputs + policy. Failing one is a deliberate, reviewable signal that prompt structure changed.
- Behavior assertions over string assertions for validation tests: assert "retry occurred once, then degraded", not the exact error string text.
- Concurrency tests must inject the event loop / time / cancellation deterministically (no real `asyncio.sleep`).

### Modules to test

- **`ContextAssembler`** — given a list of `ContextEntry`s and a `ContextPolicy`, assert correct block composition, ordering, and total token budget.
- **`RecencyPolicy`** — given task references in recent turns + policy K, assert `active` vs `stale` decision.
- **`EvictionStrategy`** — given a STATE entry list at capacity, assert correct eviction order across all states (`delivered_done` first, then `failed`, etc.; `running` never evicted).
- **`EpochManager`** — given a running rolling summary token count + policy threshold, assert seal/no-seal decision; assert digest is regenerated from RAW sealed turns (never from prior digest).
- **`Summariser`** — given raw turns + summariser version, assert output is deterministic and version-stamped.
- **`PartialJsonParser`** — given streamed byte chunks (including UTF-8 split mid-codepoint, escaped quotes, nested objects), assert yielded events match expected sequence.
- **`StreamEmitter`** — given parser events, assert correct `speech_delta` and `ui_payload` WS frames are emitted in the right order.
- **`ToolRegistry` / `ToolDispatcher`** — assert unknown tool name and unknown `task_id` both route through `on_validation_exhausted` (no silent dispatch).
- **`SayTool`, `SpawnTaskTool`, `AddendumTaskTool`, `ReplanTaskTool`, `CancelTaskTool`** — Pydantic schema validation contract tests; assert `replan` produces `lineage` carry-over; assert `addendum` does not restart the sub-agent.
- **`RetryPolicy`** — given per-tool policy + a sequence of validation errors, assert retry/degrade decisions.
- **`SubAgentRunner`** — integration tests with scripted fake LLM: assert correct iteration cap, wall-clock cap, token cap behavior; assert addendum injection only occurs at iteration boundaries; assert cancel propagates within grace window; assert `done(status=…)` is correct for each termination cause.
- **`AddendumQueue`** — assert drain semantics at turn boundary only; assert ordering preserved.
- **`EventBus` / `DebugEvent`** — assert `task_id` field flows through; assert filtered `/ws/task/{id}` subscription receives only events for that `task_id`; assert snapshot-then-tail ordering.
- **`RetrievalAPI` stub** — contract test that callers handle empty results gracefully; smoke test that call sites are logged.
- **End-to-end orchestrator integration tests** — scripted multi-turn conversations covering: (a) simple `say` reply, (b) `spawn_task` then `task_completed` delivery (`active` recency), (c) `task_completed` delivery (`stale` recency with reminder), (d) `addendum_task` mid-flight, (e) `replan_task` mid-flight, (f) `cancel_task`, (g) queue-overflow degrade, (h) validation failure → clarifying speech, (i) batched completion within 300 ms debounce window.
- **Golden prompt snapshot tests** — fixed transcripts + fixed `ContextPolicy` produce a stable assembled prompt. Re-run on every PR.
- **Frontend** — overlay subscription snapshot-then-tail; empty-state when finished task has no `ui_payload`; `speech_delta` consumer feeds TTS without dropouts (smoke).

### Prior art in the codebase

- The existing `response_parser` retry-once-then-fallback path is the closest equivalent in spirit; v2 keeps the philosophy but kills the silent raw-text fallback.
- `task_scheduler.py` (564 lines) already has concurrency-cap mechanics that can be extended into the new `SchedulerPolicy`.
- `debug_log.py` already carries `task_id` ContextVar plumbing — reuse it as the basis for the unified `DebugEvent`.
- `ws_events` and `event_bus` show the pre-existing structured event shape — collapse into a single producer rather than introducing a third channel.
- Existing structured-output schema validation in `response_parser` informs the Pydantic validation pattern for tools.

## Out of Scope

- **`ask_user` action for sub-agents.** Deferred to v2. Sub-agents in this PRD are autonomous one-shot research workers. The full state machine (persistence, resume tokens, timeout-on-pause, Jarvis-side queueing) would balloon scope and is not required for "overkill robust v1".
- **Real RAG/retrieval implementation.** Only the `RetrievalAPI.recall()` stub ships in this PRD. Building the actual index, embeddings, and retrieval logic is a separate feature.
- **Multi-user / multi-conversation threads.** Schema reserves `thread_id` for future use; behavior is single-thread.
- **Sub-agents spawning sub-agents.** Not forbidden structurally (tool registry stays generic) but no `spawn_subtask` tool is shipped in this PRD.
- **Priority field on the scheduler.** Reserved in schema; not wired to behavior.
- **Pause/resume primitive for sub-agents.** Replan = cancel + respawn for v1; pause/resume is a future improvement.
- **Batch tool calls per Jarvis turn.** Single tool call per turn is the v1 constraint; documented loudly so future contributors do not assume otherwise.
- **LLM model swap to non-LM-Studio backends** (Claude API, vLLM, etc.). The `Tokenizer` interface is abstracted so this is *possible* but not validated in this PRD.
- **i18n.** French phrasing externalised but not multi-locale.
- **Authentication / multi-user concerns.** Bob is a single-user local assistant.

## Further Notes

- The "overkill robust" target means: the architecture is intentionally over-engineered relative to today's MVP load so the user does not hit a wall as the assistant is used more heavily. Bounded context + streaming + structured tool routing are the three properties most directly tied to perceived speed and predictability.
- Several "magic numbers" surface in this PRD (recent-turns window K, epoch token threshold, concurrency caps, debounce window, retention limits). Every one is centralised in a named `Policy` config object so tuning is one-stop.
- Prompt-injection safety: validation feedback to the LLM uses a dedicated `system_validator` role and the offending output is escaped before re-injection. This is non-negotiable — using the `tool` role here would let a misbehaving local model treat its own bad output as a user instruction.
- Two existing foot-guns explicitly killed in this PRD: (1) full-history send, (2) `response_parser` raw-text fallback. Both are silent corruption sources today.
- The migration order (`1 → 3 → 6 → 2 → 4 → 8 → 5 → 7 → 9`) was chosen so that contracts lock before plumbing, validation lands before streaming (which amplifies bad-JSON blast radius), and epoch/event refactors land last on stable ground.
- Sub-agent reflections are intentionally ring-buffer-only (not durably persisted). Process restart kills running sub-agents anyway; persisting their thoughts would be lying about state continuity.
- The `RetrievalAPI` stub is shipped intentionally even though it returns empty — without an active read path, sealed-epoch logic rots silently. Forcing the read path to exist from day one keeps the seal mechanism honest.
- All sub-agent tool calls (web_search, web_fetch v1) go through the same `ToolRegistry` pattern as Jarvis tools — one mental model, one versioning scheme.
- The `cost` field on `done()` (tokens, latency) enables later adaptive caps and observability; ships now to avoid a future migration.
- Existing shipped features (0001 MVP, 0002 Voice Mode, 0003 Jarvis Orchestrator, 0004 Sphere HUD, 0005 Debug View, 0006 Debug Grouped Tree) all remain functional through the staged migration; no user-visible regression is acceptable at any stage boundary.
