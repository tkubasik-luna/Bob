## Parent

prd/0012-llm-provider-model-picker.md

## What to build

Context-length control for the selected LM model, end-to-end, with the bounded-context budget
coupling. The picker gains a context-length slider bounded to the selected model's
`max_context_length`; the value is remembered **per model** in the selection JSON; an explicit
**Apply** button triggers a reload via `LMStudioManager.load(model_id, context_length)` (the
existing validate-then-swap path). When on LM Studio, the chosen context length **drives** the
bounded-context token budget: `token_budget = max(DEFAULT_TOKEN_BUDGET, context_length − RESERVE)`
with `RESERVE ≈ 6k` (≈ 4096 generation + ≈ 2k tools/system/safety). When on Claude CLI (no
ctx control), the budget stays at `DEFAULT_TOKEN_BUDGET`.

## Acceptance criteria

- [ ] Picker shows a context-length slider clamped to the selected model's `max_context_length`; default = model default.
- [ ] The context-length value is persisted per model in the selection JSON and reapplied when returning to that model.
- [ ] An explicit Apply button triggers a reload-with-context-length (validate-then-swap); dragging the slider alone does not reload.
- [ ] On LM Studio, `token_budget = max(DEFAULT_TOKEN_BUDGET, context_length − RESERVE)` is applied to the bounded context; on Claude CLI the budget stays at `DEFAULT_TOKEN_BUDGET`.
- [ ] Tests: budget formula (floor + reserve) for LM Studio, default budget for Claude; per-model ctx persistence + reapply; Vitest slider clamp-to-max + Apply triggers the reload.

## Blocked by

- issues/0080-live-model-switch.md
