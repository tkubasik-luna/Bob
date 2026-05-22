# LLM tool-calling smoke test

Manual smoke procedure for the unified `LLMClient.complete(messages, tools=None)`
API exposed by `bob.llm_client`. The two backends — LM Studio and Claude CLI —
implement the same `complete()` contract but reach the model via different
protocols, so each one is checked separately.

The smoke is currently exercised from a Python REPL because no production code
calls `complete()` yet (this slice is API-only).

## Prerequisites

- `backend/.venv` populated via `uv sync`.
- LM Studio: a function-calling-capable model loaded (e.g. **Qwen2.5 7B
  Instruct**, **Llama 3.1 8B Instruct**) on `http://localhost:1234/v1`. Set
  `LLM_BASE_URL`, `LLM_MODEL`, `LLM_API_KEY` in `.env` at repo root.
- Claude CLI: `claude` binary on `PATH` and authenticated
  (`claude auth status`).

## Shared snippet

```python
import asyncio
from bob.config import get_settings
from bob.llm import ToolDefinition
from bob.llm_client import LMStudioClient, ClaudeCliClient

settings = get_settings()
spawn = ToolDefinition(
    name="spawn_subtask",
    description="Spawn a background subtask with a short title.",
    parameters={
        "type": "object",
        "properties": {"title": {"type": "string"}},
        "required": ["title"],
    },
)

messages = [
    {"role": "system", "content": "You are Bob, a personal assistant."},
    {
        "role": "user",
        "content": "I need to buy milk later — log that as a subtask.",
    },
]
```

## LM Studio smoke

```python
client = LMStudioClient(settings)
resp = asyncio.run(client.complete(messages=messages, tools=[spawn]))
print(resp)
```

Expected:

- `resp.is_tool_call is True`.
- `resp.tool_calls[0].name == "spawn_subtask"`.
- `resp.tool_calls[0].arguments` is a dict with a `title` key.

Then re-run without `tools` and confirm the response is plain text:

```python
resp = asyncio.run(client.complete(messages=messages))
assert resp.text is not None and resp.tool_calls == []
```

## Claude CLI smoke

```python
client = ClaudeCliClient(settings)  # works regardless of LLM_PROVIDER value
resp = asyncio.run(client.complete(messages=messages, tools=[spawn]))
print(resp)
```

Expected:

- Same as LM Studio: `resp.tool_calls[0].name == "spawn_subtask"`.

Then ask a question the model can answer directly (no tool needed):

```python
resp = asyncio.run(
    client.complete(
        messages=[{"role": "user", "content": "Say hi in French."}],
        tools=[spawn],
    )
)
assert resp.text is not None and resp.tool_calls == []
```

## Failure modes to eyeball

- LM Studio returns `tool_calls` with non-JSON `arguments` → `LLMClientError`.
- Claude CLI returns a JSON-shaped reply with `tool_calls` missing `name` →
  `LLMClientError`.
- Both backends with `tools=None` → response always treated as plain text.

The unit suite (`tests/test_llm_complete.py`) covers these via mocks; the
manual smoke just confirms the wiring against a live model.
