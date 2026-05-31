## Parent

prd/0012-llm-provider-model-picker.md

## What to build

The persistence foundation for the LLM picker. A `LLMSelectionStore` deep module that owns a
JSON file under `~/.bob` (`{ provider, lm_model, context_length: { <model_id>: <int> } }`) and is
the runtime source of truth for the LLM selection. On first boot with no JSON, it **seeds** from
the existing `.env` settings (`LLM_PROVIDER` / `LLM_MODEL`) and writes the JSON; thereafter the
JSON wins and `.env` is only a fallback seed. Expose the current selection over a read-only REST
endpoint `GET /api/llm/selection`.

No client rebuild, no model loading, no mutation endpoint yet — this slice just establishes the
store, the seed/precedence rule, and the read path end-to-end (file → store → REST → curl).

## Acceptance criteria

- [ ] `LLMSelectionStore` reads/writes `~/.bob/llm_selection.json` with the shape `{ provider, lm_model, context_length: { model_id: int } }`.
- [ ] On first boot (no JSON), the store seeds from `.env` (`LLM_PROVIDER` / `LLM_MODEL`) and persists the JSON.
- [ ] On later boots, the JSON value wins over `.env`; `.env` is used only when the JSON is absent.
- [ ] Per-model context-length map round-trips through write/read.
- [ ] `GET /api/llm/selection` returns the current `{ provider, lm_model, context_length, … }`.
- [ ] Tests: first-boot seed, JSON-wins precedence, per-model ctx round-trip, `GET` returns current selection.

## Blocked by

- None - can start immediately.
