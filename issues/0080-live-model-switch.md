## Parent

prd/0012-llm-provider-model-picker.md

## What to build

Live LM model switching, end-to-end. Add `PUT /api/llm/selection` for an `lm_model` change:
**synchronous and blocking** (generous ~120s timeout), performing **validate-then-swap**: load
the target model via `LMStudioManager.load(model_id, context_length)` with the default context
length, **unload the previously loaded model**, rebuild the `LMStudioClient`, and **swap** the
reference held by the Orchestrator (Jarvis) and the SubAgentRunner (sub-agent). The swap is
**non-interruptive** (an in-flight turn finishes on the previous client; the new client is read at
the next request) and guarded by an `asyncio.Lock`. On success, write the JSON; on failure
(OOM/model error), keep the previous selection, do not write JSON, return an error.

Resolve the **cold-start** model: provider = LM Studio with no model anywhere → use the model
already loaded in LM Studio if any, else the first chat-capable model.

Wire the picker: selecting a model fires the blocking `PUT`, shows a loading state while it runs,
an error state (and stays on the previous model) on failure, and updates the agent-console footer
label on success.

## Acceptance criteria

- [ ] `PUT /api/llm/selection` with a new `lm_model` loads it (default ctx), unloads the previous model, rebuilds + swaps the LM client for both Jarvis and sub-agent roles, and writes the JSON.
- [ ] The swap is non-interruptive: an in-flight turn completes on the previous client; the next request uses the new one. Concurrent `PUT`s are serialised by an `asyncio.Lock`.
- [ ] On load failure (OOM / model error) the previous selection is kept, the JSON is not written, and the endpoint returns an error.
- [ ] Cold-start resolves to the already-loaded model if present, else the first chat-capable model.
- [ ] Picker: model select triggers the blocking `PUT` with a loading state; failure shows an error and stays on the previous model; success updates the footer label.
- [ ] Tests: SDK faked — load+unload+swap on success; previous client retained on failure; in-flight call keeps old client; cold-start resolution; `asyncio.Lock` serialises concurrent mutations; Vitest picker loading/error/footer states.

## Blocked by

- issues/0078-llm-selection-store-and-get.md
- issues/0079-lmstudio-manager-list-and-picker.md
