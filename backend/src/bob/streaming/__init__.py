"""Streaming tool-call argument plumbing (PRD 0006 / issue 0049).

This package is the live path between the LLM's streamed
``delta.tool_calls[0].function.arguments`` byte sequence and the WS frames
the frontend consumes for progressive TTS + sphere text.

Two narrow surfaces:

- :mod:`.partial_json_parser` — thin wrapper over the
  ``partial-json-parser`` PyPI package. NEVER hand-roll a tolerant
  scanner: bad JSON is a security boundary (prompt-injection adjacent —
  see :mod:`bob.validation`) and re-implementing partial parsing per
  feature would let bugs slip in unnoticed.
- :mod:`.stream_emitter` — given a stream of argument-delta bytes,
  re-emits the parsed ``say.speech`` slice as a series of
  ``speech_delta`` :class:`bob.event_bus_v2.WsTaskEvent` frames and the
  final ``ui`` object (when non-null) as a single ``ui_payload`` frame.

Both modules are pure with respect to side effects except for the
:func:`bob.event_bus_v2.emit_event` calls performed by the emitter.
Tests can substitute the emitter call directly via the
``emit`` constructor argument.
"""

from __future__ import annotations

from bob.streaming.partial_json_parser import PartialJsonParser
from bob.streaming.stream_emitter import StreamEmitter

__all__ = [
    "PartialJsonParser",
    "StreamEmitter",
]
