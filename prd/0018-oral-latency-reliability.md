# 0018 — Latence orale & fiabilité conversationnelle

> Source : audit complet `docs/investigations/2026-06-09-perf-reliability-audit.md` (48 findings, 5 axes). Ce PRD implémente le top 10 ROI de la synthèse, regroupé en 8 modules.

## Problem Statement

Bob est un assistant **oral-first**, mais la conversation ne tient pas le rythme d'un échange parlé. Entre la fin de ma phrase et le premier son de Bob, j'attends parfois plusieurs secondes — un empilement d'attentes séquentielles invisibles (finalisation STT, fenêtres de grâce des boucles d'anticipation, re-prefill complet du prompt, synthèse vocale phrase par phrase). Quand je veux couper Bob (barge-in), il met jusqu'à 2 secondes à se taire alors qu'une interruption naturelle doit être quasi instantanée.

Pire que la lenteur : les pannes silencieuses. Une synthèse TTS qui échoue produit un « audio fantôme » — Bob croit avoir parlé, je n'entends rien, personne n'est notifié. Une fenêtre HUD au réseau dégradé peut geler l'application entière. Un turn dont le LLM ne répond jamais reste bloqué pour toujours sans aucun signal. Une machine à états vocale qui prend une exception au mauvais moment reste coincée en « Bob parle » et casse tous les turns suivants. En tant qu'utilisateur, je ne sais jamais si Bob réfléchit, a planté, ou m'a oublié — et à l'oral, le silence inexpliqué est la pire expérience possible.

Enfin, rien n'est mesuré : il n'existe aucune décomposition du temps « fin de parole → premier audio », donc impossible de savoir où part le temps ni de prouver qu'une optimisation a marché.

## Solution

Rendre le tour de parole rapide, interruptible et impossible à perdre silencieusement :

1. **Mesurer d'abord** : chaque turn vocal produit une décomposition chronométrée de son chemin critique (finalize STT, gel des boucles d'anticipation, gate du draft, premier token LLM, premier chunk audio), plus les ratios de santé (taux d'adoption du draft spéculatif, distribution des retries de validation). Visible en debug, agrégeable, et utilisé pour valider chaque gain de ce PRD.
2. **Paralléliser l'endpoint** : à la fin de la parole utilisateur, tout ce qui était séquentiel devient concurrent — l'annulation des boucles Thinker/Draft (avec grâce plafonnée à 250 ms), la finalisation STT, et le lancement du chemin de réponse. Le barge-in obtient un chemin dédié sans aucune fenêtre de grâce : Bob se tait en moins de 300 ms.
3. **Pipeliner la voix de Bob** : la phrase suivante entre en synthèse pendant que la précédente est encore en cours de diffusion ; les chunks audio sont bufferisés entre la synthèse et l'envoi réseau pour qu'un client lent ne ralentisse jamais la production.
4. **Blinder la diffusion d'événements** : un client WebSocket lent ou mort est évincé après timeout au lieu de geler l'orchestrateur ; les événements à haute fréquence du chemin chaud sont regroupés ; le journal debug n'écrit plus sur disque quand personne n'écoute.
5. **Aucune panne silencieuse** : toute tâche asynchrone critique est supervisée (erreur lue, loggée, signalée au client) ; la machine à états vocale se remet toujours en état sain après une exception ; un watchdog par turn garantit qu'un turn qui ne répond pas produit un signal et un fallback verbal au lieu d'un silence éternel.
6. **Démarrage chaud** : les moteurs STT, TTS et les clients LLM par rôle sont préchauffés en arrière-plan dès le boot, sans bloquer les connexions — le premier turn vocal est aussi rapide que les suivants.
7. **Runner réactif** : le sous-agent détecte ses propres impasses plus vite (un outil qui échoue en boucle est coupé plus tôt) et arrête de gaspiller des tokens en re-publiant son catalogue d'outils à chaque itération.

## User Stories

1. As a voice user, I want Bob to start speaking within ~1 second of my sentence ending, so that the conversation feels like talking to a person.
2. As a voice user, I want Bob to stop speaking in under 300 ms when I interrupt him, so that barge-in feels natural instead of talking over each other.
3. As a voice user, I want the speech of a multi-sentence reply to flow without gaps between sentences, so that Bob doesn't sound like he's pausing to think mid-reply.
4. As a voice user, I want Bob's very first voice turn after app launch to be as fast as later ones, so that I don't pay a cold-start penalty every morning.
5. As a voice user, I want to be told (verbally or visually) when a turn fails or times out, so that I never sit in unexplained silence wondering if Bob heard me.
6. As a voice user, I want a turn that hits a frozen LLM or TTS engine to be cut off with a short fallback response, so that one stuck provider doesn't kill the conversation.
7. As a voice user, I want Bob's audio to actually reach me whenever the backend believes it spoke, so that there are no "ghost replies" I never heard.
8. As a voice user, I want the voice session to recover to a clean state after any internal error, so that my next "voice start" always works instead of inheriting a stuck state.
9. As a voice user, I want two rapid "voice start" actions to never produce two competing listening loops, so that my speech is never routed to a dead session.
10. As a HUD user, I want the interface to keep streaming updates even if my debug window (or any other window) has a degraded connection, so that one slow window never freezes the whole app.
11. As a HUD user, I want sub-task progress and Bob's speech text to keep appearing smoothly during heavy streaming, so that the interface never feels janky while Bob works.
12. As a user waiting on a sub-task (mail search, web search), I want a stuck tool to be detected and reported within ~15 seconds instead of 30+, so that I can rephrase instead of waiting in silence.
13. As a user who fires several sub-tasks at once, I want queued tasks to remain trackable and eventually reported, so that no request of mine evaporates.
14. As a user of local models, I want each turn to reuse as much of the previous prompt prefix as possible, so that my machine spends its time generating, not re-reading the same system prompt.
15. As the developer, I want every voice turn to log a timing breakdown of its critical path, so that I can see exactly where the time goes.
16. As the developer, I want aggregated latency percentiles (P50/P95) per pipeline stage, so that I can verify each optimization actually moved the number it targeted.
17. As the developer, I want the speculative draft adoption rate tracked, so that I can decide whether the draft model earns its compute cost.
18. As the developer, I want the validation-retry distribution tracked, so that I know whether retries are a real latency multiplier or a theoretical one.
19. As the developer, I want every fire-and-forget asyncio task on a critical path to log and surface its exception, so that background failures stop being invisible.
20. As the developer, I want the proactive announcement loop to be supervised, so that a crashed flusher is detected instead of silently dropping all future announcements.
21. As the developer, I want a failed voice-turn persistence to emit a client-visible event, so that data loss is observable instead of mysterious.
22. As the developer, I want the debug JSONL sink to stop writing when nothing subscribes to it, so that the hot path doesn't pay disk I/O for an unused feature.
23. As an operator, I want a dead WebSocket emitter to be evicted after a bounded timeout, so that zombie connections can't accumulate or block the event bus.
24. As an operator, I want boot-time warmup failures (STT/TTS/role clients) to be loudly logged and reflected in health state, so that a degraded boot is visible before the first user turn fails.

## Implementation Decisions

### Module 1 — TurnLatencyMetrics (deep module, build first)

- New metrics collector keyed by turn id with a minimal interface: mark a named stage (`endpoint`, `stt_finalized`, `loops_frozen`, `gate_decided`, `llm_first_token`, `tts_first_chunk`, `audio_first_byte`), record a counter (draft adopted/discarded, validation retry), and project a per-turn summary plus rolling aggregates (P50/P95 per stage).
- Pure in-memory, bounded retention; no persistence. Summaries emitted through the existing debug-event channel at turn end so the Debug View shows them without new UI work.
- Instrumentation lands **before** the optimizations; each later module's acceptance is expressed as a delta on these metrics.
- Draft hit-rate and retry counts feed the same collector — one place answers "where does the time go".

### Module 2 — EndpointCommit (voice loop critical path)

- At endpoint, Thinker freeze, Draft freeze, and STT finalize run **concurrently** instead of sequentially; the say-path is spawned as soon as the gate decision is available, not after all cleanup completes.
- Cooperative-cancel grace for Thinker/Draft is capped at 250 ms on the endpoint path (down from 2 s); past the cap, hard cancel.
- The barge-in path gets a **separate, zero-grace policy**: hard cancel of the say-path and anticipation loops immediately after barge-in confirmation. Endpoint and barge-in no longer share the same freeze semantics.
- Backchannel synthesis becomes fire-and-forget (supervised, errors suppressed) instead of awaited in the frame loop.
- The user-turn-complete semantic signal bypasses the Thinker debounce: the endpoint bit propagates immediately even when the inference cadence is debounced.

### Module 3 — SpeechStreamPipeline (TTS)

- A dedicated pipeline object owns the path "stream of sentences in → stream of PCM chunks out": sentence N+1 enters Kokoro synthesis while sentence N's chunks are still draining to the client.
- A bounded producer/consumer queue sits between synthesis and the WebSocket write, so a slow reader applies backpressure to the queue, never to the synthesizer.
- The pipeline is cancellable as a unit (barge-in calls one cancel), and reports first-chunk timing to TurnLatencyMetrics.
- Per-chunk debug events are replaced by a periodic batch summary (count + bytes per window).

### Module 4 — Robust WS fan-out (event bus)

- Every emitter call in the broadcast loop is wrapped in a bounded timeout (~1–2 s); an emitter that times out or raises is evicted from the registry immediately.
- High-frequency hot-path events (speech deltas, reasoning deltas, audio progress) are coalesced backend-side into batched emissions on a ~50–100 ms window; low-frequency events stay immediate.
- The debug JSONL file sink writes only when enabled by setting **and** at least one subscriber/file-sink consumer exists; writes are batched instead of per-event flush.
- Retention enforcement stops re-serializing events to measure size (size cached at append).

### Module 5 — TaskSupervisor (no silent failures)

- A single helper supervises fire-and-forget tasks: it attaches a done-callback that reads the task result, logs exceptions with context, and optionally emits a client/debug event. All critical `create_task` sites adopt it: TTS synthesis (proactive and main), event-bus subscriber dispatch, the proactive flusher, typing-reset.
- The voice FSM is force-reset to idle in the say-path exception handler, even if finalize itself fails — the "never two turns speaking" invariant survives any exception path.
- The voice-start handler clears the session's loop slot **before** stopping the old loop, and stops it under suppression, eliminating the double-loop race.
- Voice-turn persistence failure emits a client-visible event instead of a log-only swallow.
- MCP runtime startup failure is recorded in app state and exposed via the health endpoint.

### Module 6 — TurnWatchdog (bounded turns)

- Every user turn (text or voice) runs under a wall-clock budget; expiry emits a turn-timeout event, transitions the FSM to a clean state, and triggers a short verbal/text fallback instead of eternal silence.
- Network-bound awaits on the turn path (summary regeneration, proactive synthesis, TTS preload, TTS streaming) get explicit timeouts with degrade-and-continue semantics.
- A separate, much shorter TTFT timeout (~15–30 s) guards "the provider never started answering", distinct from the long completion budget.

### Module 7 — Runner hardening (sub-agent)

- Stall counter resets when the tool error **changes** (a genuinely new failure means progress in diagnosis), not only on success; the force threshold drops from 4 to 3 consecutive no-progress iterations.
- The advertised-tools selection and rendered tool catalogue are computed once per run (the goal is immutable) and cached; iterations reuse the cached prompt block, which also stabilizes the prompt prefix.
- System-prompt assembly across Jarvis/runner orders stable fragments first and variable fragments (temporal context, validation feedback) last, to keep the local-model prefix cache warm across turns.

### Module 8 — BootWarmup

- STT engine, TTS engine, and the per-role LLM clients (Jarvis, Thinker, Draft) are warmed in a supervised background task started right after the app lifespan yields — clients can connect immediately; the first voice turn awaits only what isn't ready yet.
- Warmup progress/failures are logged and reflected in health state; the existing "preparing" toast events are reused when a user beats the warmup.
- The existing skip-preload setting keeps working (warmup becomes a no-op).

### Cross-cutting

- No new external dependencies; everything stays asyncio + existing stack.
- All thresholds introduced (grace cap, fan-out timeout, batch window, watchdog budgets, stall threshold) are settings with the defaults stated above, so they can be tuned without code changes.
- Order of delivery: Module 1 first (baseline numbers), then 2/3 (latency core), 4/5/6 (reliability), 7/8 (hardening) — but modules are independent enough to parallelize after 1.

## Testing Decisions

Tests target **external behavior only**: given inputs/events at a module boundary, assert observable outputs (events emitted, timing order, state transitions, fallbacks) — never internal call sequences or private attributes. The codebase has strong prior art: fake clocks and fake LLM/TTS engines in the voice-loop and runner test suites (`backend/tests`), event capture via the debug/event-bus test helpers, and FSM transition tests from the full-duplex feature (PRD 0016). Reuse those fixtures.

All modules get tests (decision A):

- **TurnLatencyMetrics**: marks produce correct per-turn summaries and aggregates under a fake clock; bounded retention; unknown turn ids are safe no-ops.
- **EndpointCommit**: with fake Thinker/Draft/STT that stall, the say-path still launches within the grace cap; barge-in cancels with zero grace; sequential-vs-concurrent verified through event timestamps on a fake clock.
- **SpeechStreamPipeline**: with a fake synthesizer and a slow sink, sentence N+1 synthesis starts before sentence N finishes draining; backpressure bounds the queue; one cancel stops everything; first-chunk mark recorded.
- **WS fan-out**: a hanging emitter is evicted within the timeout and later events still reach healthy emitters; batching coalesces a burst into bounded emissions; JSONL sink writes nothing when no subscriber.
- **TaskSupervisor**: a supervised task that raises produces a log + event; FSM returns to idle after a say-path exception injected at each stage (before first audio, mid-audio, during finalize); double voice-start leaves exactly one live loop.
- **TurnWatchdog**: a never-resolving fake provider triggers timeout event + FSM reset + fallback emission within the budget (fake clock).
- **Runner hardening**: changing error codes reset the stall counter; threshold 3 forces termination; advertised-tools computed once per run (observable via prompt content stability across iterations, not call counting).
- **BootWarmup**: boot completes without awaiting warmup; a user request arriving mid-warmup gets the preparing event then succeeds; warmup failure is visible in health state.

## Out of Scope

- **Multi-tool-call per LLM response** in the sub-agent (audit 3.2) — contract/schema change, separate PRD.
- **Runner/orchestrator god-object refactor** (audit 3.7, backlog) — only the minimal extraction each module needs.
- **Claude CLI daemon/persistent process** (audit 2.1) — Claude CLI stays the slow path; LM Studio remains the recommended voice provider.
- **Store atomicity & memory-leak fixes** (audit 5.7, 5.8, 5.12 — atomic selection writes, session dict eviction, SQLite WAL/batching) — long-run reliability, separate hardening pass.
- **Adaptive VAD/endpoint thresholds** (audit 1.10) — needs the metrics from Module 1 first; tune in a follow-up once real pause distributions exist.
- **Cross-role KV-cache sharing** between Jarvis/Thinker/Draft (audit 2.4) — speculative, revisit after Module 7's prefix stabilization is measured.
- Any UI redesign — the HUD only gains existing-channel debug data.

## Further Notes

- The audit document `docs/investigations/2026-06-09-perf-reliability-audit.md` is the canonical finding list; each module maps to its "Top 10 ROI" table (#1–#10).
- Success criteria, measured by Module 1 before/after: endpoint→first-audio P95 reduced by ≥1 s on local models; barge-in cut-time P95 < 300 ms; zero unexplained-silence paths (every failure mode produces an event); first-turn-after-boot within 1.5× of steady-state turns.
- The deferred backlog from the prod-hardening pass (`docs/investigations/2026-06-09-*`) stays valid; this PRD intentionally overlaps it only where the audit re-confirmed an item as latency-critical (boot warmup).
