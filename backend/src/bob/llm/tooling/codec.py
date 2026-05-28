"""The :class:`ToolCodec` seam: the only layer that knows the wire format.

PRD 0008 / issue 0058. A codec answers two questions for the orchestrator and
:class:`bob.llm_client.LLMClient`:

- *inject* — given the outgoing chat messages and the tool specs, what request
  payload tells this backend the tools are available? (For native function
  calling that is the OpenAI ``tools=[…]`` + ``tool_choice`` kwargs.)
- *parse* — given the backend's raw reply (a whole message, or a stream of
  deltas), what :class:`bob.llm.types.ToolCall` list did the model emit?

Call sites (orchestrator, ``LLMClient``) hold a codec and never branch on the
wire format themselves. Issue 0058 ships exactly one codec,
:class:`NativeToolCodec`, by *moving* the existing LM Studio native
inject/parse/stream logic behind this interface — behaviour-identical, the
0057 golden fixtures stay green byte-for-byte. Guided-JSON (0060) and Hermes
(0061) codecs implement the same :class:`ToolCodec` protocol later.

The codec deliberately does NOT own observability (debug events, call logs) or
the retry/validate loop — those stay in the core (``LLMClient`` /
orchestrator). The codec is pure format. When native argument JSON is
malformed it raises :class:`NativeToolCallParseError` carrying the offending
raw string; the core translates that into its existing
:class:`bob.llm_client.LLMClientError` (and emits the same debug event) so the
error surface is unchanged.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any, Protocol, cast, runtime_checkable
from uuid import uuid4

from bob.llm.types import StreamChunk, ToolCall

if TYPE_CHECKING:  # pragma: no cover — typing-only import.
    from collections.abc import Iterator

    from bob.llm.tooling.spec import ToolSpec


class NativeToolCallParseError(ValueError):
    """Raised by :class:`NativeToolCodec` when tool-call arguments are not valid JSON.

    Carries the human-readable ``message`` (pre-formatted to match the legacy
    :class:`bob.llm_client.LLMClientError` text byte-for-byte) and the raw
    ``arguments`` string that failed to decode, so the core can emit its
    existing ``malformed tool args`` debug event before re-raising as
    ``LLMClientError``. Keeping this a codec-local exception avoids an import
    cycle with :mod:`bob.llm_client` (which imports this module).
    """

    def __init__(self, message: str, *, arguments_raw: str, is_decode_error: bool) -> None:
        super().__init__(message)
        self.message = message
        self.arguments_raw = arguments_raw
        #: True when JSON decoding itself failed (the legacy path emitted a
        #: ``malformed tool args`` debug event only in this case). False when
        #: the arguments decoded but were not an object — the legacy path
        #: raised ``LLMClientError`` with no debug event.
        self.is_decode_error = is_decode_error


@runtime_checkable
class ToolCodec(Protocol):
    """Wire-format strategy for advertising tools and parsing tool calls.

    Implementations are stateless and cheap to construct;
    :func:`bob.llm.tooling.capability.select_codec` instantiates one per call
    site. The protocol is intentionally minimal — ``inject`` shapes the
    request, ``parse`` reads a whole non-streaming reply, and
    ``stream_parser`` hands back a stateful accumulator for the streaming
    path. A codec that cannot stream natively can still satisfy the protocol;
    the core falls back to replaying ``parse`` output as a synthetic stream
    (see :meth:`bob.llm_client.LLMClient._fallback_stream`).
    """

    def inject(
        self,
        messages: list[dict[str, Any]],
        specs: list[ToolSpec],
    ) -> dict[str, Any]:
        """Return the request-payload fragment that advertises ``specs``.

        The returned dict is merged into the provider request kwargs by the
        caller. ``messages`` is passed for codecs that inject into the prompt
        (guided/Hermes) — the native codec advertises tools out-of-band via
        the ``tools`` kwarg and leaves ``messages`` untouched.
        """
        ...

    def parse(self, message: Any) -> list[ToolCall]:
        """Parse a complete (non-streaming) provider message into tool calls."""
        ...

    def stream_parser(self) -> ToolCallStreamParser:
        """Return a fresh stateful parser for one streaming completion."""
        ...


class ToolCallStreamParser(Protocol):
    """Stateful per-completion accumulator for the streaming parse path.

    The core feeds each provider delta into :meth:`feed` and emits whatever
    :class:`bob.llm.types.StreamChunk` objects come back, then calls
    :meth:`finish` once the stream closes to flush the terminal
    ``tool_call_end`` chunks.
    """

    def feed(self, delta: Any) -> Iterator[StreamChunk]:
        """Consume one provider ``choices[0].delta`` and yield ready chunks."""
        ...

    def finish(self) -> Iterator[StreamChunk]:
        """Flush terminal ``tool_call_end`` chunks after the stream closes."""
        ...

    @property
    def log_calls(self) -> list[dict[str, Any]]:
        """Tool calls accumulated so far, in the shape the core logs."""
        ...


class NativeToolCodec:
    """OpenAI-compatible native function-calling codec (the LM Studio path).

    Moves three pieces of logic out of :class:`bob.llm_client.LMStudioClient`
    verbatim:

    1. :meth:`inject` — the ``tools=[{"type": "function", "function": …}]`` +
       ``tool_choice="auto"`` kwargs block (previously duplicated in
       ``complete`` and ``stream_complete``).
    2. :meth:`parse` — the ``message.tool_calls`` walk that ``json.loads`` each
       ``function.arguments`` string and raises on malformed JSON / non-object
       arguments (previously the inline loop in ``complete``).
    3. :meth:`stream_parser` — the per-``index`` delta accumulator that emits
       ``tool_call_start`` / ``tool_call_args_delta`` / ``tool_call_end``
       chunks (previously the body of ``_consume_stream``).

    The ``say`` tool's progressive TTS relies on the exact
    ``delta.tool_calls[].function.arguments`` suffixes flowing through
    unchanged into :class:`bob.streaming.PartialJsonParser`; this codec
    re-emits those suffixes byte-for-byte.
    """

    def inject(
        self,
        messages: list[dict[str, Any]],
        specs: list[ToolSpec],
    ) -> dict[str, Any]:
        """Build the native ``tools`` + ``tool_choice`` request kwargs.

        Returns an empty dict when ``specs`` is empty so the caller adds
        nothing (matching the old ``if tools:`` guard). Tool order is
        preserved (registration order in, same order out) — deterministic
        ordering is locked by issue 0063 but injection order stays stable
        here.
        """

        if not specs:
            return {}
        return {
            "tools": [
                {
                    "type": "function",
                    "function": {
                        "name": spec.name,
                        "description": spec.description,
                        "parameters": spec.parameters,
                    },
                }
                for spec in specs
            ],
            "tool_choice": "auto",
        }

    def parse(self, message: Any) -> list[ToolCall]:
        """Parse ``message.tool_calls`` into :class:`ToolCall` objects.

        Mirrors the legacy ``LMStudioClient.complete`` loop exactly:

        - each ``function.arguments`` string is ``json.loads``-ed (empty
          string → ``{}``);
        - a non-JSON string raises :class:`NativeToolCallParseError` carrying
          the raw string (the core re-raises ``LLMClientError`` + emits the
          ``malformed tool args`` debug event);
        - arguments that decode to a non-object raise
          :class:`NativeToolCallParseError` with no raw payload;
        - a missing provider id is replaced with a ``call_<8-hex>`` placeholder.
        """

        raw_tool_calls = getattr(message, "tool_calls", None) or []
        tool_calls: list[ToolCall] = []
        for raw_call in raw_tool_calls:
            function = getattr(raw_call, "function", None)
            if function is None:
                continue
            name = getattr(function, "name", None) or ""
            arguments_raw = getattr(function, "arguments", "") or ""
            try:
                arguments = json.loads(arguments_raw) if arguments_raw else {}
            except json.JSONDecodeError as exc:
                raise NativeToolCallParseError(
                    f"LM Studio tool call arguments are not valid JSON: {arguments_raw[:200]!r}",
                    arguments_raw=arguments_raw,
                    is_decode_error=True,
                ) from exc
            if not isinstance(arguments, dict):
                raise NativeToolCallParseError(
                    "LM Studio tool call arguments must decode to an object, "
                    f"got {type(arguments).__name__}",
                    arguments_raw=arguments_raw,
                    is_decode_error=False,
                )
            call_id = getattr(raw_call, "id", None) or f"call_{uuid4().hex[:8]}"
            tool_calls.append(
                ToolCall(id=call_id, name=name, arguments=cast(dict[str, Any], arguments))
            )
        return tool_calls

    def stream_parser(self) -> _NativeStreamParser:
        """Return a fresh :class:`_NativeStreamParser` for one stream."""

        return _NativeStreamParser()


class _NativeStreamParser:
    """Per-completion streaming accumulator (moved from ``_consume_stream``).

    Tracks per-``index`` tool-call state across provider ticks and emits the
    ``tool_call_*`` chunk lifecycle. The logic — id/name resolution timing,
    the ``started_yielded`` gate before any args delta, the placeholder id,
    the final-args ``json.loads`` and its malformed raise — is copied verbatim
    from the original inline loop so the streaming surface
    (:class:`bob.streaming.StreamEmitter` → ``speech_delta``) is byte-identical.
    """

    def __init__(self) -> None:
        # Per-index tool-call state: ``{index: {"id", "name", "arguments",
        # "started_yielded"}}``. ``index`` is stable across ticks per the
        # OpenAI streaming protocol.
        self._tool_call_states: dict[int, dict[str, Any]] = {}

    def feed(self, delta: Any) -> Iterator[StreamChunk]:
        """Consume one ``choices[0].delta`` and yield any ready chunks.

        Text deltas are NOT handled here — they are not a tool-call concern and
        the core emits them directly (matching the original split where
        ``delta.content`` was read inline before the tool-call branch).
        """

        raw_tool_calls = getattr(delta, "tool_calls", None) or []
        for raw_tc in raw_tool_calls:
            index = getattr(raw_tc, "index", 0)
            function = getattr(raw_tc, "function", None)
            state = self._tool_call_states.setdefault(
                index,
                {
                    "id": getattr(raw_tc, "id", None),
                    "name": None,
                    "arguments": "",
                    "started_yielded": False,
                },
            )
            # Provider-assigned id may arrive on the first or second tick.
            incoming_id = getattr(raw_tc, "id", None)
            if incoming_id and not state["id"]:
                state["id"] = incoming_id

            if function is not None:
                incoming_name = getattr(function, "name", None)
                if incoming_name and not state["name"]:
                    state["name"] = incoming_name

            # Emit ``tool_call_start`` the first time we have a name (the
            # orchestrator needs it to dispatch). A missing id gets a
            # deterministic placeholder.
            if not state["started_yielded"] and state["name"]:
                if not state["id"]:
                    state["id"] = f"call_{uuid4().hex[:8]}"
                state["started_yielded"] = True
                yield StreamChunk(
                    kind="tool_call_start",
                    tool_call_id=cast(str, state["id"]),
                    name=cast(str, state["name"]),
                )

            if function is not None:
                args_delta = getattr(function, "arguments", None) or ""
                if isinstance(args_delta, str) and args_delta:
                    state["arguments"] = cast(str, state["arguments"]) + args_delta
                    # Only emit args deltas once the start chunk has gone out
                    # — the StreamEmitter binds ``msg_id`` on the first frame.
                    if state["started_yielded"]:
                        yield StreamChunk(
                            kind="tool_call_args_delta",
                            tool_call_id=cast(str, state["id"]),
                            args_delta=args_delta,
                        )

    def finish(self) -> Iterator[StreamChunk]:
        """Emit one ``tool_call_end`` per resolved call, parsing final args.

        A tool call whose name never resolved is skipped (no dispatchable
        tool). Malformed final-args JSON raises
        :class:`NativeToolCallParseError` carrying the accumulated raw string,
        which the core translates into ``LLMClientError`` (matching the old
        post-stream behaviour) after emitting its debug event.
        """

        for state in self._tool_call_states.values():
            if not state["started_yielded"]:
                continue
            arguments_raw = cast(str, state["arguments"])
            try:
                final_arguments = json.loads(arguments_raw) if arguments_raw else {}
            except json.JSONDecodeError as exc:
                raise NativeToolCallParseError(
                    "LM Studio tool call arguments are not valid JSON after "
                    f"stream close: {arguments_raw[:200]!r}",
                    arguments_raw=arguments_raw,
                    is_decode_error=True,
                ) from exc
            if not isinstance(final_arguments, dict):
                raise NativeToolCallParseError(
                    "LM Studio tool call arguments must decode to an object, "
                    f"got {type(final_arguments).__name__}",
                    arguments_raw=arguments_raw,
                    is_decode_error=False,
                )
            yield StreamChunk(
                kind="tool_call_end",
                tool_call_id=cast(str, state["id"]),
                final_arguments=cast(dict[str, Any], final_arguments),
            )

    @property
    def log_calls(self) -> list[dict[str, Any]]:
        """Resolved calls in the ``{"id","name","arguments"}`` log shape.

        Used by the core's ``finally`` block to reconstruct the
        ``raw_for_log`` / debug ``tool_calls`` payload exactly as the old
        ``_consume_stream`` did (arguments kept as the accumulated *string*).
        """

        return [
            {
                "id": state["id"],
                "name": state["name"],
                "arguments": state["arguments"],
            }
            for state in self._tool_call_states.values()
            if state["started_yielded"]
        ]


__all__ = [
    "NativeToolCallParseError",
    "NativeToolCodec",
    "ToolCallStreamParser",
    "ToolCodec",
]
