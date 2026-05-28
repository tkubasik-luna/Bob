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
