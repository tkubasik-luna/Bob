# P3 — Guided-JSON envelope for the sub-agent (LM Studio)

## Parent

`prd/0008-tool-calling-unification.md`

## What to build

On LM Studio, emit the sub-agent control envelope (`progress` / `tool_call` / `done`, the `SubAgentAction` union) under `response_format: {"type":"json_schema", …}` guided decoding. This removes the `json.loads`-and-pray path (`runner.py:321`) on LM Studio — the envelope becomes valid by construction. Directly fixes the production failure observed in `backend/logs/orchestration.jsonl` (2026-05-28) where a local model emitted a markdown-fenced `progress` action and the task died `llm_failed`.

## Acceptance criteria

- [ ] Sub-agent `chat()` passes the `SubAgentAction` schema as `response_format` on LM Studio
- [ ] Fenced / prose-wrapped envelope failures no longer reachable on LM Studio (constrained decode)
- [ ] Live smoke: Gmail-search task end-to-end on a local model (Qwen 2.5 7B/14B or Llama 3.1 8B) with no parse failure
- [ ] Non-guided backends (Claude CLI) behave unchanged
- [ ] Tests cover the guided-envelope path

## Blocked by

- `issues/0059-subagent-tool-args-schema.md`
