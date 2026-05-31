# PRD 0012 — LLM Provider & Model Picker

## Problem Statement

Bob's LLM backend is frozen at boot. Choosing between Claude CLI and LM Studio, and
picking which local model runs, requires editing `.env` and restarting the backend by
hand (`uv run uvicorn …`). There is no way, from the running app, to:

- switch the engine (Claude CLI ↔ LM Studio),
- see which models the local LM Studio server actually has and pick one,
- tune the context length of the chosen local model,
- know which model is currently driving Bob.

For a personal desktop assistant this is friction: the user has to leave the app, hand-edit
config, and lose the running session every time they want a faster/heavier model or to flip
to Claude. The mockup (`Design Mockup/provider.jsx`) already designs the intended UX; today
it is purely cosmetic and wired to local `tweaks` state with a hardcoded model catalogue.

## Solution

A live LLM picker in the Sphere HUD (top-left zone), ported from the mockup, that lets the
user:

- toggle the provider between **Claude CLI** and **LM Studio**,
- under LM Studio, open a dropdown listing the **real models on the server** (live, filtered
  to chat-capable models), with metadata (quantisation, architecture, loaded state),
- select a model — Bob loads it proactively into VRAM, unloading the previous one,
- adjust that model's **context length** with a slider bounded to the model's maximum, applied
  on an explicit *Apply* button,
- see the active engine/model truthfully reflected in the agent-console footer.

Everything applies **live, without restarting the backend**: the selection is persisted to a
JSON file under `~/.bob`, the affected LLM clients are rebuilt in-process and swapped, and the
swap is non-interruptive (an in-flight turn finishes on the previous client). Every mutation is
**validate-then-swap**: if loading or rebuilding fails (OOM, server down, `claude` missing),
Bob stays on the previous working selection and surfaces an error — it never points at a broken
engine.

## User Stories

1. As a Bob user, I want to switch between Claude CLI and LM Studio from the HUD, so that I
   don't have to edit `.env` and restart the backend.
2. As a Bob user, I want the engine switch to take effect immediately, so that my next turn
   uses the new engine without a manual restart.
3. As a Bob user, I want to see the list of models actually available on my LM Studio server,
   so that I pick a model that really exists rather than a stale hardcoded name.
4. As a Bob user, I want the model list to refresh when I open the dropdown, so that models I
   just added/removed in LM Studio are reflected.
5. As a Bob user, I want chat-incompatible models (embeddings) hidden from the list, so that I
   can't accidentally select a model that would break the chat.
6. As a Bob user, I want each model shown with its quantisation, architecture and loaded state,
   so that I can judge the trade-off and prefer an already-loaded model.
7. As a Bob user, I want selecting a model to load it proactively into VRAM, so that the first
   real turn isn't slow and I get truthful "loading → loaded" feedback.
8. As a Bob user, I want the previously loaded model unloaded when I switch, so that VRAM is
   freed and I don't hit OOM by stacking models.
9. As a Bob user, I want to set the context length of the chosen local model, so that I can
   trade VRAM for a larger working window.
10. As a Bob user, I want the context-length slider capped at the model's maximum, so that I
    can't request a value that would fail to load.
11. As a Bob user, I want my context-length choice remembered per model, so that returning to a
    model restores the value I set for it.
12. As a Bob user, I want the context-length change to apply only when I press *Apply*, so that
    dragging the slider doesn't trigger repeated expensive reloads.
13. As a Bob user, I want a larger context length to let Bob actually use more conversation
    context, so that the setting has a real effect on behaviour.
14. As a Bob user, I want the active engine and model shown in the console footer, so that I
    always know what is driving Bob.
15. As a Bob user on Claude CLI, I want the Claude model shown as a read-only label, so that I
    see what's running even though there's nothing to pick.
16. As a Bob user, I want my provider/model/context-length selection to survive a backend
    restart, so that Bob comes back on the engine I last chose.
17. As a Bob user on a fresh install, I want Bob to start on an already-loaded model (or the
    first available one), so that it works out of the box without me configuring anything.
18. As a Bob user, I want a failed switch (OOM, server down, `claude` missing) to keep me on my
    previous working engine with an error message, so that Bob is never left broken.
19. As a Bob user, I want to keep switching/loading even while Bob is working, so that the
    picker isn't locked during a turn; the change applies to the next request.
20. As a Bob user, I want a clear loading state while a large model warms up, so that I
    understand why the picker is busy.
21. As a Bob user, I want a clear error state if my LM Studio server is unreachable when listing
    models, so that I know the problem is the server, not Bob.
22. As a developer, I want the engine selection to drive both Jarvis and the sub-agents from a
    single global model, so that the picker stays simple and consistent.
23. As a developer, I want the inference hot-path to stay on the OpenAI-compatible client, so
    that the recently-stabilised tool-calling codec and "any OpenAI-compatible endpoint"
    portability are preserved.

## Implementation Decisions

### Selection & persistence

- A `LLMSelectionStore` deep module owns a JSON file under `~/.bob`
  (`{ provider, lm_model, context_length: { <model_id>: <int> } }`). It is the runtime source
  of truth.
- On first boot with no JSON, the store **seeds** from the existing `.env` settings
  (`LLM_PROVIDER` / `LLM_MODEL`) and writes the JSON. Thereafter the JSON wins; `.env` is only a
  fallback seed.
- Context length is remembered **per model** (map keyed by model id). Switching models reapplies
  the stored value for that model, or the model's default if never set.
- The cold-start model (provider = LM Studio, no model anywhere) is the model already loaded in
  LM Studio if any, else the first chat-capable model in the list.

### Live switch (no restart)

- Both provider and model changes apply **live, in-process** via a single mechanism:
  *selection change → rebuild affected LLM clients → swap reference*. The Jarvis client (held by
  the Orchestrator) and the sub-agent client (held by the SubAgentRunner) are rebuilt from the
  factory using the new selection rather than only from frozen `Settings`.
- The swap is **non-interruptive**: an in-flight turn completes on the previous client; the new
  client is read at the next request. Swap is guarded by an `asyncio.Lock` to serialise
  concurrent mutations.
- **Validate-then-swap + revert** is the universal contract for every mutation:
  - Provider switch validates the target before swapping (probe LM Studio for LM, verify the
    `claude` binary for Claude CLI). On failure: keep previous provider, do not write JSON,
    return an error.
  - Model load / context-length reload loads via the SDK first. On failure (OOM, model error):
    keep previous selection, do not write JSON, return an error.

### LM Studio management (hybrid)

- **Inference stays on `AsyncOpenAI`** against LM Studio's OpenAI-compatible endpoint — the
  PRD 0008/0009 codec layer and the "any OpenAI-compatible endpoint" portability are untouched.
- A new `LMStudioManager` deep module uses the official **`lmstudio` Python SDK** (new dependency,
  isolated to management) for:
  - `list_models()` — returns chat-capable models only (type ∈ {llm, vlm}, embeddings excluded)
    with id, quantisation, architecture, max context length, and loaded state.
  - `load(model_id, context_length)` — loads with the given context length config, **unloads the
    previously loaded model**, and surfaces structured errors (OOM, not found).
- Proactive load is triggered on model selection and on context-length *Apply*. There is no
  warmup-request hack; the SDK load-with-config is the mechanism.

### Context length ↔ token budget coupling

- Context length is a **load-time** parameter; changing it reloads the model. The slider is
  bounded to the model's `max_context_length`; the reload fires on an explicit *Apply* button.
- When on LM Studio, the chosen context length **drives** the bounded-context token budget:
  `token_budget = max(DEFAULT_TOKEN_BUDGET, context_length − RESERVE)` where `RESERVE ≈ 6k`
  (≈ 4096 generation `max_tokens` + ≈ 2k for tool definitions / system / safety margin). The
  floor keeps the budget at least the current default.
- When on Claude CLI (no context-length control), the token budget stays at the current
  `DEFAULT_TOKEN_BUDGET` (2048). No dedicated Claude budget constant in v1.

### API contract (REST)

- `GET /api/llm/models` — live, chat-capable LM Studio models with metadata + loaded state.
- `GET /api/llm/selection` — current `{ provider, lm_model, context_length, … }`.
- `PUT /api/llm/selection` — **synchronous, blocking** (generous ~120s timeout). Performs
  validate-then-swap (provider rebuild and/or model load with context length), and on success
  writes the JSON and returns the new selection; on failure returns an error and leaves the
  previous selection intact.

### Frontend

- The `ProviderPicker` component is ported from `Design Mockup/provider.jsx` into the real
  Sphere HUD (window `new`, top-left HUD zone). The legacy ChatView does **not** get the picker.
- Segmented provider toggle (Claude CLI / LM Studio). Under LM Studio: a dropdown that fetches
  `GET /api/llm/models` **on open**, a context-length slider bounded to the selected model's
  maximum, and an *Apply* button for context length. Under Claude: a read-only model label
  (from `CLAUDE_CLI_MODEL` or default).
- Loading state while a `PUT` is in flight; error state on failed switch/load and on an
  unreachable LM Studio server at listing time. The active engine/model label feeds the
  agent-console footer.

## Testing Decisions

Tests cover **all modules**. A good test here asserts **external behaviour** through each
module's public interface — never private internals or SDK call shapes. The LM Studio SDK and
the `claude` binary are at the system boundary and are faked/stubbed so tests are deterministic
and offline, mirroring how the existing suite fakes the LLM client.

- **`LLMSelectionStore`** — seeding from `.env` on first boot, JSON-wins precedence on later
  boots, round-trip persistence, per-model context-length map read/write, cold-start model
  resolution. Prior art: settings/config tests and the JSON-backed stores under
  `backend/tests`.
- **`LMStudioManager`** (SDK faked) — `list_models()` filters embeddings out, exposes
  metadata + loaded state; `load()` unloads the previous model and propagates structured
  errors (OOM/not-found). Prior art: connector/client tests that fake an external client.
- **Client-swap / factory** — selection change rebuilds the correct client(s) and swaps the
  reference; in-flight calls keep the old client (non-interruptive); failed rebuild leaves the
  previous client in place. Prior art: `test_streaming_orchestrator_e2e.py` and factory wiring
  tests.
- **Budget coupling** — `token_budget = max(2048, ctx − RESERVE)` for LM Studio, default budget
  for Claude. Prior art: the context-policy budget tests (slice 0046).
- **REST endpoints** — `GET /models`, `GET/PUT /selection`: success writes JSON + returns new
  selection; validate-then-swap failure returns an error and leaves selection unchanged;
  concurrent `PUT`s are serialised. Prior art: FastAPI route tests via the existing httpx test
  client.
- **`ProviderPicker` (frontend)** — provider toggle, model list fetch on dropdown open,
  embeddings absent, context-length slider clamped to model max, *Apply* triggers the `PUT`,
  loading/error states render. Prior art: the Vitest component/store tests under
  `frontend/src` (e.g. `agentPhase.test.ts`, activity-feed store tests).

## Out of Scope

- **Per-model tool-calling codec.** The codec (PRD 0009) stays backend-level (`auto` → native
  for LM Studio). Choosing a model with poor native function-calling support may degrade tool
  use; this is a **documented v1 limitation**, not handled by per-model override or capability
  probing.
- **Claude model selection.** The Claude model is fixed/read-only (from `CLAUDE_CLI_MODEL`); no
  Claude model dropdown.
- **Per-role model selection.** A single global model drives Jarvis and sub-agents; no separate
  Jarvis/sub-agent pickers despite `JARVIS_BACKEND`/`SUBAGENT_BACKEND` existing.
- **Tauri backend sidecar / auto-restart.** The backend is not a Tauri-managed sidecar and this
  PRD does not make it one; the live in-process rebuild removes the need for any restart.
- **Dedicated larger Claude token budget.** Claude keeps the default 2048 budget in v1.
- **Multi-window selection sync.** The picker lives only in the Sphere HUD; REST-only, no WS
  broadcast of selection state to other windows.
- **Legacy ChatView picker.**

## Further Notes

- The `lmstudio` Python SDK is a **new backend dependency**, deliberately confined to the
  management module so the inference path keeps using `openai`.
- Warming a large model (e.g. 70B) may approach the ~120s `PUT` timeout; the timeout should be
  generous and the front should show a clear loading state.
- The picker must show a distinct error state when LM Studio is entirely unreachable at listing
  time (server down ≠ Bob bug).
- Existing `.env` setups keep working: `.env` seeds the JSON on first boot, then becomes a
  fallback. The current `Settings` validation that requires `LLM_MODEL` for `lm_studio` should be
  reconciled with cold-start (already-loaded / first-available) so a missing `.env` model no
  longer hard-blocks boot.
- Mockup reference: `Design Mockup/provider.jsx`, `Design Mockup/app.jsx` (top-left HUD zone),
  `Design Mockup/screenshots/0{1,2,3}-provider.png`.
