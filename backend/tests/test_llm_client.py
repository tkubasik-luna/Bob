"""Tests for :mod:`bob.llm_client`."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from bob.config import Settings
from bob.llm_client import LMStudioClient


def _make_settings() -> Settings:
    return Settings(
        LLM_BASE_URL="http://localhost:1234/v1",
        LLM_MODEL="test-model",
        LLM_API_KEY="test-key",
        LLM_TIMEOUT_SECONDS=12.5,
    )


def _patch_openai(client: LMStudioClient, response_text: str = "hello back") -> AsyncMock:
    create = AsyncMock(
        return_value=SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content=response_text))]
        )
    )
    fake_chat = MagicMock()
    fake_chat.completions.create = create
    client._client = SimpleNamespace(chat=fake_chat)  # type: ignore[assignment]
    return create


@pytest.mark.asyncio
async def test_chat_calls_openai_with_expected_params() -> None:
    client = LMStudioClient(_make_settings())
    create = _patch_openai(client)

    messages: list[dict[str, Any]] = [{"role": "user", "content": "hi"}]
    result = await client.chat(messages=messages)

    assert result == "hello back"
    create.assert_awaited_once()
    assert create.await_args is not None
    kwargs = create.await_args.kwargs
    assert kwargs["model"] == "test-model"
    assert kwargs["messages"] == messages
    assert kwargs["timeout"] == 12.5
    assert "response_format" not in kwargs


@pytest.mark.asyncio
async def test_chat_with_schema_passes_response_format() -> None:
    client = LMStudioClient(_make_settings())
    create = _patch_openai(client, response_text='{"ok": true}')

    schema = {"name": "Reply", "schema": {"type": "object"}}
    messages: list[dict[str, Any]] = [{"role": "user", "content": "structured?"}]

    result = await client.chat(messages=messages, schema=schema)

    assert result == '{"ok": true}'
    assert create.await_args is not None
    kwargs = create.await_args.kwargs
    assert kwargs["response_format"] == {"type": "json_schema", "json_schema": schema}


@pytest.mark.asyncio
async def test_chat_logs_llm_call(monkeypatch: pytest.MonkeyPatch) -> None:
    client = LMStudioClient(_make_settings())
    create = AsyncMock(
        return_value=SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content="ok"))],
            usage=SimpleNamespace(prompt_tokens=11, completion_tokens=22),
        )
    )
    fake_chat = MagicMock()
    fake_chat.completions.create = create
    client._client = SimpleNamespace(chat=fake_chat)  # type: ignore[assignment]

    captured: dict[str, Any] = {}

    def _spy(**kwargs: Any) -> None:
        captured.update(kwargs)

    monkeypatch.setattr("bob.llm_client.log_llm_call", _spy)

    messages: list[dict[str, Any]] = [{"role": "user", "content": "hi"}]
    await client.chat(messages=messages, session_id="sess-42")

    assert captured["session_id"] == "sess-42"
    assert captured["messages"] == messages
    assert captured["raw_response"] == "ok"
    assert captured["tokens_in"] == 11
    assert captured["tokens_out"] == 22
    assert isinstance(captured["latency_ms"], float)


@pytest.mark.asyncio
async def test_chat_returns_empty_string_when_content_is_none() -> None:
    client = LMStudioClient(_make_settings())
    create = AsyncMock(
        return_value=SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content=None))]
        )
    )
    fake_chat = MagicMock()
    fake_chat.completions.create = create
    client._client = SimpleNamespace(chat=fake_chat)  # type: ignore[assignment]

    result = await client.chat(messages=[{"role": "user", "content": "hi"}])
    assert result == ""
