"""Server-side « Listen » voice turn — STT session lifecycle + voice events.

This is the backend half of issue 0099: it owns ONE speech-to-text turn,
from ``voice_start`` to the frozen ``stt_final``. The WS router
(:mod:`bob.ws_router`) decodes binary mic frames (tag ``0x01`` stripped,
PCM 16 kHz mono s16le) and pumps them in here; this module runs them
through an :class:`bob.stt_engine.SttEngine` session and emits the wire
events (Annexe A.2):

- ``stt_partial`` ``{turn_id, text, stable_prefix_len, ts}`` per new
  whisper hypothesis;
- ``stt_final`` ``{turn_id, text, ts}`` when the turn is frozen.

Both go out via :func:`bob.event_bus_v2.emit_event` with ``category="voice"``
so they land in the ``/ws/debug`` ring buffer AND fan out to the chat
client. **Privacy (Annexe A.2)**: ``stt_*`` carries user transcript text →
the full ``payload`` reaches the client, but a **scrubbed** ``debug_payload``
(truncated/masked text) is what lands in the ring buffer / JSONL sink.

Correlation: a fresh hex ``turn_id`` is minted at :meth:`start` and
installed in the ``current_turn_id`` ContextVar for the turn's scope so the
ring-buffer :class:`bob.debug_log.DebugEvent` also carries it (the wire
payload carries it explicitly either way).

Degradation (Annexe G):

- whisper model absent → :meth:`start` emits a ``stt_preparing`` toast,
  loads (downloading) the model in a thread, then ``stt_ready``;
- the engine fails to load (no ``pywhispercpp`` / model) OR raises
  mid-turn → the turn is aborted cleanly: a ``voice_turn_error`` event
  (``end_reason:error``) is emitted and the turn returns to idle. The WS
  layer never crashes — a failed turn is a degraded turn.

The clock is a monotonic server timestamp (``ts`` in seconds, float) so
latency marks (Annexe F, future slices) and event ordering are robust to
wall-clock changes.
"""

from __future__ import annotations

import asyncio
import contextlib
import time
import uuid
from contextvars import Token

import structlog

from bob.config import Settings, get_settings
from bob.debug_log import current_turn_id
from bob.event_bus_v2 import emit_event
from bob.stt_engine import (
    SttEngine,
    SttEngineUnavailableError,
    SttFinal,
    SttPartial,
    SttSession,
    decode_pcm_frame,
)

_logger = structlog.get_logger(__name__)


def _resolve_settings() -> Settings:
    """Best-effort :class:`Settings` that does not require LLM env to be set.

    In the running backend ``get_settings()`` always succeeds (the app boots
    only with valid LLM config). But :class:`VoiceTurn` is also driven by the
    standalone attest harness / ``bob attest`` CLI, which has no LLM env — and
    it only ever needs the ``STT_*`` fields. Fall back to a validation-free
    construction (field defaults) so the « Listen » path is self-contained,
    without weakening the app's own boot-time LLM validation.
    """

    try:
        return get_settings()
    except Exception:
        return Settings.model_construct()


def _scrub_text(text: str, *, max_chars: int) -> str:
    """Truncate user transcript text for the debug ring buffer (privacy).

    Keeps the first ``max_chars`` characters and replaces the remainder with
    an elision marker carrying the masked length so the debug feed still
    shows *that* a transcript happened and roughly how long it was, without
    leaking the content. The full text always reaches the client (the
    ``payload`` passed to :func:`emit_event`), only the ``debug_payload`` is
    scrubbed.
    """

    if max_chars <= 0:
        return f"[masked {len(text)} chars]"
    if len(text) <= max_chars:
        return text
    return f"{text[:max_chars]}… [+{len(text) - max_chars} chars]"


class VoiceTurn:
    """One active speech-to-text turn for a WS session.

    Construct with the engine + session id; call :meth:`start` once, then
    :meth:`feed_frame` per binary mic frame, then exactly one of
    :meth:`finalize` (endpoint / ``voice_stop`` / socket close with a
    transcript) — :meth:`abort` is the degradation exit. All public methods
    are idempotent past their first terminal call so a racing ``voice_stop``
    + socket-close can't double-emit.
    """

    def __init__(
        self,
        *,
        engine: SttEngine,
        session_id: str,
        settings: Settings | None = None,
    ) -> None:
        self._engine = engine
        self._session_id = session_id
        self._settings = settings or _resolve_settings()
        self.turn_id: str = uuid.uuid4().hex
        self._session: SttSession | None = None
        self._turn_token: Token[str | None] | None = None
        self._finished = False
        # Monotonic count of ``stt_partial`` events emitted this turn. The
        # full-duplex loop (issue 0100) reads it to fire the FSM's
        # ``stt_partial`` self-loop only on *real* progress, without
        # re-plumbing the emit path. Additive — the 0099 surface is unchanged.
        self._partial_count = 0

    # -- lifecycle -----------------------------------------------------------

    async def start(self) -> bool:
        """Open the STT session for this turn; emit prep/ready toasts.

        Returns ``True`` when the session opened and is ready to accept
        frames, ``False`` when the engine could not load (already aborted via
        :meth:`abort`). Lazy-download (Annexe G): when the model is not cached
        a ``stt_preparing`` toast is emitted, the model is loaded in a thread,
        then ``stt_ready``. A load failure aborts the turn cleanly.
        """

        # Correlate every ring-buffer event emitted during this turn.
        self._turn_token = current_turn_id.set(self.turn_id)

        if not self._model_cached():
            await self._emit("stt_preparing", {})
            try:
                await asyncio.to_thread(self._engine.preload)
            except Exception as exc:
                await self.abort(reason=f"model download failed: {exc}")
                return False
            await self._emit("stt_ready", {})

        try:
            self._session = await asyncio.to_thread(self._engine.open_session, self.turn_id)
        except SttEngineUnavailableError as exc:
            await self.abort(reason=str(exc))
            return False
        except Exception as exc:  # pragma: no cover - defensive
            await self.abort(reason=f"stt session open failed: {exc}")
            return False
        return True

    async def feed_raw_frame(self, data: bytes) -> None:
        """Decode a raw binary WS frame (tag ``0x01``) and feed its PCM.

        A single malformed frame is dropped (logged at debug, no abort) — the
        contract is permissive on the wire so one bad packet never kills a
        turn. A transcription failure, by contrast, aborts the turn (Annexe
        G: STT fails mid-turn).
        """

        from bob.stt_engine import SttFrameError

        try:
            pcm = decode_pcm_frame(data)
        except SttFrameError:
            _logger.debug(
                "voice_turn.bad_frame",
                session_id=self._session_id,
                turn_id=self.turn_id,
                nbytes=len(data),
            )
            return
        await self.feed_frame(pcm)

    async def feed_frame(self, pcm: bytes) -> None:
        """Feed one decoded PCM payload; emit any resulting ``stt_partial``."""

        if self._finished or self._session is None:
            return
        try:
            partials = await asyncio.to_thread(self._session.accept_frame, pcm)
        except Exception as exc:
            await self.abort(reason=f"stt failed mid-turn: {exc}")
            return
        for partial in partials:
            await self._emit_partial(partial)

    async def finalize(self) -> SttFinal | None:
        """Freeze the turn; emit ``stt_final``. Returns the final transcript.

        Idempotent: a second call (e.g. socket-close after ``voice_stop``) is
        a no-op returning ``None``.
        """

        if self._finished or self._session is None:
            self._reset_turn_ctx()
            return None
        self._finished = True
        try:
            final = await asyncio.to_thread(self._session.finalize)
        except Exception as exc:
            await self._abort_already_finishing(reason=f"stt finalize failed: {exc}")
            return None
        await self._emit_final(final)
        self._reset_turn_ctx()
        return final

    async def abort(self, *, reason: str) -> None:
        """Abort the turn cleanly (Annexe G): ``end_reason:error`` → idle.

        Emits a ``voice_turn_error`` voice event (severity error) carrying the
        reason and ``end_reason:"error"`` so the client returns to idle and
        shows a toast. Idempotent. Never raises.
        """

        if self._finished:
            self._reset_turn_ctx()
            return
        await self._abort_already_finishing(reason=reason)

    async def _abort_already_finishing(self, *, reason: str) -> None:
        self._finished = True
        _logger.warning(
            "voice_turn.aborted",
            session_id=self._session_id,
            turn_id=self.turn_id,
            reason=reason,
        )
        with contextlib.suppress(Exception):
            if self._session is not None:
                self._session.close()
        await self._emit(
            "voice_turn_error",
            {"reason": reason, "end_reason": "error"},
            severity="error",
        )
        self._reset_turn_ctx()

    # -- emit helpers --------------------------------------------------------

    @property
    def partial_count(self) -> int:
        """Monotonic count of ``stt_partial`` events emitted this turn (0100)."""

        return self._partial_count

    async def _emit_partial(self, partial: SttPartial) -> None:
        self._partial_count += 1
        ts = self._now()
        payload = {
            "type": "stt_partial",
            "turn_id": self.turn_id,
            "text": partial.text,
            "stable_prefix_len": partial.stable_prefix_len,
            "ts": ts,
        }
        debug_payload = {
            **payload,
            "text": self._scrub(partial.text),
        }
        await emit_event(
            payload,
            category="voice",
            severity="debug",
            source="bob.voice_turn.stt_partial",
            summary=f"stt_partial (turn={self.turn_id})",
            debug_payload=debug_payload,
        )

    async def _emit_final(self, final: SttFinal) -> None:
        ts = self._now()
        payload = {
            "type": "stt_final",
            "turn_id": self.turn_id,
            "text": final.text,
            "ts": ts,
        }
        debug_payload = {
            **payload,
            "text": self._scrub(final.text),
        }
        await emit_event(
            payload,
            category="voice",
            severity="info",
            source="bob.voice_turn.stt_final",
            summary=f"stt_final (turn={self.turn_id})",
            debug_payload=debug_payload,
        )

    async def _emit(
        self,
        event_type: str,
        extra: dict[str, object],
        *,
        severity: str = "info",
    ) -> None:
        payload: dict[str, object] = {
            "type": event_type,
            "turn_id": self.turn_id,
            "ts": self._now(),
            **extra,
        }
        await emit_event(
            payload,
            category="voice",
            severity=severity,  # type: ignore[arg-type]
            source=f"bob.voice_turn.{event_type}",
            summary=f"{event_type} (turn={self.turn_id})",
        )

    # -- internals -----------------------------------------------------------

    def _model_cached(self) -> bool:
        try:
            return self._engine.is_model_cached()
        except Exception:
            return True

    def _scrub(self, text: str) -> str:
        return _scrub_text(text, max_chars=self._settings.STT_DEBUG_TEXT_MAX_CHARS)

    def _now(self) -> float:
        return round(time.monotonic(), 6)

    def _reset_turn_ctx(self) -> None:
        if self._turn_token is not None:
            # ContextVar.reset raises if the token was created in a different
            # context (e.g. the turn spanned a thread hop). Best-effort: a
            # failed reset just leaves the var set to this turn id until the
            # next turn overwrites it — harmless for correlation.
            with contextlib.suppress(ValueError):
                current_turn_id.reset(self._turn_token)
            self._turn_token = None
