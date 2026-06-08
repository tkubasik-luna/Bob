"""M6 — private-SDK override that resurfaces tool-call argument fragments.

This is the SINGLE module that touches the SDK's private *streaming* tool-call
surface, isolated so the fragile API lives in exactly one place (PRD 0017:
"override isolé dans UN module + test de garde contractuel + pin de version").

WHY this override exists
------------------------
The LM Studio websocket wire emits the model's tool-call arguments
INCREMENTALLY, as a sequence of ``toolCallGenerationArgumentFragmentGenerated``
messages (one per generated arg-string fragment), and only at the end a single
``toolCallGenerationEnd`` carrying the whole, already-decoded call. But the
installed ``lmstudio`` SDK (1.5.0) THROWS the incremental fragments away — its
:meth:`lmstudio.json_api.PredictionEndpoint.iter_message_events` matches that
message type with a bare ``pass  # UI event, currently ignored by Python SDK``.
Only the terminal ``toolCallGenerationEnd`` survives, as a
:class:`lmstudio.json_api.PredictionToolCallEvent`.

For Bob's voice path that is a LATENCY REGRESSION we cannot accept: the ``say``
tool starts TTS from the *partial* ``speech`` field as the arguments stream in
(``PartialJsonParser`` → ``speech_delta``). If we only ever saw the whole call,
TTS could not start until the model finished generating the entire tool call —
the exact early-start the OpenAI transport gives us (PRD 0016) would be lost.

What the override does
----------------------
:class:`BobChatResponseEndpoint` subclasses the SDK's chat response endpoint and
overrides :meth:`iter_message_events` to RESURFACE the dropped fragment messages
as a custom internal event, :class:`PredictionToolCallArgFragmentEvent`, carrying
the new argument-string FRAGMENT plus the best-effort call id / name / index it
has seen so far on this turn (from the ``toolCallGenerationStart`` /
``toolCallGenerationNameReceived`` messages, which the base SDK also ignores).
EVERY other message type is delegated UNCHANGED to ``super().iter_message_events``
so the text/reasoning fragments, the success/stats result and the terminal
``toolCallGenerationEnd`` keep their exact base-SDK semantics — we only ADD the
fragment arm, we never reinterpret the rest.

:meth:`handle_rx_event` is also overridden, but ONLY to no-op our custom event
(the base method ends in ``assert_never`` over its closed event union, so an
un-handled custom event would crash). Every other event is delegated to
``super().handle_rx_event`` unchanged.

The EXACT private surface this builds on (lmstudio 1.5.0 — quote of the real
installed package, ``lmstudio/json_api.py`` + ``lmstudio/_sdk_models``):

- ``PredictionEndpoint.iter_message_events(self, contents: DictObject | None)
  -> Iterable[PredictionRxEvent]`` — a structural ``match contents`` over the
  raw server message dicts. The dropped fragment arm is::

      case {"type": "toolCallGenerationArgumentFragmentGenerated"}:
          pass  # UI event, currently ignored by Python SDK

- The real wire dict shapes (``_sdk_models``,
  ``LlmChannelPredictToClientPacketToolCallGeneration*``):
    - ``{"type": "toolCallGenerationStart", "toolCallId": str | None}``
      (``toolCallId`` optional) — the only place a call id appears for the
      streaming phase.
    - ``{"type": "toolCallGenerationNameReceived", "name": str}`` — the tool
      name, also dropped by the base SDK.
    - ``{"type": "toolCallGenerationArgumentFragmentGenerated", "content": str}``
      — the argument-string FRAGMENT lives in ``content``. There is NO id/index
      on the fragment message itself, hence we track them from the start/name
      messages on this endpoint instance.
    - ``{"type": "toolCallGenerationEnd", "toolCallRequest": {...}}`` — the
      whole, decoded call (handled by the base SDK → ``PredictionToolCallEvent``).

The contract guard test (``tests/test_lmstudio_sdk_stream_complete.py``) feeds
this subclass faked channel-message dicts INCLUDING a fragment and asserts the
override emits :class:`PredictionToolCallArgFragmentEvent` — it fails loudly if a
future SDK upgrade renames the message type, changes the ``content`` key, or
reintegrates the fragment into the base parser (in which case the override would
need re-evaluating). That, plus the ``lmstudio`` version pin in
``pyproject.toml``, is the anti-regression sentinel the PRD demands.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, cast

from lmstudio.json_api import ChatResponseEndpoint, PredictionRxEvent

if TYPE_CHECKING:
    from collections.abc import Iterable


@dataclass
class PredictionToolCallArgFragmentEvent:
    """Internal event resurfacing one dropped tool-call argument fragment.

    Carries the NEW argument-string fragment (:attr:`fragment`, the wire
    ``content`` field) plus the best-effort call ``id`` / ``name`` / ``index``
    the endpoint has seen so far this turn (the wire fragment message itself
    carries none of these). ``index`` is a 0-based counter incremented on each
    ``toolCallGenerationStart`` so concurrent / sequential calls stay
    distinguishable even when the server omits ``toolCallId``.

    Subclasses :class:`ChannelRxEvent` indirectly only in spirit — it is a plain
    dataclass, NOT a ``ChannelRxEvent``, because the base ``handle_rx_event``
    ``match`` would ``assert_never`` on an unknown ``ChannelRxEvent`` subtype.
    Keeping it a distinct type lets :meth:`BobChatResponseEndpoint.handle_rx_event`
    short-circuit it cleanly before delegating the rest to ``super()``.
    """

    fragment: str
    id: str | None = None
    name: str | None = None
    index: int = 0


class BobChatResponseEndpoint(ChatResponseEndpoint):
    """Chat response endpoint that resurfaces incremental tool-call arg fragments.

    See the module docstring for the full rationale + the exact private surface
    this overrides. The override is deliberately MINIMAL: it adds an arm for the
    dropped ``toolCallGenerationArgumentFragmentGenerated`` message (and tracks
    id/name/index from the start/name messages the base SDK also ignores) and
    delegates EVERY other message type to ``super().iter_message_events`` so the
    base-SDK semantics for text/reasoning/result/terminal-tool-call are
    byte-unchanged.
    """

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        # Per-turn tracking of the streaming tool-call identity. The fragment
        # wire message carries no id/name/index, so we remember the last
        # ``toolCallGenerationStart`` / ``toolCallGenerationNameReceived`` we
        # saw and stamp the fragment with them.
        self._stream_tool_call_id: str | None = None
        self._stream_tool_call_name: str | None = None
        self._stream_tool_call_index: int = -1

    def iter_message_events(self, contents: Any) -> Iterable[PredictionRxEvent]:
        """Resurface dropped arg-fragment messages; delegate everything else.

        Matches ONLY the three message types the base SDK drops for the
        streaming tool-call phase:

        - ``toolCallGenerationStart`` → bump the index + remember ``toolCallId``;
          also delegate to ``super()`` so the base SDK's debug logging still runs
          and the base parser stays the source of truth for that message.
        - ``toolCallGenerationNameReceived`` → remember ``name``.
        - ``toolCallGenerationArgumentFragmentGenerated`` → emit a
          :class:`PredictionToolCallArgFragmentEvent` carrying the ``content``
          fragment + the tracked id/name/index.

        Any other ``contents`` (fragment / success / toolCallGenerationEnd /
        error / …) falls straight through to ``super().iter_message_events`` with
        no reinterpretation.
        """

        if isinstance(contents, dict):
            msg_type = contents.get("type")
            if msg_type == "toolCallGenerationStart":
                # New streaming tool call: advance the index + remember any id.
                self._stream_tool_call_index += 1
                self._stream_tool_call_id = contents.get("toolCallId")
                self._stream_tool_call_name = None
                # Delegate too so the base SDK's debug log + state are unchanged.
                yield from super().iter_message_events(contents)
                return
            if msg_type == "toolCallGenerationNameReceived":
                name = contents.get("name")
                if isinstance(name, str):
                    self._stream_tool_call_name = name
                # The base SDK ignores this message (no event); nothing to
                # delegate, but call super() to preserve any future handling.
                yield from super().iter_message_events(contents)
                return
            if msg_type == "toolCallGenerationArgumentFragmentGenerated":
                fragment = contents.get("content")
                if isinstance(fragment, str) and fragment:
                    # The custom event is not in the SDK's closed ``PredictionRxEvent``
                    # union (deliberately — see the class docstring); the driver
                    # routes it via ``isinstance``, so cast to satisfy the
                    # supertype-matching return annotation.
                    yield cast(
                        "PredictionRxEvent",
                        PredictionToolCallArgFragmentEvent(
                            fragment=fragment,
                            id=self._stream_tool_call_id,
                            name=self._stream_tool_call_name,
                            index=max(self._stream_tool_call_index, 0),
                        ),
                    )
                # The base SDK drops this message entirely (no event), so there
                # is nothing to delegate — return without calling super().
                return

        # Every other message type keeps its exact base-SDK semantics.
        yield from super().iter_message_events(contents)

    def handle_rx_event(self, event: Any) -> None:
        """No-op our custom event; delegate every real SDK event to ``super()``.

        The base ``handle_rx_event`` is a closed ``match`` ending in
        ``assert_never`` over the SDK's event union, so handing it our custom
        :class:`PredictionToolCallArgFragmentEvent` would crash. We short-circuit
        it here (it needs no endpoint-side handling — it is consumed purely in
        the ``stream_complete`` driver) and pass everything else through
        unchanged.
        """

        if isinstance(event, PredictionToolCallArgFragmentEvent):
            return
        super().handle_rx_event(event)


__all__ = ["BobChatResponseEndpoint", "PredictionToolCallArgFragmentEvent"]
