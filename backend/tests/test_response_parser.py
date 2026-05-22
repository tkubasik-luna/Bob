"""Tests for :mod:`bob.response_parser`."""

from __future__ import annotations

import json
from typing import Any

import pytest

from bob.llm.types import LLMResponse, ToolDefinition
from bob.llm_client import LLMClient
from bob.response_parser import parse


class FakeLLMClient(LLMClient):
    """Returns pre-canned responses in order; records every call."""

    def __init__(self, responses: list[str]) -> None:
        self._responses = list(responses)
        self.calls: list[dict[str, Any]] = []

    async def chat(
        self,
        messages: list[dict[str, Any]],
        schema: dict[str, Any] | None = None,
        session_id: str | None = None,
    ) -> str:
        self.calls.append({"messages": messages, "schema": schema, "session_id": session_id})
        if not self._responses:
            raise AssertionError("FakeLLMClient ran out of canned responses")
        return self._responses.pop(0)

    async def complete(
        self,
        messages: list[dict[str, Any]],
        tools: list[ToolDefinition] | None = None,
        session_id: str | None = None,
    ) -> LLMResponse:
        raise NotImplementedError("FakeLLMClient does not exercise complete()")


def _valid_payload_str() -> str:
    return json.dumps(
        {
            "speech": "Bonjour",
            "ui": [
                {
                    "component": "ChatMessage",
                    "props": {"role": "assistant", "content": "Bonjour"},
                }
            ],
        }
    )


@pytest.mark.asyncio
async def test_parse_happy_path_no_retry() -> None:
    client = FakeLLMClient([])
    raw = _valid_payload_str()
    parsed = await parse(raw, client, messages_so_far=[{"role": "user", "content": "hi"}])
    assert parsed.speech == "Bonjour"
    assert len(parsed.ui) == 1
    assert parsed.ui[0].component == "ChatMessage"
    assert client.calls == []


@pytest.mark.asyncio
async def test_parse_json_syntax_error_retry_succeeds() -> None:
    valid = _valid_payload_str()
    client = FakeLLMClient([valid])
    raw = "{ not valid json"
    messages_so_far: list[dict[str, Any]] = [{"role": "user", "content": "salut"}]

    parsed = await parse(raw, client, messages_so_far=messages_so_far)

    assert parsed.speech == "Bonjour"
    assert len(client.calls) == 1
    call = client.calls[0]
    # Retry should include the original assistant reply + the correction message.
    sent = call["messages"]
    assert sent[: len(messages_so_far)] == messages_so_far
    assert sent[-2] == {"role": "assistant", "content": raw}
    assert sent[-1]["role"] == "user"
    assert "invalide" in sent[-1]["content"]
    assert "schéma" in sent[-1]["content"]
    assert call["schema"] is not None


@pytest.mark.asyncio
async def test_parse_schema_violation_retry_succeeds() -> None:
    bad_payload = json.dumps(
        {
            "speech": "Hi",
            "ui": [{"component": "NotAComponent", "props": {}}],
        }
    )
    client = FakeLLMClient([_valid_payload_str()])

    parsed = await parse(bad_payload, client, messages_so_far=[])

    assert parsed.speech == "Bonjour"
    assert len(client.calls) == 1
    assert client.calls[0]["schema"] is not None


@pytest.mark.asyncio
async def test_parse_both_attempts_fail_returns_fallback() -> None:
    raw_first = "totally not json"
    raw_retry = "still not json"
    client = FakeLLMClient([raw_retry])

    parsed = await parse(raw_first, client, messages_so_far=[])

    assert parsed.speech == raw_first
    assert parsed.ui == []
    assert len(client.calls) == 1
