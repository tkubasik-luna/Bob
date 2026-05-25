"""Tests for the unified ``LLMClient.complete()`` tool-calling API.

Both backends are exercised against mocks:

- :class:`LMStudioClient` against a stubbed ``openai.AsyncOpenAI`` ``chat``
  endpoint.
- :class:`ClaudeCliClient` against a monkey-patched ``chat()`` method (the
  surface the JSON-in-system-prompt protocol layers on top of).
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from bob.config import Settings
from bob.llm import LLMResponse, ToolCall, ToolDefinition
from bob.llm_client import ClaudeCliClient, LLMClientError, LMStudioClient


def _make_lm_settings() -> Settings:
    return Settings(
        LLM_BASE_URL="http://localhost:1234/v1",
        LLM_MODEL="test-model",
        LLM_API_KEY="test-key",
        LLM_TIMEOUT_SECONDS=12.5,
    )


def _claude_settings(**overrides: Any) -> Settings:
    base: dict[str, Any] = {
        "LLM_PROVIDER": "claude_cli",
        "CLAUDE_CLI_BIN": "claude",
        "CLAUDE_CLI_TIMEOUT_SECONDS": 30.0,
    }
    base.update(overrides)
    return Settings(_env_file=None, **base)  # type: ignore[call-arg]


def _spawn_tool() -> ToolDefinition:
    return ToolDefinition(
        name="spawn_subtask",
        description="Spawn a background subtask.",
        parameters={
            "type": "object",
            "properties": {"title": {"type": "string"}},
            "required": ["title"],
        },
    )


def _patch_openai_completion(
    client: LMStudioClient,
    *,
    content: str | None = None,
    tool_calls: list[Any] | None = None,
    reasoning_content: str | None = None,
) -> AsyncMock:
    message_kwargs: dict[str, Any] = {
        "content": content,
        "tool_calls": tool_calls,
    }
    if reasoning_content is not None:
        message_kwargs["reasoning_content"] = reasoning_content
    create = AsyncMock(
        return_value=SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(**message_kwargs))],
            usage=SimpleNamespace(prompt_tokens=5, completion_tokens=9),
        )
    )
    fake_chat = MagicMock()
    fake_chat.completions.create = create
    client._client = SimpleNamespace(chat=fake_chat)  # type: ignore[assignment]
    return create


# ---------------------------------------------------------------------------
# LMStudioClient.complete
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_lm_studio_complete_returns_tool_calls() -> None:
    client = LMStudioClient(_make_lm_settings())
    create = _patch_openai_completion(
        client,
        tool_calls=[
            SimpleNamespace(
                id="call_abc",
                type="function",
                function=SimpleNamespace(
                    name="spawn_subtask",
                    arguments='{"title": "buy milk"}',
                ),
            )
        ],
    )

    response = await client.complete(
        messages=[{"role": "user", "content": "spawn it"}],
        tools=[_spawn_tool()],
    )

    assert isinstance(response, LLMResponse)
    assert response.is_tool_call is True
    assert response.text is None
    assert response.tool_calls == [
        ToolCall(id="call_abc", name="spawn_subtask", arguments={"title": "buy milk"})
    ]

    kwargs = create.await_args.kwargs  # type: ignore[union-attr]
    assert kwargs["tool_choice"] == "auto"
    assert kwargs["tools"] == [
        {
            "type": "function",
            "function": {
                "name": "spawn_subtask",
                "description": "Spawn a background subtask.",
                "parameters": _spawn_tool().parameters,
            },
        }
    ]


@pytest.mark.asyncio
async def test_lm_studio_complete_returns_plain_text_when_no_tool_call() -> None:
    client = LMStudioClient(_make_lm_settings())
    _patch_openai_completion(client, content="just answering directly")

    response = await client.complete(
        messages=[{"role": "user", "content": "hi"}],
        tools=[_spawn_tool()],
    )

    assert response.is_tool_call is False
    assert response.text == "just answering directly"
    assert response.tool_calls == []


@pytest.mark.asyncio
async def test_lm_studio_complete_without_tools_omits_tools_kwarg() -> None:
    client = LMStudioClient(_make_lm_settings())
    create = _patch_openai_completion(client, content="hello")

    response = await client.complete(messages=[{"role": "user", "content": "hi"}])

    assert response.text == "hello"
    assert response.tool_calls == []
    kwargs = create.await_args.kwargs  # type: ignore[union-attr]
    assert "tools" not in kwargs
    assert "tool_choice" not in kwargs


@pytest.mark.asyncio
async def test_lm_studio_complete_raises_on_malformed_arguments() -> None:
    client = LMStudioClient(_make_lm_settings())
    _patch_openai_completion(
        client,
        tool_calls=[
            SimpleNamespace(
                id="call_xyz",
                type="function",
                function=SimpleNamespace(
                    name="spawn_subtask",
                    arguments="this-is-not-json",
                ),
            )
        ],
    )

    with pytest.raises(LLMClientError, match="not valid JSON"):
        await client.complete(
            messages=[{"role": "user", "content": "go"}],
            tools=[_spawn_tool()],
        )


@pytest.mark.asyncio
async def test_lm_studio_complete_raises_on_empty_response() -> None:
    client = LMStudioClient(_make_lm_settings())
    _patch_openai_completion(client, content=None)

    with pytest.raises(LLMClientError, match="empty response"):
        await client.complete(messages=[{"role": "user", "content": "hi"}])


@pytest.mark.asyncio
async def test_lm_studio_complete_logs_llm_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = LMStudioClient(_make_lm_settings())
    _patch_openai_completion(client, content="hi there")

    captured: dict[str, Any] = {}

    def _spy(**kwargs: Any) -> None:
        captured.update(kwargs)

    monkeypatch.setattr("bob.llm_client.log_llm_call", _spy)

    await client.complete(
        messages=[{"role": "user", "content": "hi"}],
        session_id="sess-1",
    )

    assert captured["session_id"] == "sess-1"
    assert captured["raw_response"] == "hi there"
    assert captured["tokens_in"] == 5
    assert captured["tokens_out"] == 9
    assert isinstance(captured["latency_ms"], float)


@pytest.mark.asyncio
async def test_lm_studio_complete_generates_id_when_provider_omits_it() -> None:
    client = LMStudioClient(_make_lm_settings())
    _patch_openai_completion(
        client,
        tool_calls=[
            SimpleNamespace(
                id=None,
                type="function",
                function=SimpleNamespace(name="spawn_subtask", arguments="{}"),
            )
        ],
    )

    response = await client.complete(
        messages=[{"role": "user", "content": "go"}],
        tools=[_spawn_tool()],
    )

    assert len(response.tool_calls) == 1
    assert response.tool_calls[0].id.startswith("call_")
    assert response.tool_calls[0].arguments == {}


# ---------------------------------------------------------------------------
# ClaudeCliClient.complete
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_claude_cli_complete_parses_tool_call_json(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = ClaudeCliClient(_claude_settings())
    raw_reply = json.dumps(
        {
            "tool_calls": [
                {
                    "id": "call_99",
                    "name": "spawn_subtask",
                    "arguments": {"title": "buy milk"},
                }
            ]
        }
    )
    captured_messages: dict[str, Any] = {}

    async def _fake_chat(
        messages: list[dict[str, Any]],
        schema: dict[str, Any] | None = None,
        session_id: str | None = None,
    ) -> str:
        captured_messages["messages"] = messages
        return raw_reply

    monkeypatch.setattr(client, "chat", _fake_chat)

    response = await client.complete(
        messages=[
            {"role": "system", "content": "you are bob"},
            {"role": "user", "content": "go"},
        ],
        tools=[_spawn_tool()],
    )

    assert response.is_tool_call is True
    assert response.text is None
    assert response.tool_calls == [
        ToolCall(
            id="call_99",
            name="spawn_subtask",
            arguments={"title": "buy milk"},
        )
    ]
    sent_system = captured_messages["messages"][0]
    assert sent_system["role"] == "system"
    assert "spawn_subtask" in sent_system["content"]
    assert "tool_calls" in sent_system["content"]


@pytest.mark.asyncio
async def test_claude_cli_complete_parses_tool_call_with_trailing_prose(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Claude often appends a confirmation sentence after the tool-call JSON.

    Regression for slice #0003: ``json.loads`` on the full reply fails when
    Claude tacks on natural-language text after the closing brace, which
    caused the tool call to be silently dropped and the orchestrator to fall
    through to the structured-output text path (no task spawned).
    """

    client = ClaudeCliClient(_claude_settings())
    payload = (
        '{"tool_calls": [{"id": "call_1", "name": "spawn_subtask", '
        '"arguments": {"title": "Draft email", "goal": "Write three variants."}}]}'
        "\n\nTâche lancée. Résultat dans quelques instants."
    )

    async def _fake_chat(
        messages: list[dict[str, Any]],
        schema: dict[str, Any] | None = None,
        session_id: str | None = None,
    ) -> str:
        return payload

    monkeypatch.setattr(client, "chat", _fake_chat)

    response = await client.complete(
        messages=[{"role": "user", "content": "draft"}],
        tools=[_spawn_tool()],
    )

    assert response.is_tool_call is True
    assert response.text is None
    assert len(response.tool_calls) == 1
    call = response.tool_calls[0]
    assert call.name == "spawn_subtask"
    assert call.arguments == {"title": "Draft email", "goal": "Write three variants."}


@pytest.mark.asyncio
async def test_claude_cli_complete_returns_text_when_model_skips_tool(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = ClaudeCliClient(_claude_settings())

    async def _fake_chat(
        messages: list[dict[str, Any]],
        schema: dict[str, Any] | None = None,
        session_id: str | None = None,
    ) -> str:
        return "I don't need any tools, thanks."

    monkeypatch.setattr(client, "chat", _fake_chat)

    response = await client.complete(
        messages=[{"role": "user", "content": "hi"}],
        tools=[_spawn_tool()],
    )

    assert response.is_tool_call is False
    assert response.text == "I don't need any tools, thanks."
    assert response.tool_calls == []


@pytest.mark.asyncio
async def test_claude_cli_complete_strips_markdown_fence_around_tool_calls(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = ClaudeCliClient(_claude_settings())
    payload = '{"tool_calls": [{"id": "c1", "name": "spawn_subtask", "arguments": {}}]}'
    fenced = f"```json\n{payload}\n```"

    async def _fake_chat(
        messages: list[dict[str, Any]],
        schema: dict[str, Any] | None = None,
        session_id: str | None = None,
    ) -> str:
        return fenced

    monkeypatch.setattr(client, "chat", _fake_chat)

    response = await client.complete(
        messages=[{"role": "user", "content": "go"}],
        tools=[_spawn_tool()],
    )

    assert response.is_tool_call is True
    assert response.tool_calls[0].name == "spawn_subtask"


@pytest.mark.asyncio
async def test_claude_cli_complete_raises_on_malformed_tool_calls(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = ClaudeCliClient(_claude_settings())

    async def _fake_chat(
        messages: list[dict[str, Any]],
        schema: dict[str, Any] | None = None,
        session_id: str | None = None,
    ) -> str:
        return json.dumps({"tool_calls": "not-a-list"})

    monkeypatch.setattr(client, "chat", _fake_chat)

    with pytest.raises(LLMClientError, match="malformed tool call"):
        await client.complete(
            messages=[{"role": "user", "content": "go"}],
            tools=[_spawn_tool()],
        )


@pytest.mark.asyncio
async def test_claude_cli_complete_raises_when_entry_missing_name(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = ClaudeCliClient(_claude_settings())

    async def _fake_chat(
        messages: list[dict[str, Any]],
        schema: dict[str, Any] | None = None,
        session_id: str | None = None,
    ) -> str:
        return json.dumps({"tool_calls": [{"arguments": {}}]})

    monkeypatch.setattr(client, "chat", _fake_chat)

    with pytest.raises(LLMClientError, match="missing 'name'"):
        await client.complete(
            messages=[{"role": "user", "content": "go"}],
            tools=[_spawn_tool()],
        )


@pytest.mark.asyncio
async def test_claude_cli_complete_generates_id_when_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = ClaudeCliClient(_claude_settings())

    async def _fake_chat(
        messages: list[dict[str, Any]],
        schema: dict[str, Any] | None = None,
        session_id: str | None = None,
    ) -> str:
        return json.dumps({"tool_calls": [{"name": "spawn_subtask", "arguments": {"title": "x"}}]})

    monkeypatch.setattr(client, "chat", _fake_chat)

    response = await client.complete(
        messages=[{"role": "user", "content": "go"}],
        tools=[_spawn_tool()],
    )

    assert len(response.tool_calls) == 1
    assert response.tool_calls[0].id.startswith("call_")


@pytest.mark.asyncio
async def test_claude_cli_complete_without_tools_returns_text(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When ``tools`` is omitted the response is always treated as plain text."""

    client = ClaudeCliClient(_claude_settings())
    captured_messages: dict[str, Any] = {}

    async def _fake_chat(
        messages: list[dict[str, Any]],
        schema: dict[str, Any] | None = None,
        session_id: str | None = None,
    ) -> str:
        captured_messages["messages"] = messages
        # Even if the model happens to return a tool-call-shaped JSON, we
        # don't ask for tools so we should pass it through as text.
        return '{"tool_calls": [{"name": "x", "arguments": {}}]}'

    monkeypatch.setattr(client, "chat", _fake_chat)

    response = await client.complete(messages=[{"role": "user", "content": "hi"}])

    assert response.is_tool_call is False
    assert response.text is not None
    assert response.tool_calls == []
    # No system addendum injected when tools is None.
    assert captured_messages["messages"] == [{"role": "user", "content": "hi"}]


def test_claude_cli_build_tools_system_addendum_contains_schema() -> None:
    addendum = ClaudeCliClient._build_tools_system_addendum([_spawn_tool()])
    assert "spawn_subtask" in addendum
    assert "Spawn a background subtask." in addendum
    assert '"required": ["title"]' in addendum
    assert "tool_calls" in addendum


# ---------------------------------------------------------------------------
# Slice 0039 — debug event instrumentation on LMStudioClient.complete
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_lm_studio_complete_emits_start_and_end_debug_events() -> None:
    from bob import debug_log

    debug_log.clear()
    debug_log.current_turn_id.set(None)

    client = LMStudioClient(_make_lm_settings())
    _patch_openai_completion(client, content="hi")
    await client.complete(messages=[{"role": "user", "content": "ping"}])

    llm_events = [e for e in debug_log.snapshot() if e.category == "llm"]
    assert len(llm_events) == 2
    start, end = llm_events

    assert start.summary.startswith("LLM call démarré")
    assert end.summary.startswith("LLM call terminé")
    # Same correlation_id pairs the two events.
    assert start.correlation_id is not None
    assert start.correlation_id == end.correlation_id

    # Start payload carries messages + model.
    assert "messages" in start.payload
    assert start.payload["model"] == "test-model"
    # End payload carries response + latency + tokens.
    assert end.payload["response"] == "hi"
    assert isinstance(end.payload["latency_ms"], float)
    assert end.payload["tokens_in"] == 5
    assert end.payload["tokens_out"] == 9


@pytest.mark.asyncio
async def test_lm_studio_complete_emits_error_end_event_on_exception() -> None:
    from bob import debug_log

    debug_log.clear()
    debug_log.current_turn_id.set(None)

    client = LMStudioClient(_make_lm_settings())

    boom = RuntimeError("network down")

    async def _explode(**_kw: Any) -> Any:
        raise boom

    fake_chat = MagicMock()
    fake_chat.completions.create = _explode
    client._client = SimpleNamespace(chat=fake_chat)  # type: ignore[assignment]

    with pytest.raises(RuntimeError, match="network down"):
        await client.complete(messages=[{"role": "user", "content": "x"}])

    llm_events = [e for e in debug_log.snapshot() if e.category == "llm"]
    assert len(llm_events) == 2
    start, end = llm_events
    assert start.summary.startswith("LLM call démarré")
    assert start.severity == "info"
    assert start.correlation_id == end.correlation_id
    assert end.severity == "error"
    assert end.summary.startswith("LLM call échoué")
    assert "network down" in end.payload["exception"]
    assert end.payload["exception_type"] == "RuntimeError"
    assert "traceback" in end.payload
    assert isinstance(end.payload["latency_ms"], float)


@pytest.mark.asyncio
async def test_lm_studio_chat_emits_start_and_end_debug_events() -> None:
    """``LLMClient.chat`` is also instrumented (used by sub-agents + retries)."""

    from bob import debug_log

    debug_log.clear()
    debug_log.current_turn_id.set(None)

    client = LMStudioClient(_make_lm_settings())
    _patch_openai_completion(client, content="bonjour")
    out = await client.chat(messages=[{"role": "user", "content": "salut"}])
    assert out == "bonjour"

    llm_events = [e for e in debug_log.snapshot() if e.category == "llm"]
    assert len(llm_events) == 2
    start, end = llm_events
    assert start.summary.startswith("LLM call démarré")
    assert end.summary.startswith("LLM call terminé")
    assert start.correlation_id == end.correlation_id
    assert end.payload["response"] == "bonjour"
