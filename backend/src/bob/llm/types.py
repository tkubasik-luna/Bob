"""Shared data types for the unified tool-calling API.

Used by :meth:`bob.llm_client.LLMClient.complete` so callers can pass tool
definitions and consume tool calls without caring whether the backend is
LM Studio (OpenAI-compatible function calling) or the Claude CLI
(JSON-in-system-prompt protocol).

PRD 0006 / issue 0049 adds :class:`StreamChunk` for the streaming
tool-call surface :meth:`bob.llm_client.LLMClient.stream_complete`.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal


@dataclass(frozen=True)
class ToolDefinition:
    """Description of a tool the model is allowed to call.

    ``parameters`` is a JSON Schema object (the same shape OpenAI uses for the
    ``function.parameters`` field).
    """

    name: str
    description: str
    parameters: dict[str, Any]


@dataclass(frozen=True)
class ToolCall:
    """A single tool invocation emitted by the model.

    ``id`` is the provider-assigned call id. Bob uses it to route tool results
    back into the next turn. When the underlying provider does not supply an
    id (Claude CLI is a JSON-only protocol), the client generates one.
    """

    id: str
    name: str
    arguments: dict[str, Any]


@dataclass(frozen=True)
class LLMResponse:
    """Outcome of an :meth:`LLMClient.complete` call.

    Exactly one of ``text`` and ``tool_calls`` is meaningful per call:

    - ``text != None`` and ``tool_calls == []`` → the model answered with plain
      text.
    - ``text is None`` and ``tool_calls`` non-empty → the model wants to invoke
      one or more tools.
    """

    text: str | None
    tool_calls: list[ToolCall]

    @property
    def is_tool_call(self) -> bool:
        return bool(self.tool_calls)


#: Kind discriminator for :class:`StreamChunk`. See the class docstring
#: for the supported phases.
StreamChunkKind = Literal[
    "tool_call_start", "tool_call_args_delta", "tool_call_end", "text", "reasoning", "perf"
]


@dataclass(frozen=True)
class StreamChunk:
    """A single chunk emitted by :meth:`bob.llm_client.LLMClient.stream_complete`.

    The four kinds together describe the lifecycle of one streamed
    tool call:

    - ``tool_call_start`` — first chunk for a new tool call. Carries
      ``tool_call_id`` + ``name`` (resolved as early as the provider
      surfaces it). ``args_delta`` is empty here.
    - ``tool_call_args_delta`` — every subsequent chunk for that call
      whose ``delta.tool_calls[0].function.arguments`` payload is
      non-empty. ``args_delta`` is the new suffix; the caller is
      responsible for accumulating it (a :class:`bob.streaming.StreamEmitter`
      does this on the Bob side).
    - ``tool_call_end`` — emitted exactly once when the provider closes
      the stream for that call. ``final_arguments`` carries the parsed
      JSON object (or ``None`` if the arguments were never closeable —
      the caller can decide how to handle that).
    - ``text`` — emitted when the model chose plain text over a tool
      call. ``text_delta`` carries the latest chunk. Streamed text mode
      is rare under the unified ``say`` tool but supported for
      robustness (the orchestrator's retry path treats it as a
      contract violation and re-prompts).
    - ``reasoning`` — emitted when the provider surfaces the model's
      chain-of-thought as ``delta.reasoning_content`` (reasoning-capable
      models on OpenAI-compatible endpoints, e.g. LM Studio).
      ``reasoning_delta`` carries the latest reasoning suffix. This is a
      purely COSMETIC channel (PRD 0011 / issue 0069): it feeds the live
      agent-activity feed and never participates in tool-call / text
      action parsing — the action is always parsed from the aggregated
      ``content`` (the ``text`` / ``tool_call_*`` chunks), never from the
      reasoning stream.

    The discriminator design avoids a seven-field union with mostly-None
    members. Call sites pattern-match on ``kind`` and read only the
    fields meaningful for that kind.
    """

    kind: StreamChunkKind
    #: Set on ``tool_call_*`` chunks. Stable across the whole call.
    tool_call_id: str | None = None
    #: Set on ``tool_call_start``. Empty on subsequent chunks.
    name: str | None = None
    #: Set on ``tool_call_args_delta``. The NEW suffix of the
    #: streaming argument string for this tick, NOT the accumulated
    #: buffer.
    args_delta: str = ""
    #: Set on ``tool_call_end``. The fully-parsed arguments dict, or
    #: ``None`` when the stream closed before valid JSON was visible.
    final_arguments: dict[str, Any] | None = None
    #: Set on ``text``. The latest text suffix, not the accumulated
    #: buffer.
    text_delta: str = ""
    #: Set on ``reasoning``. The latest reasoning-content suffix
    #: (``delta.reasoning_content``), not the accumulated buffer. Cosmetic
    #: — never feeds action parsing (PRD 0011 / issue 0069).
    reasoning_delta: str = ""
    #: Set on ``perf`` (emitted once at stream end). Token usage + timing for
    #: the activity-feed perf footer. All optional — a provider that omits
    #: ``usage`` (or a stream that closed early) leaves them ``None``. Purely
    #: COSMETIC, like ``reasoning``: never feeds action parsing.
    tokens_in: int | None = None
    tokens_out: int | None = None
    reasoning_tokens: int | None = None
    #: Time to first token (seconds) and generation throughput (tokens/sec).
    ttft_s: float | None = None
    tok_s: float | None = None
