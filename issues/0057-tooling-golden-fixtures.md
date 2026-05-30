# P0 — Lock tool-calling behavior with golden fixtures

## Parent

`prd/0008-tool-calling-unification.md`

## What to build

Capture the *current* tool-calling behavior across all three divergent paths as golden fixtures, before any refactor touches production code. The three paths: Jarvis + LM Studio native (`message.tool_calls`), Jarvis + Claude CLI prompt-based (`{"tool_calls":[…]}` + brace repair), and the sub-agent action envelope (`{"action":"tool_call",…}`). Cover both well-formed calls and the malformed inputs each path handles today — including the broken-brace payloads `_repair_json_braces` currently salvages and the markdown-fenced envelope that fails the sub-agent parse. This slice changes no production code; it locks behavior so every later phase is regression-checked at PR time.

## Acceptance criteria

- [ ] Golden fixtures for well-formed tool calls on each of the three paths
- [ ] Golden fixtures for malformed calls: broken-brace cases (currently repaired by `_repair_json_braces`), markdown-fenced JSON, prose-wrapped JSON
- [ ] Fixtures asserted in `backend/tests/test_llm_client.py` and `backend/tests/test_sub_agent_v2_runner.py`
- [ ] No production code change
- [ ] Full backend check suite green (`uv run ruff check . && uv run ruff format --check . && uv run mypy . && uv run pytest`)

## Blocked by

None - can start immediately
