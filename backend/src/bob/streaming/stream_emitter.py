"""Stream emitter — argument-delta bytes → WS frames (PRD 0006 / issue 0049).

Sits between the LLM client's streaming surface
(``delta.tool_calls[0].function.arguments`` chunks) and the WS bus
(:func:`bob.event_bus_v2.emit_event`). The emitter owns one
:class:`bob.streaming.PartialJsonParser`, accumulates argument deltas,
and re-emits two distinct frame types:

- ``speech_delta`` — one frame per **growth** of the parsed ``say.speech``
  field. The frame's ``delta`` is the NEW characters since the previous
  emit. Re-emits are idempotent: a no-op tick (empty buffer growth, or
  parser stuck waiting for more bytes) emits nothing.
- ``ui_payload`` — exactly one frame on argument-object close, carrying
  the final ``ui`` value when non-null. Emitted ONLY for the ``say``
  tool: other tools don't carry a ``ui`` argument. An empty / null /
  missing ``ui`` does NOT emit anything (the frontend treats absence as
  "no overlay to open").

All emits route through :func:`bob.event_bus_v2.emit_event` so:

1. The chat WS forwarder picks them up and sends them as WS frames
   (single-user desktop app — there's at most one connected client per
   session).
2. The unified debug ring buffer captures the same frames for the
   debug view (PRD 0005) and the per-task overlay subscription (issue
   0052).

Lifecycle
---------

1. The orchestrator constructs one :class:`StreamEmitter` per Jarvis
   turn. The emitter is bound to a stable ``msg_id`` for the turn
   (matches the eventual ``assistant_msg`` frame so the frontend can
   correlate the streamed deltas with the bubble being filled in).
2. On every LLM-streamed argument delta the orchestrator calls
   :meth:`StreamEmitter.feed`. The emitter parses the accumulated
   buffer, computes the ``speech`` delta, and emits a
   ``speech_delta`` event if there is one.
3. On argument-object close (the orchestrator sees the LLM stop
   streaming for this call) it calls
   :meth:`StreamEmitter.finalize`. If the parsed final object carries a
   non-null ``ui``, one ``ui_payload`` event fires.

Validation interaction (0048 wiring)
------------------------------------

The emitter is **purely additive** to the validation retry path:

- ``speech_delta`` frames are emitted as the parser sees the ``speech``
  field grow. They are committed to the WS bus before the final
  Pydantic ``SayArgs`` validation runs. This is intentional — the
  user-facing UX win is "hear Jarvis start speaking immediately"; we
  cannot retro-cancel audio that has already been spoken.
- If the streamed ``say.speech`` validates but ``say.ui`` malforms,
  the validation policy's ``accept_partial=True`` setting on the
  ``say`` tool (see :data:`bob.validation.POLICY_TABLE`) rescues the
  call by stripping the bad key and accepting the rest. The
  ``ui_payload`` frame is NOT emitted in that case (the parser yields
  a dict with no ``ui`` key, or with a malformed nested value the
  emitter ignores) — the user hears the speech but no overlay opens.
- If ``say.speech`` itself never validates (empty after strip, missing,
  wrong type), the orchestrator retries via the normal validation
  path. Any ``speech_delta`` already emitted stays committed — the
  retry produces a fresh batch. We document the wart here rather than
  silently swallowing it: the alternative ("hold speech until full
  validation") would defeat the streaming UX, and the LLM emitting
  garbage half-way through a stream is rare enough in practice not to
  warrant a multi-stage commit / rollback machine on top of the WS.

The emitter never calls the validator itself — that lives upstream in
the orchestrator.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

import structlog

from bob.event_bus_v2 import emit_event as default_emit_event
from bob.streaming.partial_json_parser import PartialJsonParser
from bob.ui_registry import coerce_component_descriptor

_logger = structlog.get_logger(__name__)


#: Type of the emit callable the :class:`StreamEmitter` calls into.
#: Mirrors :func:`bob.event_bus_v2.emit_event` so tests can substitute a
#: simple ``async def`` recorder without monkey-patching the module.
EmitFn = Callable[[dict[str, Any]], Awaitable[None]]


class StreamEmitter:
    """Stateful emitter for one streamed Jarvis tool call.

    Construction:

    - ``msg_id`` — turn-stable id shared with the eventual
      ``assistant_msg`` frame. The frontend uses it to attach the
      streamed deltas to the right assistant bubble.
    - ``emit`` — async callable taking the WS payload dict. Defaults to
      :func:`bob.event_bus_v2.emit_event`. Tests inject a recorder.
    - ``parser`` — :class:`PartialJsonParser` instance. Defaults to a
      fresh one; tests can pin ``tolerate_trailing_comma=False``.

    State carried between :meth:`feed` calls:

    - ``_buffer`` — accumulated argument string.
    - ``_emitted_speech`` — total bytes of ``speech`` already emitted
      as ``speech_delta`` frames. Used to compute the incremental
      delta on the next tick.
    - ``_ui_emitted`` — guard against duplicate ``ui_payload`` emits
      (defensive; :meth:`finalize` is the only path that emits it but
      a buggy caller might call it twice).
    """

    def __init__(
        self,
        *,
        msg_id: str,
        emit: EmitFn | None = None,
        parser: PartialJsonParser | None = None,
    ) -> None:
        self._msg_id = msg_id
        self._emit: EmitFn = emit if emit is not None else default_emit_event
        self._parser = parser if parser is not None else PartialJsonParser()
        self._buffer: str = ""
        self._emitted_speech: str = ""
        self._ui_emitted: bool = False
        self._finalised: bool = False

    @property
    def msg_id(self) -> str:
        """Stable id shared with the ``assistant_msg`` frame for this turn."""

        return self._msg_id

    @property
    def buffer(self) -> str:
        """Accumulated argument string — exposed for tests + diagnostics."""

        return self._buffer

    @property
    def emitted_speech(self) -> str:
        """Total ``speech`` characters emitted so far (for tests)."""

        return self._emitted_speech

    async def feed(self, delta: str) -> None:
        """Accumulate ``delta`` and emit any newly-visible ``speech_delta``.

        Called once per LLM-streamed argument chunk. ``delta`` is the
        UTF-8-decoded string the LLM client received from the upstream
        provider; the client owns the byte-level boundary safety (it
        joins streaming bytes into a valid str before invoking us).

        Steps:

        1. Append ``delta`` to ``_buffer``.
        2. Parse the full buffer via :attr:`_parser`.
        3. Extract the current ``speech`` value (string or None).
        4. If it has grown past ``_emitted_speech``, emit a
           ``speech_delta`` event carrying ONLY the new suffix.
        5. Update ``_emitted_speech`` to the full current value.

        The emit is awaited so a slow WS socket can back-pressure the
        producer (matches the legacy ``ws_events.emit`` contract). If
        the emit fails, we log + swallow: a bad WS forward must not
        crash the turn (the rest of the orchestrator pipeline still
        needs to dispatch the eventual tool call).
        """

        if self._finalised:
            # Defensive: a caller that hands us bytes after finalize()
            # is a bug — log loudly and ignore.
            _logger.warning(
                "stream_emitter.feed_after_finalize",
                msg_id=self._msg_id,
                delta_chars=len(delta),
            )
            return
        if not delta:
            return

        self._buffer += delta
        parsed = self._parser.parse(self._buffer)
        if parsed is None:
            return

        raw_speech = parsed.get("speech")
        if not isinstance(raw_speech, str):
            return

        # Compute the new-suffix delta. We compare to the already-emitted
        # prefix so an LLM that backs up (rare, but observed with some
        # local models retrying mid-stream) doesn't double-emit. If the
        # current parse contradicts what we already emitted (e.g. parser
        # now sees ``"He"`` after we already emitted ``"Hel"``), we
        # silently skip — the emit-once contract on the WS side wins.
        if not raw_speech.startswith(self._emitted_speech):
            return
        new_suffix = raw_speech[len(self._emitted_speech) :]
        if not new_suffix:
            return

        await self._safe_emit(
            {
                "type": "speech_delta",
                "msg_id": self._msg_id,
                "delta": new_suffix,
            }
        )
        self._emitted_speech = raw_speech

    async def finalize(self, final_arguments: dict[str, Any] | None = None) -> None:
        """Emit the closing ``ui_payload`` frame (when applicable).

        ``final_arguments`` is the fully-parsed argument dict, normally
        the output of :func:`json.loads` on the complete buffer
        performed by the LLM client once the stream closes. When
        ``None`` we fall back to parsing :attr:`_buffer` ourselves
        — this lets tests drive the emitter without round-tripping
        through the LLM client.

        A ``ui_payload`` frame is emitted iff:

        - The arguments contain a key ``"ui"``.
        - The value is non-null (not ``None``).
        - The value is a dict-like (matches the ``{component, props}``
          shape the orchestrator + frontend agree on). A non-dict value
          (string, number, list, bool) is logged + ignored — those would
          fail the orchestrator's ``_coerce_say_ui`` anyway.

        :meth:`finalize` is idempotent: a second call is a no-op so
        defensive callers can invoke it from a ``finally`` block.
        """

        if self._finalised:
            return
        self._finalised = True

        if final_arguments is None:
            final_arguments = self._parser.parse(self._buffer)

        if not isinstance(final_arguments, dict):
            return

        ui = final_arguments.get("ui")
        if ui is None:
            # Empty / null / missing ui → no overlay open. PRD AC.
            return
        # Normalise into the canonical ``{component, props}`` shape the
        # frontend overlay reads (``props.content``). Tolerates the flat
        # ``{component, content}`` variant the LLM sometimes emits — without
        # this the raw dict reaches the frontend with no ``props`` and the
        # overlay renders empty. ``None`` means non-dict / no component.
        descriptor = coerce_component_descriptor(ui)
        if descriptor is None:
            _logger.warning(
                "stream_emitter.ui_payload_unexpected_shape",
                msg_id=self._msg_id,
                ui_type=type(ui).__name__,
            )
            return
        if self._ui_emitted:
            return
        self._ui_emitted = True

        await self._safe_emit(
            {
                "type": "ui_payload",
                "msg_id": self._msg_id,
                "ui": descriptor.model_dump(),
            }
        )

    async def _safe_emit(self, payload: dict[str, Any]) -> None:
        """Emit ``payload`` through ``self._emit``, swallowing failures.

        A bad WS forward (slow socket, broken pipe) must not crash the
        live turn — the rest of the orchestrator pipeline still owes
        the user the dispatch + the final ``assistant_msg``. We log the
        exception with ``structlog.exception`` so the failure surfaces
        in the debug view + log file.
        """

        try:
            await self._emit(payload)
        except Exception:  # pragma: no cover — defensive net.
            _logger.exception(
                "stream_emitter.emit_failed",
                msg_id=self._msg_id,
                event_type=payload.get("type"),
            )


__all__ = ["EmitFn", "StreamEmitter"]
