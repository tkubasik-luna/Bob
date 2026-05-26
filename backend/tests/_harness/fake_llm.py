"""Scriptable :class:`bob.llm_client.LLMClient` for integration tests.

:class:`FakeLLMClient` replays a pre-baked list of :class:`LLMResponse` /
``chat()`` strings in FIFO order, recording every call so the test can
inspect the messages list, schema and tool-call arguments after the fact.

This mirrors the inline ``FakeLLMClient`` already in
:mod:`tests.test_orchestrator`; we extract it here so every later slice's
contract tests can reuse the exact same scriptable client without the
copy/paste-and-evolve drift the existing tree already shows.

Issue 0049 extends this harness (NOT a parallel class) with two
companions:

- A ``stream_complete()`` override that consumes scripted
  :class:`StreamChunk` sequences from :attr:`_stream_responses` so
  integration tests can drive the streaming orchestrator path
  end-to-end. The default fallback (replay of ``complete()`` queue as
  a synthetic single-shot chunk trio) still works for tests that don't
  care about the per-tick shape.
- A ``queue_stream`` helper that scripts a sequence of chunks for one
  stream call.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

from bob.llm.types import LLMResponse, StreamChunk, ToolDefinition
from bob.llm_client import LLMClient


class FakeLLMClient(LLMClient):
    """Replay scripted ``complete()``, ``chat()``, and ``stream_complete()``.

    Construction:

    - ``complete_responses`` — list of :class:`LLMResponse` returned in FIFO
      order by :meth:`complete`. Each call pops one. An empty list when
      :meth:`complete` is invoked raises ``AssertionError`` so tests fail
      loudly on under-scripted scenarios.
    - ``chat_responses`` — same idea for :meth:`chat` but for raw strings.
    - ``stream_responses`` — list of pre-baked :class:`StreamChunk`
      sequences (a ``list[list[StreamChunk]]``). Each :meth:`stream_complete`
      call pops one sub-list and replays it; tests that don't script
      streams explicitly fall back to the synthetic single-shot path
      from the parent :class:`LLMClient`.

    Recorded state:

    - ``complete_calls`` / ``chat_calls`` / ``stream_calls`` — every
      call's args (messages, tools/schema, session_id). Inspect these
      from tests to assert the orchestrator built the expected prompt.
    """

    def __init__(
        self,
        *,
        complete_responses: list[LLMResponse] | None = None,
        chat_responses: list[str] | None = None,
        stream_responses: list[list[StreamChunk]] | None = None,
    ) -> None:
        self._complete_responses = list(complete_responses or [])
        self._chat_responses = list(chat_responses or [])
        self._stream_responses: list[list[StreamChunk]] = list(stream_responses or [])
        self.chat_calls: list[dict[str, Any]] = []
        self.complete_calls: list[dict[str, Any]] = []
        self.stream_calls: list[dict[str, Any]] = []

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

    async def stream_complete(
        self,
        messages: list[dict[str, Any]],
        tools: list[ToolDefinition] | None = None,
        session_id: str | None = None,
    ) -> AsyncIterator[StreamChunk]:
        """Replay the next scripted stream, falling back to ``complete()``.

        When the test pre-loaded a stream via :meth:`queue_stream`, that
        sequence is returned exactly as scripted. Otherwise we delegate
        to the base-class fallback which calls :meth:`complete` and
        synthesises a (``tool_call_start`` / ``args_delta`` /
        ``tool_call_end``) trio. This lets existing tests that only
        scripted ``complete_responses`` still work when the orchestrator
        switches to the streaming path.
        """

        self.stream_calls.append({"messages": messages, "tools": tools, "session_id": session_id})
        if self._stream_responses:
            chunks = self._stream_responses.pop(0)
            return _replay_chunks(chunks)
        # Fallback path mirrors :meth:`LLMClient.stream_complete` from
        # the base class, but we re-implement it here so we can record
        # the call site separately from ``complete_calls``.
        if not self._complete_responses:
            raise AssertionError("FakeLLMClient ran out of canned responses for stream_complete()")
        response = self._complete_responses.pop(0)
        return LLMClient._fallback_stream(response)

    def queue_complete(self, response: LLMResponse) -> None:
        """Append a scripted response to the back of the ``complete()`` queue."""

        self._complete_responses.append(response)

    def queue_chat(self, response: str) -> None:
        """Append a scripted response to the back of the ``chat()`` queue."""

        self._chat_responses.append(response)

    def queue_stream(self, chunks: list[StreamChunk]) -> None:
        """Append a scripted chunk sequence for the next ``stream_complete()``."""

        self._stream_responses.append(list(chunks))


async def _replay_chunks(chunks: list[StreamChunk]) -> AsyncIterator[StreamChunk]:
    """Yield ``chunks`` in order. Module-level so the async-gen identity is stable."""

    for chunk in chunks:
        yield chunk
