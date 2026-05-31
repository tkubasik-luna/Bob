# LLM Provider & Model Picker

Shipped on 2026-06-01 from PRD `prd/0012-llm-provider-model-picker.md`.

## What it does

A live LLM picker in the Sphere HUD (top-left zone) lets the user switch the engine
(Claude CLI ↔ LM Studio), pick a local model from the real list on the LM Studio server,
and tune that model's context length — all **without restarting the backend**. Selecting a
model loads it proactively into VRAM (unloading the previous one); switching is
validate-then-swap, so a failed switch (OOM, server down, `claude` missing) keeps Bob on the
previous working engine and surfaces an error. The selection survives a backend restart.

## Technical surface

- **REST** (mounted under `/api/llm`, `backend/src/bob/llm_router.py`):
  - `GET /api/llm/selection` — current `{ provider, lm_model, context_length, claude_model }`.
  - `GET /api/llm/models` — live chat-capable LM Studio models (id, quantisation, architecture,
    `max_context_length`, loaded state); distinct 503 when the server is unreachable.
  - `PUT /api/llm/selection` — synchronous/blocking (~120s). Handles `lm_model` (+ optional
    `context_length`) and `provider` changes via validate-then-swap; writes the JSON on success.
- **Backend modules**:
  - `LLMSelectionStore` (`llm_selection_store.py`) — JSON source of truth at
    `~/.bob/llm_selection.json`; seeds from `.env` on first boot, JSON wins after.
  - `LMStudioManager` (`lm_studio_manager.py`) — `lmstudio` SDK (management only; inference stays
    on `openai`): `list_models()` (embeddings excluded), `load(model_id, context_length)`
    (loads target, unloads previous).
  - `LLMSwitcher` + `SubAgentClientHolder` (`llm_swap.py`) — `asyncio.Lock`-guarded,
    non-interruptive swap of the Jarvis client (`Orchestrator.set_jarvis_client`) and the
    sub-agent client (mutable holder read per task by the runner factory).
  - Factory (`llm/factory.py`) rebuilds clients from an `LLMSelection` via
    `settings.model_copy(update=...)`.
  - Budget coupling (`context/policy.py`): `token_budget_for_context_length(ctx)` =
    `max(DEFAULT_TOKEN_BUDGET, ctx − CONTEXT_LENGTH_RESERVE)` with `RESERVE = 6000`; applied live
    via `Orchestrator.set_token_budget`.
- **Frontend** (Sphere HUD only, not legacy ChatView):
  - `ProviderPicker` (`frontend/src/components/sphere/ProviderPicker.tsx`) — segmented provider
    toggle, fetch-on-open model dropdown, per-model context-length slider (clamped to
    `max_context_length`) + Apply, loading/error states, Claude read-only label.
  - `frontend/src/lib/llmApi.ts` — typed client for the three endpoints; `.pv-*` styles in
    `frontend/src/styles/hud.css`.
- **New dependency**: `lmstudio` Python SDK (backend, management-isolated).
- **Persistence**: `~/.bob/llm_selection.json` — no Flyway/SQLite tables; no application events.

## Notable decisions

- **Inference stays on `AsyncOpenAI`** against LM Studio's OpenAI-compatible endpoint; the
  `lmstudio` SDK is confined to model management (list/load/unload). The PRD 0008/0009 codec layer
  and "any OpenAI-compatible endpoint" portability are untouched.
- **Non-interruptive swap is free**: the Orchestrator reads `self._jarvis_client` per request, so
  replacing the reference lets an in-flight turn finish on the old client; the sub-agent runner is
  rebuilt per task from a mutable `SubAgentClientHolder`. All swaps serialise on one `asyncio.Lock`.
- **Validate-then-swap + revert** is the universal contract: load/probe first, mutate state and
  write JSON only on success — Bob never points at a broken engine.
- **Cold-start** (LM Studio, no model anywhere): use the already-loaded model if any, else the
  first chat-capable model.
- Context length is a **load-time** parameter remembered per model in the JSON; the slider drags
  local-only and reloads only on explicit **Apply**.
- Single global model drives both Jarvis and sub-agents (no per-role picker). Claude model is
  read-only (`CLAUDE_CLI_MODEL`); Claude keeps the default 2048 token budget.

## Issues

- `issues/0078-llm-selection-store-and-get.md` — LLM selection store + `GET /api/llm/selection` — commit c41564f
- `issues/0079-lmstudio-manager-list-and-picker.md` — `LMStudioManager.list_models` + `GET /api/llm/models` + picker skeleton — commit 47608bc
- `issues/0080-live-model-switch.md` — live LM model switch (`PUT`, validate-then-swap) — commit 66e1ff7
- `issues/0081-live-provider-switch.md` — live provider switch Claude CLI ↔ LM Studio — commit f4fa383
- `issues/0082-context-length-and-budget-coupling.md` — context-length slider + token-budget coupling — commit 59d7262
