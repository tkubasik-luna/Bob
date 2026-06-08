# MVP → Prod hardening — 2026-06-09

Autonomous overnight pass on the three reported pain areas (tooling reliability,
settings/startup, voice) plus a clean-architecture sweep. Branch:
`prod-hardening`. Backend: 1675 passed / 1 pre-existing failure. Frontend: tsc
clean / 6 pre-existing SettingsControl failures. Zero regressions introduced.

## What shipped (this branch)

| Area | Root cause | Fix |
|---|---|---|
| Tooling "sometimes works" | `select_tools` could advertise an EMPTY catalogue (no `always_on` tool, lexical gate misses) → tool use silently disabled for the run | `ensure_non_empty` safety net: fall back to top-k by score; runner always opts in |
| "model doesn't know it failed" | failed sub-task leaves `task.result=None`; failed-synthesis read only that | orchestrator falls back to the persisted reason (last `system` msg), translating bare reason codes to human FR |
| Wrong URL in picker | `/api/llm/roles` projected role `base_url` verbatim (null), UI substituted hardcoded `localhost:1234` | `_role_map_payload` falls back lm_studio roles to effective `LLM_BASE_URL` |
| Duplicate model loads | host strings not canonicalised; `assign_role` ignored already-resident models | loopback canonicalisation in `host_from_base_url`; `assign_role` adopts server-loaded models |
| Voice laggy / "broken" | STT re-transcribed the WHOLE growing buffer every ~1s (quadratic) | trailing-window partials (`STT_PARTIAL_WINDOW_SECONDS`) + independent cadence (`STT_PARTIAL_INTERVAL_SECONDS`); full-buffer pass only on finalize |
| No voice feedback | bridge dropped all `stt_*` / `voice_turn_error` events | `liveUserTranscript` store slice + handlers (live transcript, prep/error toasts) |
| Bob cuts himself off | half-duplex `muteOutbound` gate never driven | `SphereUI` lifts `useTurnState`, mutes outbound PCM while `bob_speaking` |
| Startup UX | HUD launched on a possibly-misconfigured backend | `SetupScreen` gate before the HUD (provider/URL/model, live ping, explicit load) |
| Dead code | — | removed AgentActivityPanel/Lanes/Block tree, TaskOverlay, sub_agent_runner shim |

## Deferred (conscious "not tonight" — robustness-first, need real server / wider blast radius)

1. **Boot double-seed** (`main.py:151-187`) — v1 flat store + v2 role store both
   seed/rewrite the same JSON; cold-start model written to flat only. It's file
   churn, not the duplicate-LOAD bug (that's fixed). Reworking the boot sequence
   is the highest-risk change and needs a real LM Studio server to validate.
2. **Backchannel playback** — synthesised audio is dropped client-side (its
   `backchannel:*` msg_id never matches `currentMsgIdRef`). Fixing it means an
   unvalidated concurrent-audio path; it currently no-ops harmlessly. Either wire
   a dedicated backchannel stream or stop synthesising them.
3. **SDK transport fragility** (`llm/lmstudio_sdk/`) — touches private SDK
   internals; pin the `lmstudio` version + add a defensive fallback to the openai
   transport on `AttributeError`/`TypeError` (version drift). Held 0116 purge.
4. **Hermes/Jarvis malformed-call vs no-call** (RC-D/E) — `HermesToolCodec.parse`
   and the streamed-arg path silently return `[]`/skip; surface a parse-specific
   correction instead of the generic "you didn't call a tool".
5. **v1 `/selection` vs v2 `/roles` overlap** — two selection stores write one
   file. Pick one surface; the dual-write is the structural source of the URL/
   load divergence. SetupScreen drives v1; the HUD panel drives v2.
6. **Misleading renames** — `event_bus_v2.py` → `ws_event_producer.py`;
   `legacy_full_history` → `full_history` (CAREFUL: persisted policy-id strings);
   `components/sphere/` → `components/overlay/` (collides with `src/sphere/`).
   No behaviour win, wide diff — do as a dedicated pass.
7. **God objects** — `orchestrator.py` (2k LOC), `ws_router.py` (1.9k, business
   logic in a router), `sub_agent/runner.py` (2.9k). Extract along existing seams.
8. **Multi-tenancy** — ~13 module-global singletons (`set_default_*`) hard-cap
   Bob at single-tenant; an `AppContext`/DI container is the prerequisite for a
   SaaS path. Land last, after the god objects shrink the blast radius.

## Pre-existing test failures (NOT introduced here)
- `tests/test_config.py::test_settings_loads_from_env` — env pollution
  (`LLM_TIMEOUT_SECONDS`); fails on a clean tree too.
- `frontend SettingsControl.test.tsx` — 6 failures, pre-existing on clean tree.
- mypy baseline dirty (`test_role_swap`, `test_tool_dispatcher`,
  `test_sub_agent_v2_runner`, env-dependent pywhispercpp `type: ignore`).
