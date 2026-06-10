# Latence orale & fiabilité conversationnelle

Shipped on 2026-06-10 from PRD `prd/0018-oral-latency-reliability.md`.

## What it does

Makes the voice turn fast, interruptible, and impossible to lose silently. Bob starts speaking sooner after the user stops (concurrent endpoint path, pipelined TTS, warm boot), shuts up in under 300 ms on barge-in (zero-grace hard cut), and every failure mode now produces a visible signal instead of unexplained silence (supervised background tasks, turn watchdog with verbal fallback, FSM force-reset). A per-turn latency breakdown with rolling P50/P95 aggregates measures it all in the Debug View.

## Technical surface

- `bob/turn_metrics.py` (new) — `TurnLatencyMetrics` collector: `mark`/`count` per turn id, `turn_metrics` debug event at turn end with stage durations + P50/P95 aggregates + draft adoption rate + retry counts. ContextVar-based correlation for say-path sites.
- `bob/voice_loop.py` — concurrent endpoint (`gather` of Thinker+Draft freeze ∥ STT finalize; say-path spawns at gate decision), zero-grace barge-in (`_hard_cut_say_path` + `hard_cancel` on both loops, fully synchronous before the first await), `note_thinker_complete` push-path bypassing the Thinker debounce, fire-and-forget supervised backchannel, `_force_reset` defensive FSM recovery, `voice_persist_failed` event.
- `bob/speech_pipeline.py` (new) — `SpeechStreamPipeline`: sentence N+1 synthesizes while N drains; bounded chunk queue (backpressure to queue, never to synthesizer); single idempotent `cancel()`; `audio_chunk_batch` windowed summaries replace per-chunk events.
- `bob/task_supervisor.py` (new) — `create_supervised_task`/`supervise`: done-callback reads result, logs with context, emits debug event. Adopted at TTS spawns, event-bus dispatch, proactive flusher, typing-reset, say-path, warmup, watchdog timer.
- `bob/turn_watchdog.py` (new) — TTFT + completion wall-clock budgets per turn (distinct voice values); expiry → `turn_timeout` event + FSM reset + short verbal/text fallback. Degrade-and-continue timeouts on summary regen, proactive synthesis, TTS preload/stream.
- `bob/boot_warmup.py` (new) — supervised background warmup (STT, TTS, per-role LM Studio models via `reconcile`) started after lifespan yield; `/health` gains `warmup_errors` + `degraded` status (also `mcp_startup_error` from 0124).
- `bob/event_bus_v2.py` — per-emitter `wait_for` timeout (`WS_EMITTER_TIMEOUT_SECONDS=1.5`) with immediate eviction + logging; `_HotEventBatcher` coalesces `speech_delta`/`reasoning_delta` per stream over `WS_HOT_EVENT_BATCH_WINDOW_MS=75` (cold events flush buffers, zero delay).
- `bob/debug_log.py` — JSONL sink writes only when installed, batched flush; retention uses size-cached-at-append (no re-serialization).
- `bob/sub_agent/runner.py` + `policy.py` — stall counter resets on error-code change; `stall_force_threshold=3`; per-run cached `select_tools` + tool-catalogue block.
- `bob/orchestrator.py` + runner prompt assembly — stable fragments first, variable (temporal context, validation feedback) last → byte-identical prefix across turns/iterations for LM Studio KV-cache.
- `bob/thinker_loop.py` / `speculative_draft.py` — `stop()` grace capped by `THINKER_CANCEL_GRACE_CAP_MS=250`; sync `hard_cancel()` for the barge-in path.
- New settings (all tunable, see `config.py`): `TURN_METRICS_*`, `THINKER_CANCEL_GRACE_CAP_MS`, `SPEECH_PIPELINE_QUEUE_MAX_CHUNKS`, `SPEECH_PIPELINE_BATCH_WINDOW_MS`, `WS_EMITTER_TIMEOUT_SECONDS`, `WS_HOT_EVENT_BATCH_WINDOW_MS`, `ORCHESTRATION_LOG_FLUSH_*`, `TURN_TTFT/COMPLETION_TIMEOUT_SECONDS` (+ `VOICE_` pair), `SUMMARY_REGEN/PROACTIVE_SYNTHESIS/TTS_PRELOAD/TTS_STREAM_TIMEOUT_SECONDS`, `stall_force_threshold`.

## Notable decisions

- Endpoint and barge-in have **distinct cancellation policies**: endpoint = cooperative stop with 250 ms grace cap; barge-in = synchronous hard cancel, never awaited. Don't re-unify them.
- The barge-in cut is correct even if cancelled tasks never unwind: stop flags latch before `Task.cancel`, and the pipeline `cancel()` guarantees no further chunk reaches the client.
- `TurnTimeoutError` deliberately does NOT subclass `TimeoutError` (keeps the legacy `LLM_TIMEOUT` error-frame path distinct); external cancellation through the watchdog is distinguished via `Task.cancelling()`.
- Metrics correlation uses a ContextVar bound only inside the say-path task — text turns and proactive TTS no-op for free; unknown turn ids are safe no-ops.
- Hot-event batching emits merged events **of the same wire type** (concatenated `delta`) — no frontend change; window=0 restores per-token emission.
- LM Studio role warmup failures (server down/OOM) are loud WARNs, not health `degraded` — an off LM Studio at boot must not stick the health endpoint.
- `voice_start` clears the session loop slot BEFORE stopping the old loop, stop under suppression — the double-loop race is structurally gone.
- Tests are external-behavior only (events, timing order, FSM state) per the PRD testing decision; `tests/_harness/virtual_clock.py` provides a virtual-time asyncio policy for zero-wall-clock timeout tests.

## Issues

- `issues/0117-turn-latency-metrics.md` — TurnLatencyMetrics — commit 65eb3b6
- `issues/0124-task-supervisor.md` — TaskSupervisor — commit 3c86333
- `issues/0120-endpoint-bit-immediate-backchannel-fnf.md` — immediate turn-complete + FnF backchannel — commit f771af3
- `issues/0122-ws-fanout-timeout-eviction.md` — WS fan-out timeout + eviction — commit 5800941
- `issues/0127-runner-stall-guard.md` — reactive runner stall guard — commit 9f6b239
- `issues/0128-stable-prompt-prefix.md` — stable prompt prefix — commit 8b60fb5
- `issues/0118-endpoint-concurrent-commit.md` — concurrent endpoint — commit 1c96e86
- `issues/0121-speech-stream-pipeline.md` — SpeechStreamPipeline — commit 7684f43
- `issues/0123-hot-event-batching-jsonl-gating.md` — hot-event batching + JSONL gating — commit 16a0145
- `issues/0126-turn-watchdog.md` — TurnWatchdog — commit bc6e65f
- `issues/0129-boot-warmup.md` — BootWarmup — commit cfa5934
- `issues/0119-bargein-zero-grace.md` — zero-grace barge-in — commit 7a27276
- `issues/0125-fsm-force-reset-voice-start-race.md` — FSM force-reset + voice_start race — commit e072222
