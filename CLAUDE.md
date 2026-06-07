# Bob — Claude collaboration notes

Personal AI assistant. Tauri + React frontend, FastAPI backend, LM Studio (or any OpenAI-compatible endpoint) as the LLM. See `README.md` for the run/check commands.

## Shipped features

- [0001 Bob MVP Foundation](docs/features/0001-bob-mvp-foundation.md) — Desktop chat with LM Studio over WS + server-driven UI components.
- [0002 Voice Mode](docs/features/0002-voice-mode.md) — Kokoro TTS streaming, voice toggle, interruption + UI feedback.
- [0003 Jarvis Orchestrator](docs/features/0003-jarvis-orchestrator.md) — Persistent Jarvis thread + sub-task sidebar/drawer + tool-calling delegation + proactive synthesis.
- [0004 Sphere HUD UI](docs/features/0004-sphere-hud-ui.md) — Sphère WebGL + HUD minimal (tasks panel, transcript line, markdown overlay) en fenêtre Tauri séparée, cohabitant avec ChatView legacy.
- [0005 Debug View](docs/features/0005-debug-view.md) — Real-time event feed in a Cmd+Shift+D Tauri window over `/ws/debug` with filters, payload expand, turn highlight, tail scroll.
- [0006 Debug View — Grouped Tree](docs/features/0006-debug-view-grouped-tree.md) — Hierarchical tree (turn → sub-tasks → fused LLM calls), auto-expand current, replay collapsed-except-last.
- [0007 Jarvis v2 Context Overhaul](docs/features/0007-jarvis-v2-context-overhaul.md) — Bounded context + streaming TTS + task-aware tools + structured validation/retry.
- [0008 Gmail Connector & Mail Overlay](docs/features/0008-gmail-mail-overlay.md) — Read-only Gmail search via sub-task + dedicated Mail HUD overlay.
- [0009 Tool-Calling Unification](docs/features/0009-tool-calling-unification.md) — One canonical codec layer (native/Hermes/guided) + self-correction + typed deliverable.
- [0010 Tool Result Store](docs/features/0010-tool-result-store.md) — Per-run blackboard of tool results: compact transcript digest, deterministic deliverable projection on every exit path, convergence on terminal results. Fixes empty-overlay-on-stall; robust on weak local models.
- [0011 Adaptive Composite UI](docs/features/0010-adaptive-composite-ui.md) — Stacked-sections overlay: deliverable is `ComponentDescriptor[]`, multi-mail cards, per-section drop validation, defensive payload codec.
- [0012 Agent Activity Feed](docs/features/0011-agent-activity-feed.md) — Live per-agent reasoning feed in the HUD: token streaming + narrated fallback, chips, collapsable right rail, retention + rehydrate.
- [0013 LLM Provider & Model Picker](docs/features/0012-llm-provider-model-picker.md) — Live HUD picker: switch Claude CLI ↔ LM Studio, pick/load local models, tune context length — no backend restart.
- [0014 Refonte HUD « Piste 3D · Nacre »](docs/features/0014-hud-piste-3d-nacre.md) — In-place `new` HUD reskin: conscience orb + left thread-deck + right data dock + typed overlay + settings modal.
- [0015 Web Search Tool (Tavily)](docs/features/0015-web-search.md) — Tavily-backed `web_search` + `web_fetch` sub-agent tools (search → fetch → synthesise), dedicated `WebResults` HUD card, gated on `TAVILY_API_KEY`.
- [0016 MCP Tool Scaling](docs/features/0015-mcp-tool-scaling.md) — MCP-client connectors via config manifest + goal-driven `select_tools` gating; weather end-to-end, generic Markdown card, full LM Studio.
- [0017 Jarvis Temps Réel Full-Duplex](docs/features/0016-jarvis-realtime-fullduplex.md) — Full-duplex voice: streaming STT, parallel Thinker+Draft anticipation, barge-in, backchannels, per-role LLM picker + headless `bob attest` harness.
