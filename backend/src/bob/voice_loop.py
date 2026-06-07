"""Full-duplex turn loop — audio-in → existing brain → audio-out (issue 0100).

PRD 0016 / Annexe B + F. This is the **bare** full-duplex loop: it stitches the
already-built « Listen » STT spine (:class:`bob.voice_turn.VoiceTurn`, issue
0099) to the already-built Jarvis say-path + Kokoro TTS (the text turn's
``process_user_message`` → ``speech_delta`` / ``audio_chunk`` path) via the
real-time turn FSM (:class:`bob.turn_fsm.TurnFsm`). NO Thinker, NO Draft, NO
barge-in (those are 0101-0104) - this proves the audio->brain->audio skeleton and
the FSM lifecycle.

What it owns
------------

For ONE WS session, across one ``voice_start`` … ``voice_stop`` window:

- a :class:`bob.vad.EnergyVad` + :class:`bob.endpointer.Endpointer` over the
  inbound mic frames (the same decoded PCM the STT session consumes);
- a :class:`bob.turn_fsm.TurnFsm` it drives from the VAD / STT / TTS edges,
  emitting a ``turn_state`` voice event (Annexe A.2) on every transition;
- the latency marks (Annexe F basics): ``t_first_mic_frame``, ``t_endpoint``,
  ``t_first_audio_chunk`` - emitted in a ``turn_latency`` voice event at turn
  end;
- the glue that, at ``endpoint``, freezes the STT transcript and hands it to a
  caller-supplied :data:`SayPathDriver` (the ws_router wires the orchestrator +
  TTS there) so a voice turn converges on the EXACT say-path a ``client_text``
  turn uses.

STT lifecycle (keeps 0099 zero-regression)
------------------------------------------

The mic is armed on ``voice_start`` (which calls :meth:`start`): the loop opens
a :class:`VoiceTurn` STT session *immediately* and feeds it every frame — so the
0099 contract (``voice_start`` → frames → ``voice_stop`` → ``stt_final``) holds
unchanged even when the VAD never fires (e.g. silent test frames). The FSM is a
layer *on top*: when the energy VAD detects speech the FSM moves
``idle`` → ``user_speaking`` (re-using the open STT turn's id), the silence
floor fires ``endpoint`` (freeze + say-path), and ``tts_end`` returns to idle.
After a turn ends the loop opens a fresh STT session for the next utterance so
back-to-back turns work within one armed window. ``voice_stop`` / socket close
finalize the open STT turn and tear the FSM down.

Why a driver callback rather than importing the orchestrator here?
------------------------------------------------------------------

The say-path needs the live WebSocket (to stream ``audio_chunk`` frames) and
the per-session orchestrator — both owned by :mod:`bob.ws_router`. Keeping the
effectful say-path behind a small protocol lets this module stay testable with
a fake driver and keeps the asyncio / WS specifics in the router. The FSM stays
pure; the loop is the thin imperative shell that interprets its transitions.
"""

from __future__ import annotations

import asyncio
import contextlib
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Protocol

import structlog

from bob.config import Settings
from bob.endpointer import Endpointer
from bob.event_bus_v2 import emit_event
from bob.stt_engine import SttFrameError, decode_pcm_frame
from bob.turn_fsm import Transition, TurnEvent, TurnFsm, TurnState
from bob.vad import EnergyVad, VadEvent, rms_normalised
from bob.voice_turn import VoiceTurn

_logger = structlog.get_logger(__name__)


class SayPathDriver(Protocol):
    """Effectful say-path the loop invokes at ``endpoint`` (wired by ws_router).

    Given the frozen final transcript, run the existing Jarvis brain
    (``process_user_message``) and stream the reply's TTS audio out on the
    session's WebSocket, invoking ``on_first_audio`` exactly once just before
    the first ``audio_chunk`` leaves (so the loop can mark
    ``t_first_audio_chunk`` + flip the FSM into ``bob_speaking``). Returns when
    the outbound audio has finished (or there was none) so the loop can flip the
    FSM back to ``idle``. Implementations must not raise on a degraded turn — a
    turn that produced no audio simply never calls ``on_first_audio``.
    """

    async def __call__(
        self,
        transcript: str,
        *,
        turn_id: str,
        on_first_audio: Callable[[], Awaitable[None]],
    ) -> None: ...


@dataclass
class _Marks:
    """Annexe F latency marks for one turn (monotonic seconds; ``None`` = unset)."""

    t_first_mic_frame: float | None = None
    t_endpoint: float | None = None
    t_first_audio_chunk: float | None = None

    def as_payload(self) -> dict[str, float]:
        """The non-null marks as a plain dict (the ``turn_latency.marks`` body)."""

        out: dict[str, float] = {}
        if self.t_first_mic_frame is not None:
            out["t_first_mic_frame"] = self.t_first_mic_frame
        if self.t_endpoint is not None:
            out["t_endpoint"] = self.t_endpoint
        if self.t_first_audio_chunk is not None:
            out["t_first_audio_chunk"] = self.t_first_audio_chunk
        return out

    def derived(self) -> dict[str, float]:
        """Derived metrics (Annexe F): ``endpoint_to_first_audio_ms`` when both marks exist."""

        out: dict[str, float] = {}
        if self.t_endpoint is not None and self.t_first_audio_chunk is not None:
            out["endpoint_to_first_audio_ms"] = round(
                (self.t_first_audio_chunk - self.t_endpoint) * 1000.0, 3
            )
        return out


@dataclass
class FullDuplexLoop:
    """Per-session full-duplex turn loop (issue 0100).

    Construct with a ``voice_turn_factory`` (mints a fresh STT-backed
    :class:`VoiceTurn` per utterance), the say-path driver, the resolved
    :class:`Settings` (thresholds) and the session id. Call :meth:`start` on
    ``voice_start``, pump mic frames through :meth:`feed_raw_frame`, and call
    :meth:`stop` on ``voice_stop`` / socket close.
    """

    voice_turn_factory: Callable[[], VoiceTurn]
    say_path: SayPathDriver
    settings: Settings
    session_id: str

    _fsm: TurnFsm = field(default_factory=TurnFsm)
    _vad: EnergyVad = field(init=False)
    _endpointer: Endpointer = field(init=False)
    _turn: VoiceTurn | None = None
    _marks: _Marks = field(default_factory=_Marks)
    _say_task: asyncio.Task[None] | None = None
    _stopped: bool = False
    _started: bool = False

    def __post_init__(self) -> None:
        frame_ms = self._frame_ms()
        self._vad = EnergyVad(
            speech_rms=self.settings.VAD_SPEECH_RMS,
            pause_frames=max(1, round(self.settings.VAD_PAUSE_MS / frame_ms)),
            frame_ms=frame_ms,
        )
        self._endpointer = Endpointer(
            silence_floor_frames=max(1, round(self.settings.ENDPOINT_SILENCE_MS / frame_ms)),
            speech_rms=self.settings.VAD_SPEECH_RMS,
        )

    # -- public API ----------------------------------------------------------

    @property
    def state(self) -> TurnState:
        """Current FSM state (observability / tests)."""

        return self._fsm.state

    async def start(self) -> bool:
        """Arm the mic: open the first STT session (0099 ``voice_start`` semantics).

        Returns ``True`` when the STT session opened (the loop is ready to
        accept frames), ``False`` when the engine could not load — in which case
        the VoiceTurn already emitted its abort event (Annexe G) and the loop is
        inert (frames are dropped). Idempotent: a second call is a no-op.
        """

        if self._started:
            return self._turn is not None
        self._started = True
        return await self._open_stt_turn()

    async def feed_raw_frame(self, data: bytes) -> None:
        """Decode one binary mic frame and drive STT + VAD + Endpointer + FSM.

        A malformed frame is dropped (the 0099 contract — one bad packet never
        kills a turn). The decoded PCM is fanned out: the active STT turn
        (partials / final — 0099), the VAD (speech / pause edges) and the
        Endpointer (silence floor). The first frame stamps ``t_first_mic_frame``.
        Frames that arrive while Bob is mid-reply (``thinking`` /
        ``bob_speaking``) still feed STT but cannot open a new turn — there is no
        barge-in in this slice (0101).
        """

        if self._stopped:
            return
        try:
            pcm = decode_pcm_frame(data)
        except SttFrameError:
            _logger.debug("voice_loop.bad_frame", session_id=self.session_id, nbytes=len(data))
            return

        if self._marks.t_first_mic_frame is None:
            self._marks.t_first_mic_frame = self._now()

        # One RMS pass; the same per-frame speech/silence decision feeds both
        # the VAD (speech/pause edges) and the Endpointer (silence floor).
        is_speech = rms_normalised(pcm) >= self.settings.VAD_SPEECH_RMS

        # 1) Feed STT first so the 0099 partial/final path is identical whether
        #    or not the FSM ever leaves idle (silent test frames never trip the
        #    VAD, but must still transcribe).
        turn = self._turn
        advanced = False
        if turn is not None:
            advanced = await self._feed_stt(turn, pcm)

        # 2) VAD edge — opens a turn (idle -> user_speaking) or, while thinking,
        #    the legal "user resumes" edge.
        vad_event = self._vad.observe(is_speech=is_speech)
        if vad_event is VadEvent.SPEECH_START:
            await self._on_speech_start()
        elif vad_event is VadEvent.PAUSE and self._fsm.state is TurnState.USER_SPEAKING:
            await self._dispatch(TurnEvent.VAD_PAUSE, turn_id=self._fsm.turn_id)

        # 3) An STT partial nudges the FSM's stt_partial self-loop, but only
        #    while the user holds the floor (a partial flushed during finalize
        #    after endpoint must not move the FSM).
        if advanced and self._fsm.state is TurnState.USER_SPEAKING:
            await self._dispatch(TurnEvent.STT_PARTIAL, turn_id=self._fsm.turn_id)

        # 4) Endpointer — the silence floor. Only meaningful while the user has
        #    the floor; firing closes the turn and launches the say-path.
        if self._fsm.state is TurnState.USER_SPEAKING and self._endpointer.observe(
            is_speech=is_speech
        ):
            await self._on_endpoint()

    async def join(self) -> None:
        """Await the in-flight say-path task, if any (test / shutdown helper).

        The say-path runs as a background task launched at ``endpoint`` so the
        frame pump never blocks on a long generation. Tests (and an orderly
        shutdown that wants the reply to finish) call this to await its
        completion deterministically. A cancelled task is suppressed. Does not
        cancel — use :meth:`stop` for the kill-switch path.
        """

        task = self._say_task
        if task is not None and not task.done():
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task

    async def stop(self) -> None:
        """Tear the loop down (``voice_stop`` / socket close). Idempotent.

        Cancels any in-flight say-path task, finalizes the open STT turn, and
        drives the FSM's universal ``voice_stop`` edge so a partially-spoken turn
        returns to idle cleanly.
        """

        if self._stopped:
            return
        self._stopped = True
        await self._cancel_say_task()
        turn = self._turn
        self._turn = None
        if turn is not None:
            with contextlib.suppress(Exception):
                await turn.finalize()
        await self._dispatch(TurnEvent.VOICE_STOP)

    # -- FSM-driven steps ----------------------------------------------------

    async def _open_stt_turn(self) -> bool:
        """Mint + open a fresh STT VoiceTurn for the next utterance.

        Returns ``True`` on success. On STT load failure the VoiceTurn emitted
        its own abort (Annexe G); we leave ``_turn`` ``None`` so frames are
        dropped until the next ``voice_start``.
        """

        turn = self.voice_turn_factory()
        started = await turn.start()
        if not started:
            self._turn = None
            return False
        self._turn = turn
        return True

    async def _on_speech_start(self) -> None:
        """Handle a ``vad_speech_start`` edge (open a turn or resume from thinking)."""

        if self._fsm.state is TurnState.IDLE:
            # Re-use the already-open STT turn's id so the FSM turn id and the
            # VoiceTurn id stay in lockstep. If STT failed to open, skip — there
            # is no turn to drive.
            turn = self._turn
            if turn is None:
                return
            transition = self._fsm.on_event(TurnEvent.VAD_SPEECH_START, turn_id=turn.turn_id)
            if transition is None:
                return
            # Fresh latency marks for this turn (keep the first-mic-frame stamp).
            self._marks = _Marks(t_first_mic_frame=self._marks.t_first_mic_frame)
            self._endpointer.reset()
            await self._emit_turn_state(transition)
            return
        # ``thinking`` -> ``user_speaking`` (user resumes before Bob spoke). This
        # is the legal Annexe B "user reprend pendant thinking" edge — NOT
        # barge-in (bob_speaking + VAD, issue 0101). Move the FSM FIRST (so the
        # cancelled say task's ``_finalize_say`` sees it no longer owns the turn
        # and becomes a no-op), then cancel the in-flight say-path generation per
        # the table's ``cancel_generation`` action, then re-open STT for the
        # resumed utterance.
        if self._fsm.state is TurnState.THINKING:
            await self._dispatch(TurnEvent.VAD_SPEECH_START, turn_id=self._fsm.turn_id)
            await self._cancel_say_task()
            if self._turn is None:
                await self._open_stt_turn()

    async def _on_endpoint(self) -> None:
        """Handle the silence-floor ``endpoint``: freeze + drive the say-path."""

        self._marks.t_endpoint = self._now()
        transition = self._fsm.on_event(TurnEvent.ENDPOINT, turn_id=self._fsm.turn_id)
        if transition is None:
            return
        await self._emit_turn_state(transition)

        # Freeze the transcript (0099 finalize → stt_final) and detach the turn.
        turn = self._turn
        self._turn = None
        transcript = ""
        if turn is not None:
            final = await turn.finalize()
            if final is not None:
                transcript = final.text

        # Launch the say-path as a background task so a long generation does not
        # block the frame pump. Frames keep feeding the next STT turn (opened
        # when we return to idle); they cannot barge in this slice.
        self._say_task = asyncio.create_task(self._run_say_path(transcript, transition.turn_id))

    async def _run_say_path(self, transcript: str, turn_id: str) -> None:
        """Drive the existing Jarvis say-path on the frozen transcript, then idle.

        ``on_first_audio`` flips ``thinking`` -> ``bob_speaking`` and stamps
        ``t_first_audio_chunk`` just before the first outbound chunk. When the
        driver returns (audio done, or none produced) we flip the FSM back to
        ``idle`` and emit the latency summary. A driver exception degrades to a
        clean idle (never crashes the session).
        """

        first_audio_seen = False

        async def _on_first_audio() -> None:
            nonlocal first_audio_seen
            if first_audio_seen:
                return
            first_audio_seen = True
            self._marks.t_first_audio_chunk = self._now()
            moved = self._fsm.on_event(TurnEvent.SPEAK_START, turn_id=turn_id)
            if moved is not None:
                await self._emit_turn_state(moved)

        try:
            await self.say_path(transcript, turn_id=turn_id, on_first_audio=_on_first_audio)
        except asyncio.CancelledError:
            raise
        except Exception:
            _logger.exception("voice_loop.say_path_failed", session_id=self.session_id)
        finally:
            await self._finalize_say(turn_id)

    async def _finalize_say(self, turn_id: str) -> None:
        """Return the FSM to idle at the end of the say-path + reopen STT.

        From ``bob_speaking`` this is the Annexe B ``tts_end`` edge. From
        ``thinking`` (the say-path produced no audio at all) ``tts_end`` is not a
        legal edge, so we drive the universal ``voice_stop`` teardown to idle —
        the turn still ends cleanly and the latency summary still fires. After
        the FSM is idle we open a fresh STT session so a following utterance in
        the same armed window is captured.

        Ownership guard: if the FSM has already moved off ``turn_id`` (the user
        resumed during ``thinking`` and a new turn was started, or ``stop`` tore
        the loop down), this say-path no longer owns the floor — we skip the FSM
        teardown entirely (the new owner / teardown drives it) and only emit the
        finished turn's latency summary.
        """

        owns_turn = self._fsm.turn_id == turn_id
        if owns_turn and self._fsm.state is TurnState.BOB_SPEAKING:
            transition = self._fsm.on_event(TurnEvent.TTS_END, turn_id=turn_id)
        elif owns_turn and self._fsm.state is TurnState.THINKING:
            transition = self._fsm.on_event(TurnEvent.VOICE_STOP)
        else:
            transition = None
        if transition is not None:
            await self._emit_turn_state(transition)
        await self._emit_turn_latency(turn_id)
        # Re-arm STT for the next utterance unless we have been stopped or a
        # resumed turn already opened one.
        if not self._stopped and self._turn is None:
            await self._open_stt_turn()

    # -- helpers -------------------------------------------------------------

    async def _dispatch(self, event: TurnEvent, *, turn_id: str | None = None) -> None:
        """Apply an FSM event and emit ``turn_state`` if it moved."""

        transition = self._fsm.on_event(event, turn_id=turn_id)
        if transition is not None:
            await self._emit_turn_state(transition)

    @staticmethod
    async def _feed_stt(turn: VoiceTurn, pcm: bytes) -> bool:
        """Feed PCM to the STT turn; return whether a partial was produced.

        ``VoiceTurn.feed_frame`` emits its own ``stt_partial`` events; we read
        its monotonic :attr:`VoiceTurn.partial_count` to learn *whether*
        anything advanced so the FSM's ``stt_partial`` self-loop fires only on
        real progress. The PCM is already decoded by the caller.
        """

        before = turn.partial_count
        await turn.feed_frame(pcm)
        return turn.partial_count != before

    async def _cancel_say_task(self) -> None:
        task = self._say_task
        self._say_task = None
        if task is not None and not task.done():
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task

    async def _emit_turn_state(self, transition: Transition) -> None:
        """Emit an Annexe A.2 ``turn_state`` voice event for one transition."""

        await emit_event(
            {
                "type": "turn_state",
                "turn_id": transition.turn_id,
                "from": transition.from_state.value,
                "to": transition.to_state.value,
                "reason": transition.reason,
                "ts": self._now(),
            },
            category="voice",
            severity="info",
            source="bob.voice_loop.turn_state",
            summary=(
                f"turn_state {transition.from_state.value}->{transition.to_state.value} "
                f"(turn={transition.turn_id})"
            ),
        )

    async def _emit_turn_latency(self, turn_id: str) -> None:
        """Emit the Annexe F ``turn_latency`` summary for a finished turn."""

        await emit_event(
            {
                "type": "turn_latency",
                "turn_id": turn_id,
                "marks": self._marks.as_payload(),
                "derived": self._marks.derived(),
                "ts": self._now(),
            },
            category="voice",
            severity="debug",
            source="bob.voice_loop.turn_latency",
            summary=f"turn_latency (turn={turn_id})",
        )

    def _frame_ms(self) -> int:
        """Mic frame duration in ms from the contract sample rate (~30 ms frames).

        The webview worklet + the harness both ship 480-sample frames at
        16 kHz = 30 ms (Annexe A.1). We derive it from ``STT_SAMPLE_RATE`` and a
        480-sample frame so the VAD / endpoint windows stay engine-relative.
        """

        rate = self.settings.STT_SAMPLE_RATE or 16_000
        return max(1, round(480 * 1000 / rate))

    @staticmethod
    def _now() -> float:
        return round(time.monotonic(), 6)
