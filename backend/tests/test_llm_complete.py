"""Tests for the unified ``LLMClient.complete()`` tool-calling API.

Both backends are exercised against mocks:

- :class:`LMStudioClient` against a stubbed ``openai.AsyncOpenAI`` ``chat``
  endpoint.
- :class:`ClaudeCliClient` against a monkey-patched ``chat()`` method (the
  surface the JSON-in-system-prompt protocol layers on top of).
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from bob.config import Settings
from bob.llm import LLMResponse, ToolCall, ToolDefinition
from bob.llm_client import (
    ClaudeCliClient,
    LLMClientError,
    LMStudioClient,
)


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
    """Issue 0061 — the CLI parses a Nous-Hermes ``<tool_call>`` reply.

    Also asserts the codec injected a ``<tools>`` block into the system
    message (the trained advertisement format) — the old hand-written
    ``{"tool_calls":[…]}`` addendum is gone.
    """

    client = ClaudeCliClient(_claude_settings())
    raw_reply = (
        '<tool_call>{"id": "call_99", "name": "spawn_subtask", '
        '"arguments": {"title": "buy milk"}}</tool_call>'
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
    # Hermes <tools> block advertises the tool; emission protocol uses
    # <tool_call> tags (not the deleted "tool_calls" JSON wrapper).
    assert "spawn_subtask" in sent_system["content"]
    assert "<tools>" in sent_system["content"]
    assert "<tool_call>" in sent_system["content"]
    # The original system text is preserved (block is appended, not replaced).
    assert sent_system["content"].startswith("you are bob")


@pytest.mark.asyncio
async def test_claude_cli_complete_does_not_mutate_caller_messages(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The Hermes inject works on a copy — the caller's list is untouched."""

    client = ClaudeCliClient(_claude_settings())

    async def _fake_chat(
        messages: list[dict[str, Any]],
        schema: dict[str, Any] | None = None,
        session_id: str | None = None,
    ) -> str:
        return "plain text"

    monkeypatch.setattr(client, "chat", _fake_chat)

    original = [{"role": "system", "content": "you are bob"}, {"role": "user", "content": "go"}]
    await client.complete(messages=original, tools=[_spawn_tool()])

    # Caller's system message must not have grown a <tools> block.
    assert original[0]["content"] == "you are bob"
    assert "<tools>" not in original[0]["content"]


@pytest.mark.asyncio
async def test_claude_cli_complete_parses_tool_call_with_trailing_prose(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Claude often appends a confirmation sentence after the tool-call block.

    The ``<root>`` wrap + ``<tool_call>`` span extraction recover the call
    regardless of the surrounding narration, so the tool call is not dropped
    (the failure that pre-0061 forced a fall-through to the text path).
    """

    client = ClaudeCliClient(_claude_settings())
    payload = (
        '<tool_call>{"id": "call_1", "name": "spawn_subtask", '
        '"arguments": {"title": "Draft email", "goal": "Write three variants."}}</tool_call>'
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
async def test_claude_cli_complete_recovers_single_quoted_py_dict(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Issue 0061 — a Python-dict (single-quoted) body recovers via ast.literal_eval.

    Replaces the pre-0061 brace-repair regression: the tolerant chain's
    ``ast.literal_eval`` rung decodes a body the strict JSON rung rejects,
    so a deeply-nested ``say`` still resolves without any brace counting.
    """

    client = ClaudeCliClient(_claude_settings())
    body = (
        "{'name': 'say', 'arguments': {'speech': 'Bitcoin, en bref', "
        "'ui': {'component': 'Markdown', 'props': {'content': 'rare et cher'}}}}"
    )
    reply = f"<tool_call>{body}</tool_call>"

    async def _fake_chat(
        messages: list[dict[str, Any]],
        schema: dict[str, Any] | None = None,
        session_id: str | None = None,
    ) -> str:
        return reply

    monkeypatch.setattr(client, "chat", _fake_chat)

    response = await client.complete(
        messages=[{"role": "user", "content": "parle-moi du bitcoin"}],
        tools=[_spawn_tool()],
    )

    assert response.is_tool_call is True
    assert len(response.tool_calls) == 1
    call = response.tool_calls[0]
    assert call.name == "say"
    assert call.arguments["speech"] == "Bitcoin, en bref"
    assert call.arguments["ui"]["props"]["content"] == "rare et cher"


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
    """Issue 0061 — a ```` ```json ```` fence INSIDE the ``<tool_call>`` body unwraps."""

    client = ClaudeCliClient(_claude_settings())
    body = '{"id": "c1", "name": "spawn_subtask", "arguments": {}}'
    fenced = f"<tool_call>\n```json\n{body}\n```\n</tool_call>"

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
async def test_claude_cli_complete_garbled_tool_call_degrades_to_text(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Issue 0061 — a ``<tool_call>`` the chain cannot decode degrades to text.

    Pre-0061 a non-list / nameless ``{"tool_calls":…}`` raised
    ``LLMClientError``. Under Hermes a span none of the tolerant rungs decode
    is simply skipped, the reply is surfaced as plain text, and recovery of a
    still-malformed call is deferred to the self-correction loop (issue 0062).
    """

    client = ClaudeCliClient(_claude_settings())
    garbled = '<tool_call>{"name": "say", "arg</tool_call>'

    async def _fake_chat(
        messages: list[dict[str, Any]],
        schema: dict[str, Any] | None = None,
        session_id: str | None = None,
    ) -> str:
        return garbled

    monkeypatch.setattr(client, "chat", _fake_chat)

    response = await client.complete(
        messages=[{"role": "user", "content": "go"}],
        tools=[_spawn_tool()],
    )

    assert response.is_tool_call is False
    assert response.tool_calls == []
    assert response.text == garbled


@pytest.mark.asyncio
async def test_claude_cli_complete_nameless_tool_call_skipped(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A decodable ``<tool_call>`` body with no ``name`` is not a dispatchable call.

    The span decodes (valid JSON) but carries no ``name``; the codec skips it,
    so the reply degrades to text rather than raising (pre-0061 this raised
    ``missing 'name'``).
    """

    client = ClaudeCliClient(_claude_settings())
    nameless = '<tool_call>{"arguments": {}}</tool_call>'

    async def _fake_chat(
        messages: list[dict[str, Any]],
        schema: dict[str, Any] | None = None,
        session_id: str | None = None,
    ) -> str:
        return nameless

    monkeypatch.setattr(client, "chat", _fake_chat)

    response = await client.complete(
        messages=[{"role": "user", "content": "go"}],
        tools=[_spawn_tool()],
    )

    assert response.is_tool_call is False
    assert response.tool_calls == []


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
        return '<tool_call>{"name": "spawn_subtask", "arguments": {"title": "x"}}</tool_call>'

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
        # Even if the model happens to return a tool-call-shaped reply, we
        # don't ask for tools so we should pass it through as text.
        return '<tool_call>{"name": "x", "arguments": {}}</tool_call>'

    monkeypatch.setattr(client, "chat", _fake_chat)

    response = await client.complete(messages=[{"role": "user", "content": "hi"}])

    assert response.is_tool_call is False
    assert response.text is not None
    assert response.tool_calls == []
    # No <tools> block injected when tools is None.
    assert captured_messages["messages"] == [{"role": "user", "content": "hi"}]


def test_claude_cli_inject_builds_tools_block_with_schema() -> None:
    """The Hermes codec advertises the tool's schema in a ``<tools>`` block.

    Replaces the deleted ``_build_tools_system_addendum`` unit test — the
    injected block carries the name, description and JSON Schema and tells the
    model to emit ``<tool_call>`` tags.
    """

    from bob.llm.tooling import ToolSpec

    messages: list[dict[str, Any]] = [{"role": "system", "content": "base"}]
    client = ClaudeCliClient(_claude_settings())
    client._tool_codec.inject(messages, [ToolSpec.from_tool_definition(_spawn_tool())])

    system = messages[0]["content"]
    assert "spawn_subtask" in system
    assert "Spawn a background subtask." in system
    assert '"required": ["title"]' in system
    assert "<tools>" in system and "</tools>" in system
    assert "<tool_call>" in system


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
