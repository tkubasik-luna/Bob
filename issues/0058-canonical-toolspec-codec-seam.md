# P1 — Canonical ToolSpec + codec seam

## Parent

`prd/0008-tool-calling-unification.md`

## What to build

Introduce the codec abstraction: a single canonical `ToolSpec` (its `parameters` derived from the Pydantic `args_model` via `model_json_schema()`), a `ToolCodec` protocol exposing `inject(messages, tools)` and `parse(raw | stream) -> ToolCall[]`, a `BackendCapability` descriptor, and `select_codec()`. Extract the existing native LM Studio path into `NativeToolCodec` and route the Jarvis + LM Studio call site through it. Behavior-preserving — the 0057 golden fixtures must stay byte-identical green. This is the seam "core owns the loop, codec owns the format"; call sites stop seeing wire-format details.

## Acceptance criteria

- [ ] New package `bob/llm/tooling/` with `ToolSpec`, `ToolCodec` protocol, `BackendCapability`, `select_codec()`
- [ ] `NativeToolCodec` extracted; Jarvis + LM Studio native path routed through it
- [ ] `LLM_TOOL_MODE` config (`auto` default) selects codec by capability with no per-call branching
- [ ] Streaming preserved byte-identical (`PartialJsonParser` → `speech_delta`)
- [ ] 0057 golden fixtures green; `mypy`/`ruff`/`pytest` pass

## Blocked by

- `issues/0057-tooling-golden-fixtures.md`
