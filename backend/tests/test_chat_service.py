"""Tests for :mod:`bob.chat_service`."""

from __future__ import annotations

import json
from typing import Any

import pytest

from bob.chat_service import ChatService
from bob.conversation import ConversationStore
from bob.llm_client import LLMClient


class FakeLLMClient(LLMClient):
    """Returns canned responses in order; records every call."""

    def __init__(self, responses: list[str]) -> None:
        self._responses = list(responses)
        self.calls: list[dict[str, Any]] = []

    async def chat(
        self,
        messages: list[dict[str, Any]],
        schema: dict[str, Any] | None = None,
    ) -> str:
        self.calls.append({"messages": messages, "schema": schema})
        if not self._responses:
            raise AssertionError("FakeLLMClient ran out of canned responses")
        return self._responses.pop(0)


def _valid_payload(speech: str = "Bonjour Tom") -> str:
    return json.dumps({"speech": speech, "ui": []})


def _make_service(responses: list[str]) -> tuple[ChatService, FakeLLMClient, ConversationStore]:
    client = FakeLLMClient(responses)
    store = ConversationStore()
    service = ChatService(llm_client=client, conversation=store)
    return service, client, store


@pytest.mark.asyncio
async def test_single_exchange_appends_user_and_assistant() -> None:
    service, _client, store = _make_service([_valid_payload("Salut")])

    parsed = await service.handle_user_message("s1", "Coucou")

    assert parsed.speech == "Salut"
    assert store.get_history("s1") == [
        {"role": "user", "content": "Coucou"},
        {"role": "assistant", "content": "Salut"},
    ]


@pytest.mark.asyncio
async def test_system_prompt_is_first_message_sent_to_llm() -> None:
    service, client, _store = _make_service([_valid_payload()])

    await service.handle_user_message("s1", "Hello")

    sent = client.calls[0]["messages"]
    assert sent[0]["role"] == "system"
    assert "Bob" in sent[0]["content"]
    # components_description placeholder must be rendered (not left literal).
    assert "{components_description}" not in sent[0]["content"]
    assert "ChatMessage" in sent[0]["content"]
    # User message follows the system prompt.
    assert sent[1] == {"role": "user", "content": "Hello"}
    # Schema is forwarded.
    assert client.calls[0]["schema"] is not None


@pytest.mark.asyncio
async def test_parser_retry_does_not_pollute_conversation() -> None:
    # First call returns garbage → parser will retry once with a correction
    # message; that correction must stay local to the parser.
    service, client, store = _make_service(["not json at all", _valid_payload("ok")])

    parsed = await service.handle_user_message("s1", "Ping")

    assert parsed.speech == "ok"
    history = store.get_history("s1")
    assert history == [
        {"role": "user", "content": "Ping"},
        {"role": "assistant", "content": "ok"},
    ]
    # The parser issued exactly one retry call to the LLM.
    assert len(client.calls) == 2


@pytest.mark.asyncio
async def test_fallback_when_both_attempts_invalid() -> None:
    raw_first = "garbage first"
    raw_retry = "garbage retry"
    service, client, store = _make_service([raw_first, raw_retry])

    parsed = await service.handle_user_message("s1", "Question")

    # Fallback: speech is the raw first attempt, ui is empty.
    assert parsed.speech == raw_first
    assert parsed.ui == []

    history = store.get_history("s1")
    assert history == [
        {"role": "user", "content": "Question"},
        {"role": "assistant", "content": raw_first},
    ]
    assert len(client.calls) == 2
