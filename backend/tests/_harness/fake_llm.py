"""Scriptable :class:`bob.llm_client.LLMClient` for integration tests.

:class:`FakeLLMClient` replays a pre-baked list of :class:`LLMResponse` /
``chat()`` strings in FIFO order, recording every call so the test can
inspect the messages list, schema and tool-call arguments after the fact.

This mirrors the inline ``FakeLLMClient`` already in
:mod:`tests.test_orchestrator`; we extract it here so every later slice's
contract tests can reuse the exact same scriptable client without the
copy/paste-and-evolve drift the existing tree already shows.

Issue 0043 only supports the existing ``complete()`` + ``chat()`` signatures.
The streaming + per-token tool-arguments modes that issue 0049 introduces
will be folded into this same client by extending it (NOT a parallel
class).
"""

from __future__ import annotations

from typing import Any

from bob.llm.types import LLMResponse, ToolDefinition
from bob.llm_client import LLMClient


class FakeLLMClient(LLMClient):
    """Replay scripted ``complete()`` and ``chat()`` responses.

    Construction:

    - ``complete_responses`` — list of :class:`LLMResponse` returned in FIFO
      order by :meth:`complete`. Each call pops one. An empty list when
      :meth:`complete` is invoked raises ``AssertionError`` so tests fail
      loudly on under-scripted scenarios.
    - ``chat_responses`` — same idea for :meth:`chat` but for raw strings.

    Recorded state:

    - ``complete_calls`` / ``chat_calls`` — every call's args (messages,
      tools/schema, session_id). Inspect these from tests to assert the
      orchestrator built the expected prompt.
    """

    def __init__(
        self,
        *,
        complete_responses: list[LLMResponse] | None = None,
        chat_responses: list[str] | None = None,
    ) -> None:
        self._complete_responses = list(complete_responses or [])
        self._chat_responses = list(chat_responses or [])
        self.chat_calls: list[dict[str, Any]] = []
        self.complete_calls: list[dict[str, Any]] = []

    async def chat(
        self,
        messages: list[dict[str, Any]],
        schema: dict[str, Any] | None = None,
        session_id: str | None = None,
    ) -> str:
        self.chat_calls.append({"messages": messages, "schema": schema, "session_id": session_id})
        if not self._chat_responses:
            raise AssertionError("FakeLLMClient ran out of canned chat() responses")
        return self._chat_responses.pop(0)

    async def complete(
        self,
        messages: list[dict[str, Any]],
        tools: list[ToolDefinition] | None = None,
        session_id: str | None = None,
    ) -> LLMResponse:
        self.complete_calls.append({"messages": messages, "tools": tools, "session_id": session_id})
        if not self._complete_responses:
            raise AssertionError("FakeLLMClient ran out of canned complete() responses")
        return self._complete_responses.pop(0)

    def queue_complete(self, response: LLMResponse) -> None:
        """Append a scripted response to the back of the ``complete()`` queue."""

        self._complete_responses.append(response)

    def queue_chat(self, response: str) -> None:
        """Append a scripted response to the back of the ``chat()`` queue."""

        self._chat_responses.append(response)
