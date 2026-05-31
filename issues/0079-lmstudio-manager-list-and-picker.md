## Parent

prd/0012-llm-provider-model-picker.md

## What to build

The live model list, end-to-end. A new `LMStudioManager` deep module using the official
`lmstudio` Python SDK (new backend dependency, isolated to management — inference stays on
`openai`) exposing `list_models()`: chat-capable models only (type ∈ {llm, vlm}, embeddings
excluded), each with id, quantisation, architecture, max context length, and loaded state.
Expose it over `GET /api/llm/models`. Port the `ProviderPicker` skeleton from
`Design Mockup/provider.jsx` into the real Sphere HUD (window `new`, top-left zone) so that under
LM Studio the dropdown fetches the list **on open** and renders it (read-only at this stage),
highlighting the current selection from `GET /api/llm/selection`.

Demoable: open the picker dropdown, see the real models on the LM Studio server with metadata.

## Acceptance criteria

- [ ] `LMStudioManager.list_models()` returns only chat-capable models (type ∈ {llm, vlm}); embeddings are excluded.
- [ ] Each model carries id, quantisation, architecture, max context length, loaded state.
- [ ] `GET /api/llm/models` returns the live list; an unreachable LM Studio server yields a distinct error response (not a crash).
- [ ] `ProviderPicker` is mounted in the Sphere HUD top-left zone and fetches `GET /api/llm/models` when the dropdown opens.
- [ ] The dropdown highlights the current selection and renders metadata + loaded state; embeddings are absent from the list.
- [ ] Tests: SDK faked — `list_models()` filters embeddings and exposes metadata/loaded state; `GET /models` success + server-down error; Vitest picker test for fetch-on-open, embeddings absent, current selection highlighted.

## Blocked by

- issues/0078-llm-selection-store-and-get.md
