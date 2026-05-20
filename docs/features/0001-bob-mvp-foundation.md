# Bob MVP Foundation

Shipped on 2026-05-20 from PRD `prd/0001-bob-mvp-foundation.md`.

## What it does

Bob is a desktop personal AI assistant. The user opens the Tauri app, types a message, and sees the assistant reply: a textual answer plus, optionally, additional UI components rendered dynamically (server-driven UI). All wiring sits on top of a local LM Studio server (or any OpenAI-compatible endpoint) talking to the FastAPI backend over a single WebSocket connection.

## Technical surface

- **Backend (`backend/`, Python 3.12 + FastAPI + uv)**
  - HTTP `GET /health` — readiness probe.
  - WebSocket `GET /ws/chat` — bidirectional protocol (frame types: `session`, `user_msg`, `assistant_msg`, `thinking`, `error`).
  - Modules: `bob.config` (pydantic-settings), `bob.llm_client` (`LLMClient` ABC + `LMStudioClient`), `bob.prompts` (Markdown loader), `bob.ui_registry` (component catalog + JSON Schema), `bob.response_parser` (validate + retry once + fallback), `bob.conversation` (in-memory per-session history), `bob.chat_service` (orchestrator), `bob.ws_router` (WS endpoint with DI seam for tests), `bob.logging_setup` (structlog JSON to stdout + daily `logs/llm-YYYY-MM-DD.jsonl`).
  - Smoke CLI: `python -m bob.smoke "hello"` (one-shot) or `python -m bob.smoke` (REPL).
- **Frontend (`frontend/`, Tauri 2 + Vite + React 19 + TypeScript strict + Tailwind v4 + Biome + Zustand 5)**
  - `useWebSocket` hook (auto-reconnect, exponential backoff, outbound queue).
  - `chatStore` Zustand store (messages, connection status, thinking indicator, toasts).
  - `ChatView` (header + scrollable history + textarea+send + auto-scroll + thinking dots + disconnect badge).
  - `componentRegistry` mapping `ChatMessage` → `ChatMessageBlock`, `Markdown` → `MarkdownView` (react-markdown + remark-gfm).
  - `Dispatcher` renders the `ui[]` array; unknown components fall back to a warning block without crashing.
  - `Toast` component for error frames with auto-dismiss.
- **Prompts**: `backend/prompts/system_chat.md` — V0 French system prompt with `{components_description}` placeholder injected at runtime.
- **Config**: `.env` at repo root, see `.env.example`. Required: `LLM_BASE_URL`, `LLM_MODEL`, `LLM_API_KEY`. Defaults: `BACKEND_HOST=127.0.0.1`, `BACKEND_PORT=8000`, `LOG_LEVEL=INFO`, `LLM_TIMEOUT_SECONDS=60`.

## Notable decisions

- `LMStudioClient.chat` accepts an optional JSON Schema and forwards it via `response_format={"type": "json_schema", "json_schema": <schema>}`. The schema shape returned by `ui_registry.get_response_schema()` is the LM Studio wrapped envelope `{name, strict, schema}` — `validate_response` unwraps before feeding `jsonschema.Draft202012Validator`.
- `response_parser.parse` does one retry on JSON parse failure or schema violation. The correction message is appended to a local copy of the messages — the persistent `conversation` store is never polluted by retries.
- `session_id` is threaded through `LLMClient.chat` (optional kwarg) so every LLM call (including the retry) shows up in `logs/llm-*.jsonl` annotated with its session.
- Error mapping in `ws_router`: `ConnectionError` / `httpx.ConnectError` / `openai.APIConnectionError` → `LLM_UNREACHABLE`; `TimeoutError` / `openai.APITimeoutError` → `LLM_TIMEOUT`; anything else → `INTERNAL` (stack logged backend-side, never leaked to the client). The WS connection stays alive across errors.
- Frontend treats the connection lifecycle as the only correlation: the backend-generated `session_id` is exposed in the `session` frame for debug only — the frontend doesn't use it in its protocol.
- React 19 used instead of React 18 (PRD said 18) — newer is default in the scaffold, no architectural impact.

## Issues

- `issues/0001-scaffold-monorepo-tooling.md` — Scaffold monorepo + tooling — commit `ea73078`
- `issues/0002-ws-echo-end-to-end.md` — WS echo end-to-end — commit `3641628`
- `issues/0003-llm-client-config-prompts.md` — config + llm_client + prompts + smoke CLI — commit `a3f32df`
- `issues/0004-ui-registry-response-parser.md` — ui_registry + response_parser — commit `752ed99`
- `issues/0005-conversation-chat-service.md` — conversation store + chat_service — commit `7f9a0d1`
- `issues/0006-wire-ws-frontend-dispatch.md` — wire WS to chat_service + frontend Dispatcher — commit `830c74c`
- `issues/0007-logging-error-handling-ws-tests.md` — structlog + WS error mapping + frontend toasts — commit `29568ab`
