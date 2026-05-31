"""Reasoning-stream reader — separated cosmetic + action channels (PRD 0011 / issue 0069).

The foundational tracer-bullet of the agent-activity feed. A sub-agent LLM call
is STREAMED (via :meth:`bob.llm_client.LLMClient.stream_chat`) so the model's
chain-of-thought can surface live in the HUD while the call is still running.
But the sub-agent's control envelope (the :class:`bob.sub_agent.actions.SubAgentAction`)
must STILL be parsed from the final aggregated content exactly as the
non-streaming ``chat`` path did — reasoning is purely cosmetic and has ZERO
correctness impact.

:class:`ReasoningStreamReader` owns that split. It consumes a
``StreamChunk`` async-iterator and exposes two channels:

- a live iterator of reasoning deltas (``reasoning`` chunks' ``reasoning_delta``),
  in order, for the caller to forward to the feed as they arrive;
- the aggregated ``content`` (``text`` chunks concatenated), collected to the end
  and read once the stream is exhausted — this is what the action is parsed from.

It also detects the ABSENCE of a reasoning channel: a stream that never carries a
``reasoning`` chunk (a model / endpoint without ``reasoning_content``) leaves
:attr:`degraded` ``True`` — the hook issue 0070 builds its narrated-steps
fallback on. Here we only expose the fact.

Usage::

    reader = ReasoningStreamReader(client.stream_chat(messages, schema=schema))
    async for delta in reader.reasoning_deltas():
        await emit_reasoning(delta)   # cosmetic, live
    content = reader.content          # aggregated, parse the action from THIS
    if reader.degraded:
        ...                           # no reasoning channel (0070 hook)

The reasoning iterator drives the consumption of the underlying stream; reading
:attr:`content` / :attr:`degraded` before the iterator is exhausted raises, so a
caller can never accidentally parse a half-collected envelope.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

from bob.llm.types import StreamChunk


class ReasoningStreamReaderError(RuntimeError):
    """Raised when the aggregated channels are read before the stream is drained."""


class ReasoningStreamReader:
    """Split a sub-agent ``StreamChunk`` stream into reasoning + content channels."""

    def __init__(self, stream: AsyncIterator[StreamChunk]) -> None:
        self._stream = stream
        self._content_parts: list[str] = []
        self._saw_reasoning = False
        self._done = False

    async def reasoning_deltas(self) -> AsyncIterator[str]:
        """Yield reasoning deltas in order while draining the underlying stream.

        Driving this iterator to exhaustion is what walks the whole stream:
        ``text`` chunks are aggregated into :attr:`content` as a side effect and
        any other chunk kind (``tool_call_*``) is ignored (the sub-agent control
        envelope travels as guided-JSON ``text`` content, never native tool
        calls). On completion :attr:`content` and :attr:`degraded` are readable.
        """

        async for chunk in self._stream:
            if chunk.kind == "reasoning":
                self._saw_reasoning = True
                if chunk.reasoning_delta:
                    yield chunk.reasoning_delta
            elif chunk.kind == "text":
                if chunk.text_delta:
                    self._content_parts.append(chunk.text_delta)
            # tool_call_* chunks are not part of the sub-agent envelope path —
            # ignored deliberately (the action is guided-JSON text content).
        self._done = True

    @property
    def content(self) -> str:
        """The aggregated text content — the ONLY thing the action is parsed from.

        Raises :class:`ReasoningStreamReaderError` if read before the reasoning
        iterator has been exhausted, so a caller can never parse a partial
        envelope.
        """

        if not self._done:
            raise ReasoningStreamReaderError(
                "content read before the reasoning stream was drained — exhaust "
                "reasoning_deltas() first"
            )
        return "".join(self._content_parts)

    @property
    def degraded(self) -> bool:
        """True when the stream carried NO reasoning channel (issue 0070 hook).

        A reasoning-capable model leaves this ``False``; a model / endpoint that
        never surfaces ``reasoning_content`` leaves it ``True``. Issue 0070 will
        switch to a narrated-steps fallback on this signal; here it is only
        exposed. Raises before the stream is drained (same guard as
        :attr:`content`).
        """

        if not self._done:
            raise ReasoningStreamReaderError(
                "degraded read before the reasoning stream was drained — exhaust "
                "reasoning_deltas() first"
            )
        return not self._saw_reasoning


__all__ = ["ReasoningStreamReader", "ReasoningStreamReaderError"]
