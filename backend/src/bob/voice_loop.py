"""Full-duplex turn loop — audio-in → existing brain → audio-out (issue 0100).

PRD 0016 / Annexe B + F. This is the full-duplex loop: it stitches the
already-built « Listen » STT spine (:class:`bob.voice_turn.VoiceTurn`, issue
0099) to the already-built Jarvis say-path + Kokoro TTS (the text turn's
``process_user_message`` → ``speech_delta`` / ``audio_chunk`` path) via the
real-time turn FSM (:class:`bob.turn_fsm.TurnFsm`). Issue 0100 built the bare
audio→brain→audio skeleton + FSM lifecycle; issue 0101 adds **barge-in** (the
``bob_speaking`` interrupt). Issue 0102 wires the real **ThinkerLoop** behind the
0100 symbolic ``start_thinker`` / ``feed_thinker`` actions (the ``on_thinker_*``
hooks below). Issue 0103 makes the endpoint **semantic**: the loop routes the
Thinker's ``user_turn_complete`` (+ the STT stable-prefix confirmation) into the
:class:`bob.endpointer.Endpointer`, so a confirmed complete clause fires
``t_endpoint`` EARLIER than the silence floor (Annexe B + H). Issue 0104 adds the
**SpeculativeDraft** (the anticipation capstone): a ``draft`` mini model pre-writes
the conversational reply on the PARTIAL transcript IN PARALLEL with the Thinker, and
at the endpoint a pure commit gate (prefix fast-path -> similarity guard -> discard)
decides whether to adopt it verbatim into the say-path (``prepared_reply``) for a
near-instant reply, or regenerate COLD.

Barge-in (issue 0101, Annexe B + F)
-----------------------------------

While Bob holds the floor (``bob_speaking``), the inbound mic frames still feed
STT (the 0100 seam) AND now feed a :class:`bob.bargein.BargeInController`: once
it confirms a continuous-speech window (~200-300 ms, configurable — filters
short backchannels), the loop fires the ``bargein_confirmed`` FSM edge and
performs its Annexe B actions — cancel the in-flight say-path (which cancels the
LLM stream + the TTS), commit the text Bob actually *played*
(``committed_spoken_text``, derived from the say-path's per-sentence playback
progress) to the Jarvis history, restart the Thinker (no-op hook today), and
re-arm STT for the resumed utterance. It emits a ``bargein`` voice event
(Annexe A.2) + the ``t_bargein_detected`` / ``t_cut`` latency marks (Annexe F),
whose derived ``bargein_cut_ms`` targets <300 ms.

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
from collections.abc import Awaitable, Callable, Coroutine
from dataclasses import dataclass, field
from typing import Any, Protocol

import structlog

from bob import turn_metrics
from bob.backchannel import BackchannelDecider
from bob.bargein import BargeInConfirmation, BargeInController
from bob.config import Settings
from bob.endpointer import Endpointer
from bob.event_bus_v2 import emit_event
from bob.latency import DerivedValue, TurnLatency
from bob.speculative_draft import DraftDecision
from bob.stt_engine import SttFrameError, decode_pcm_frame
from bob.task_supervisor import create_supervised_task
from bob.turn_fsm import Transition, TurnEvent, TurnFsm, TurnState
from bob.turn_watchdog import TURN_TIMEOUT_FALLBACK_SPEECH, TurnTimeoutError, TurnWatchdog
from bob.vad import EnergyVad, VadEvent, rms_normalised
from bob.voice_turn import VoiceTurn, _scrub_text

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

    ``on_spoken_progress`` (issue 0101) is invoked after each *fully streamed*
    unit of speech (a sentence's chunks) with the **cumulative cleaned text Bob
    has actually played so far**. The loop holds the latest value so that, on a
    barge-in cut, ``committed_spoken_text`` is exactly what left the speaker
    before the interrupt — never the un-played tail. A driver that streams
    nothing simply never calls it (``committed_spoken_text`` stays empty).

    ``on_audio_chunk`` (PRD 0016 / issue 0109) is invoked with the raw PCM bytes
    + sample rate of each outbound TTS block, just after it leaves the socket.
    The loop accumulates these into the turn's ``tts_out`` recording so the
    persistence hook can write the WAV. ``None`` skips it (tests / the text
    path that does not persist a voice turn).
    """

    async def __call__(
        self,
        transcript: str,
        *,
        turn_id: str,
        on_first_audio: Callable[[], Awaitable[None]],
        on_spoken_progress: Callable[[str], Awaitable[None]] | None = None,
        on_audio_chunk: Callable[[bytes, int], Awaitable[None]] | None = None,
        prepared_reply: str | None = None,
    ) -> None:
        """``prepared_reply`` (PRD 0016 / issue 0104): a COMMITTED speculative draft.

        When the endpoint's commit gate adopts a pre-written draft, the loop hands
        its text here so the driver skips the cold Speaker generation and speaks
        the draft verbatim (trivial validation — it is already text) → TTS, while
        still persisting it as the assistant turn. ``None`` (the cold path) keeps
        the normal say-path: the driver runs ``process_user_message`` on the
        transcript exactly as in 0100/0103.
        """
        ...


@dataclass(frozen=True)
class PersistedTurn:
    """Everything the persistence hook needs to write one finalized turn.

    PRD 0016 / issue 0109 (Annexe E). Assembled by the loop at every finalize
    exit path (endpoint→reply done, barge-in cut, ``voice_stop`` / socket close,
    clean error) and handed to the injected ``persist_turn`` hook, which owns
    the DB row + WAV files + Jarvis-history link + the persistence event. The
    loop never touches SQLite/disk itself — it only knows the turn's shape.

    - ``end_reason`` — 'completed' | 'bargein' | 'voice_stop' | 'error'.
    - ``final_transcript`` — the frozen ``stt_final`` text (may be empty).
    - ``spoken_text`` — what Bob ACTUALLY played (the committed prefix on a
      barge-in cut; the full reply otherwise; empty when Bob never spoke).
    - ``marks`` / ``derived`` — the Annexe F latency body for ``latency_json``
      (centralized in :class:`bob.latency.TurnLatency`; ``derived`` carries the
      ms deltas plus the feature-gated ``backchannel_ms`` / ``draft_hit``).
    - ``mic_pcm`` — concatenated s16le mic frames captured this turn.
    - ``mic_sample_rate`` — the mic contract rate (``STT_SAMPLE_RATE``).
    - ``tts_pcm`` — concatenated s16le TTS blocks Bob played (empty if none).
    - ``tts_sample_rate`` — the TTS model rate of those blocks (0 when none).
    """

    turn_id: str
    end_reason: str
    final_transcript: str
    spoken_text: str
    marks: dict[str, float]
    derived: dict[str, DerivedValue]
    mic_pcm: bytes
    mic_sample_rate: int
    tts_pcm: bytes
    tts_sample_rate: int


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
    #: Restart-the-Thinker hook fired on the barge-in ``start_thinker`` action
    #: (issue 0101). Receives the (interrupted) ``turn_id``. Issue 0102 wires the
    #: real ThinkerLoop (re)start here; ``None`` keeps the 0101 no-op.
    on_thinker_restart: Callable[[str], Awaitable[None]] | None = None
    #: ThinkerLoop hooks (PRD 0016 / issue 0102 — the real wiring behind the
    #: 0100 symbolic ``start_thinker`` / ``feed_thinker`` actions). ``on_thinker_start``
    #: arms the loop for a turn (``idle -> user_speaking``); ``on_thinker_feed``
    #: hands it each ``stt_partial`` (debounced inside the loop, Annexe H);
    #: ``on_thinker_stop`` cooperatively cancels it on ``endpoint`` / ``voice_stop``
    #: (cancel + grace + hard-kill). All ``None`` by default so the bare-loop tests
    #: (no Thinker) keep their exact behaviour.
    on_thinker_start: Callable[[str], None] | None = None
    on_thinker_feed: Callable[[str], Awaitable[None]] | None = None
    on_thinker_stop: Callable[[], Awaitable[None]] | None = None
    #: Semantic-endpoint signal source (PRD 0016 / issue 0103, Annexe B + H). A
    #: pure read of the Thinker's LATEST ``user_turn_complete`` from the shared
    #: :class:`bob.live_transcript_state.LiveTranscriptState` (wired by ws_router
    #: as ``lambda: <latest snapshot>.user_turn_complete``). The loop polls it on
    #: every advancing partial while the user holds the floor and routes the
    #: value into the :class:`bob.endpointer.Endpointer`, which fires the endpoint
    #: EARLY only once a stable partial confirms the clause (anti-false-positive).
    #: ``None`` keeps the silence-floor-only behaviour (bare loop / no Thinker).
    thinker_complete: Callable[[], bool] | None = None
    #: Backchannel trigger source (PRD 0016 / issue 0105, Annexe B + A.2). A pure
    #: read of the Thinker's LATEST ``backchannel`` token from the shared
    #: :class:`bob.live_transcript_state.LiveTranscriptState` (wired by ws_router
    #: as ``lambda: <latest snapshot>.backchannel``). The loop consults it on each
    #: ``vad_pause`` during ``user_speaking`` (the FSM ``maybe_backchannel`` hook)
    #: and, gated by the :class:`bob.backchannel.BackchannelDecider`, plays a short
    #: acknowledgement WITHOUT a floor transition. ``None`` (bare loop / no
    #: Thinker) means no backchannel ever fires — every pause stays silent.
    backchannel_trigger: Callable[[], str | None] | None = None
    #: Short-token synthesis hook for a gated backchannel (PRD 0016 / issue 0105).
    #: Receives the (interrupted) ``turn_id`` + the brief token; synthesises it via
    #: Kokoro (fake TTS under attest) and streams it out WITHOUT touching the FSM
    #: floor. Wired by ws_router; ``None`` skips the audio (the decision + event
    #: still fire so the gate is attestable in narrow test setups). Dispatched as
    #: a supervised fire-and-forget task (PRD 0018 / issue 0120): the frame loop
    #: never awaits the synthesis, and a failure is logged — a backchannel hiccup
    #: must never perturb the live user turn.
    backchannel_tts: Callable[[str, str], Awaitable[None]] | None = None
    #: SpeculativeDraft hooks (PRD 0016 / issue 0104 — the anticipation capstone).
    #: They MIRROR the Thinker hooks: ``on_draft_start`` arms the drafter for a
    #: turn (``idle -> user_speaking``); ``on_draft_feed`` hands it each
    #: ``stt_partial`` (debounced inside the loop, so a draft is pre-written while
    #: the user still speaks); ``on_draft_stop`` cooperatively cancels it on
    #: ``endpoint`` / ``voice_stop`` (cancel + grace + hard-kill), leaving the
    #: latest landed draft readable for the commit gate. All ``None`` (the bare
    #: loop, or the ``draft`` model unavailable — Annexe G) means NO anticipation:
    #: every turn regenerates COLD, byte-for-byte as 0100/0103.
    on_draft_start: Callable[[str], None] | None = None
    on_draft_feed: Callable[[str], Awaitable[None]] | None = None
    on_draft_stop: Callable[[], Awaitable[None]] | None = None
    #: Commit gate (PRD 0016 / issue 0104, Annexe F). A PURE read run at the
    #: endpoint on the FINAL frozen transcript: it returns a
    #: :class:`bob.speculative_draft.DraftDecision` — ``committed`` (adopt the
    #: pre-written draft text into the say-path) via the prefix fast-path or the
    #: similarity guard, else ``discarded`` (regenerate COLD). ``None`` keeps the
    #: cold path (no drafter). Wired by ws_router to ``drafter.commit_gate``.
    draft_commit_gate: Callable[[str], DraftDecision] | None = None
    #: Emit the terminal ``draft_status`` event for the gate verdict (Annexe A.2).
    #: Receives the ``turn_id`` + the :class:`DraftDecision` the gate returned, so
    #: the marks (``t_commit_decision`` / ``draft_hit``) and the wire event stay in
    #: one place. ``None`` skips the event (the marks still flip). Wired to
    #: ``drafter.emit_decision``.
    draft_emit_decision: Callable[[str, DraftDecision], Awaitable[None]] | None = None
    #: Commit-to-history hook for the barge-in ``commit_spoken_partial`` action
    #: (issue 0101): persist what Bob actually played before the cut. Defaults to
    #: the Jarvis store append (wired by ws_router); ``None`` = skip (tests).
    commit_spoken: Callable[[str, str], Awaitable[None]] | None = None
    #: Persistence hook fired once per turn at every finalize exit path (PRD
    #: 0016 / issue 0109, Annexe E). Receives a :class:`PersistedTurn` snapshot
    #: (transcript, spoken text, end reason, latency marks, mic + tts PCM) and
    #: owns the DB row + WAV files + Jarvis link + the persistence event. Wired
    #: by ws_router; ``None`` = no persistence (the bare-loop tests / voice OFF).
    persist_turn: Callable[[PersistedTurn], Awaitable[None]] | None = None

    _fsm: TurnFsm = field(default_factory=TurnFsm)
    _vad: EnergyVad = field(init=False)
    _endpointer: Endpointer = field(init=False)
    _bargein: BargeInController = field(init=False)
    #: Proactivity gate for pause backchannels (issue 0105). Pure decision
    #: (relevance = Thinker trigger present; silence-decay = refractory window);
    #: the loop performs the synthesis only when it says emit. Reset per turn.
    _backchannel: BackchannelDecider = field(init=False)
    _turn: VoiceTurn | None = None
    #: Annexe F latency accumulator for the turn in flight (issue 0110). Slices
    #: stamp their marks into it; the loop emits the full ``turn_latency`` body
    #: once at finalize (completed + barge-in paths) and hands the same
    #: marks/derived to ``persist_turn``. Centralizes what the 0100 ``_Marks``
    #: did inline + adds ``t_first_partial`` / ``t_tts_end``.
    _latency: TurnLatency = field(default_factory=TurnLatency)
    _say_task: asyncio.Task[None] | None = None
    #: The in-flight fire-and-forget backchannel synthesis task (PRD 0018 /
    #: issue 0120). Spawned supervised by ``_maybe_backchannel`` (never awaited
    #: in the frame loop); ``stop`` cancels it so no synthesis outlives the
    #: session. Only the latest is tracked — the refractory window makes an
    #: overlap rare, and an orphaned earlier task is still supervised (logged).
    _backchannel_task: asyncio.Task[None] | None = None
    #: Cumulative cleaned text Bob has actually played in the active say-path
    #: (updated by the say-path's ``on_spoken_progress``); the basis for
    #: ``committed_spoken_text`` on a barge-in cut.
    _spoken_text: str = ""
    _stopped: bool = False
    _started: bool = False
    #: Per-turn audio + transcript capture for persistence (PRD 0016 / issue
    #: 0109). ``_mic_pcm`` accumulates the decoded s16le mic frames of the turn
    #: in flight; ``_tts_pcm`` accumulates the outbound TTS blocks Bob played;
    #: ``_tts_sample_rate`` is the rate of those blocks (0 until the first one);
    #: ``_final_transcript`` is the frozen ``stt_final`` text. All reset when a
    #: new turn opens (``_begin_capture``) and drained by ``_persist_turn``. A
    #: ``bytearray`` keeps the per-frame append O(1).
    _mic_pcm: bytearray = field(default_factory=bytearray)
    _tts_pcm: bytearray = field(default_factory=bytearray)
    _tts_sample_rate: int = 0
    _final_transcript: str = ""
    #: Turn ids already handed to ``persist_turn`` — the finalize exits race
    #: (``voice_stop`` after an endpoint say-path is still finishing), so we
    #: persist each turn AT MOST ONCE. Bounded implicitly by the session length.
    _persisted_turn_ids: set[str] = field(default_factory=set)

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
        self._bargein = BargeInController(confirm_ms=self.settings.BARGEIN_CONFIRM_MS)
        self._backchannel = BackchannelDecider(
            min_interval_s=max(0.0, self.settings.BACKCHANNEL_MIN_INTERVAL_MS / 1000.0)
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
        Frames that arrive while Bob is mid-reply still feed STT; during
        ``bob_speaking`` they also feed the :class:`bob.bargein.BargeInController`,
        which can cut Bob off once a confirmation window of continuous speech
        elapses (issue 0101).
        """

        if self._stopped:
            return
        try:
            pcm = decode_pcm_frame(data)
        except SttFrameError:
            _logger.debug("voice_loop.bad_frame", session_id=self.session_id, nbytes=len(data))
            return

        if self._latency.t_first_mic_frame is None:
            self._latency.t_first_mic_frame = self._now()

        # Capture the raw mic PCM for this turn's ``mic_in`` recording (PRD 0016
        # / issue 0109). Only when persistence is wired (the bare-loop tests skip
        # it). ``_begin_capture`` resets the buffer at every turn open so what we
        # persist is the utterance from speech-start onward, not stale silence.
        if self.persist_turn is not None:
            self._mic_pcm.extend(pcm)

        # One RMS pass; the same per-frame speech/silence decision feeds the VAD
        # (speech/pause edges), the Endpointer (silence floor) AND the barge-in
        # controller (confirmation window during bob_speaking).
        is_speech = rms_normalised(pcm) >= self.settings.VAD_SPEECH_RMS

        # 1) Feed STT first so the 0099 partial/final path is identical whether
        #    or not the FSM ever leaves idle (silent test frames never trip the
        #    VAD, but must still transcribe). This also keeps capturing the
        #    user's audio while Bob speaks, so the barged-in utterance is
        #    transcribed for the resumed turn.
        turn = self._turn
        advanced = False
        if turn is not None:
            advanced = await self._feed_stt(turn, pcm)

        # 1b) Barge-in (issue 0101): while Bob holds the floor, run the
        #     confirmation window. On confirmation we cut Bob and re-open the
        #     user's turn; nothing else on this frame applies (the VAD/endpoint
        #     fan-out below is for the user_speaking / idle states), so return.
        if self._fsm.state is TurnState.BOB_SPEAKING:
            confirmation = self._bargein.observe(is_speech=is_speech, now=self._now())
            if confirmation is not None:
                await self._on_bargein(confirmation)
            return

        # 2) VAD edge — opens a turn (idle -> user_speaking) or, while thinking,
        #    the legal "user resumes" edge.
        vad_event = self._vad.observe(is_speech=is_speech)
        if vad_event is VadEvent.SPEECH_START:
            await self._on_speech_start()
        elif vad_event is VadEvent.PAUSE and self._fsm.state is TurnState.USER_SPEAKING:
            # The ``user_speaking --vad_pause--> user_speaking`` self-loop whose
            # Annexe B action is ``maybe_backchannel`` (issue 0105): the turn does
            # NOT change floor — we just *maybe* drop a brief acknowledgement in
            # the pause. The decision/synthesis are gated below; the FSM move
            # itself is a no-op transition that only emits ``turn_state``.
            transition = self._fsm.on_event(TurnEvent.VAD_PAUSE, turn_id=self._fsm.turn_id)
            if transition is not None:
                await self._emit_turn_state(transition)
                if "maybe_backchannel" in transition.actions:
                    await self._maybe_backchannel(transition.turn_id)

        # 3) An STT partial nudges the FSM's stt_partial self-loop, but only
        #    while the user holds the floor (a partial flushed during finalize
        #    after endpoint must not move the FSM). The FSM's ``stt_partial``
        #    action is ``feed_thinker`` (Annexe B): hand the latest partial text
        #    to the background ThinkerLoop (debounced inside the loop, Annexe H).
        if advanced and self._fsm.state is TurnState.USER_SPEAKING:
            # Annexe F ``t_first_partial`` — the first moment this turn had ANY
            # hypothesis (the latency from speech-start to "the machine heard
            # something"). Stamp once per turn; reset at the next turn open.
            if self._latency.t_first_partial is None:
                self._latency.t_first_partial = self._now()
            await self._dispatch(TurnEvent.STT_PARTIAL, turn_id=self._fsm.turn_id)
            if turn is not None:
                if self.on_thinker_feed is not None:
                    with contextlib.suppress(Exception):
                        await self.on_thinker_feed(turn.latest_partial_text)
                # Feed the SAME partial to the SpeculativeDraft (PRD 0016 / issue
                # 0104). It runs IN PARALLEL with the Thinker on a DISTINCT role
                # client, pre-writing the conversational reply (debounced, ≤1 in
                # flight inside the drafter). No-op when unwired (Annexe G).
                if self.on_draft_feed is not None:
                    with contextlib.suppress(Exception):
                        await self.on_draft_feed(turn.latest_partial_text)

        # 3b) Semantic-endpoint signals (issue 0103, Annexe B + H). The order is
        #     normative: route the Thinker's LATEST ``user_turn_complete`` FIRST
        #     (it reflects a *prior* background pass — the snapshot lands
        #     asynchronously, so we poll it every frame the user holds the floor),
        #     so it ARMS the pending endpoint against the stable-prefix watermark
        #     as it stood BEFORE this frame's partial. THEN route this partial's
        #     ``stable_prefix_len`` — a prefix that grew past that watermark is the
        #     "next stable partial" that CONFIRMS the clause settled (Annexe H
        #     anti-false-positive). The Endpointer never fires on the raw signal.
        if self._fsm.state is TurnState.USER_SPEAKING:
            if self.thinker_complete is not None:
                complete = False
                with contextlib.suppress(Exception):
                    complete = bool(self.thinker_complete())
                self._endpointer.note_user_turn_complete(complete)
            if advanced and turn is not None:
                self._endpointer.note_stable_prefix(turn.latest_partial_stable_prefix_len)

        # 4) Endpointer — the merged net (issue 0103): the silence floor OR a
        #    CONFIRMED semantic ``user_turn_complete``. Only meaningful while the
        #    user has the floor; firing closes the turn and launches the say-path.
        if self._fsm.state is TurnState.USER_SPEAKING and self._endpointer.observe(
            is_speech=is_speech
        ):
            await self._on_endpoint()

    def note_thinker_complete(self, complete: bool) -> None:
        """Out-of-band semantic-endpoint push (PRD 0018 / issue 0120).

        Wired by ws_router to the ThinkerLoop's ``on_turn_complete`` hook: the
        instant a Thinker pass concludes, its ``user_turn_complete`` bit lands
        here — WITHOUT waiting for the inference-cadence debounce or the next
        frame's ``thinker_complete`` poll. The bit only ARMS (or withdraws) the
        pending semantic endpoint; the actual fire still happens on the next
        frame's :meth:`bob.endpointer.Endpointer.observe` under the Annexe H
        stable-prefix confirmation, so the anti-false-positive rule is intact.
        Sync + pure (a note on the endpointer) so the Thinker's pass never
        blocks on the voice loop. Dropped outside ``user_speaking`` — a late
        push from a pass that outlived its turn must not arm the next one.
        """

        if self._stopped or self._fsm.state is not TurnState.USER_SPEAKING:
            return
        self._endpointer.note_user_turn_complete(complete)

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
        # A fire-and-forget backchannel synthesis (issue 0120) must not outlive
        # the session — cancel it here (the only place it is ever awaited).
        await self._cancel_backchannel_task()
        # Annexe H — ``voice_stop`` tears the loop down: cooperatively cancel any
        # in-flight Thinker pass too (no-op when unwired / already stopped).
        if self.on_thinker_stop is not None:
            with contextlib.suppress(Exception):
                await self.on_thinker_stop()
        # Likewise tear down the SpeculativeDraft (PRD 0016 / issue 0104): cancel
        # an in-flight pass so no background drafting outlives the session.
        if self.on_draft_stop is not None:
            with contextlib.suppress(Exception):
                await self.on_draft_stop()
        turn = self._turn
        self._turn = None
        # Capture the FSM turn id + whether the open turn aborted BEFORE the
        # VOICE_STOP dispatch resets the FSM, so a mid-turn teardown persists the
        # right turn with the right end reason (PRD 0016 / issue 0109).
        active_turn_id = self._fsm.turn_id
        mid_turn = self._fsm.state is not TurnState.IDLE and bool(active_turn_id)
        if turn is not None:
            aborted = turn.aborted
            with contextlib.suppress(Exception):
                final = await turn.finalize()
                if final is not None:
                    self._final_transcript = final.text
        else:
            aborted = False
        await self._dispatch(TurnEvent.VOICE_STOP)
        # Persist the interrupted-in-flight turn (Annexe G: WS cut / toggle OFF
        # mid-turn → finalize + persist the partial). Only when the loop was
        # genuinely mid-turn — a ``stop`` after a turn already completed (FSM
        # idle) persists nothing, and a turn that already persisted (barge-in /
        # completed) is a no-op via the idempotency guard. ``error`` when the STT
        # turn aborted (Annexe G), else ``voice_stop``.
        if mid_turn and active_turn_id is not None:
            await self._persist_turn(active_turn_id, "error" if aborted else "voice_stop")
            # Issue 0117 — a mid-turn teardown still produces a metrics summary
            # (no-op when the cancelled say-path's finalize already emitted it).
            await self._emit_turn_metrics(active_turn_id)

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
            # Fresh latency accumulator for this turn (keep the first-mic-frame
            # stamp — it measures from when Bob started listening).
            self._latency = TurnLatency(t_first_mic_frame=self._latency.t_first_mic_frame)
            # Register the turn with the critical-path metrics collector (PRD
            # 0018 / issue 0117) — its time origin is speech start, so the
            # ``endpoint`` stage measures the user's speaking time and every
            # later stage a pipeline step. The summary is emitted at finalize.
            turn_metrics.get_default_collector().begin_turn(transition.turn_id)
            # Fresh per-turn capture buffers (PRD 0016 / issue 0109). Reset HERE
            # at the open so the persisted ``mic_in`` is this utterance only; the
            # frame that just tripped speech-start is already in ``_mic_pcm`` from
            # this same call, minus the pre-speech silence we drop here.
            self._begin_capture()
            self._endpointer.reset()
            # Fresh backchannel proactivity budget for this turn (issue 0105) —
            # no carry-over of the previous turn's last-emission watermark.
            self._backchannel.reset()
            # Annexe B ``start_thinker`` — arm the background ThinkerLoop for this
            # turn (PRD 0016 / issue 0102). Synchronous + cheap (resets cadence +
            # clears the live-transcript store); no-op when unwired (bare loop).
            if self.on_thinker_start is not None:
                self.on_thinker_start(transition.turn_id)
            # Arm the SpeculativeDraft for this turn (PRD 0016 / issue 0104) — same
            # synchronous cadence reset as the Thinker; clears the previous turn's
            # held draft so we never adopt a stale pre-written reply. No-op when
            # unwired (bare loop / draft model unavailable — Annexe G).
            if self.on_draft_start is not None:
                self.on_draft_start(transition.turn_id)
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
            # The cancelled say-path's finalize closed the turn's metrics entry
            # (issue 0117); the resumed utterance re-uses the SAME turn id, so
            # re-register it with a fresh time origin.
            if self._fsm.turn_id is not None:
                turn_metrics.get_default_collector().begin_turn(self._fsm.turn_id)
            if self._turn is None:
                await self._open_stt_turn()

    async def _on_endpoint(self) -> None:
        """Handle the ``endpoint`` (silence floor OR semantic): freeze + say-path.

        Issue 0103: ``t_endpoint`` is stamped here whichever source crossed — the
        silence-floor net or a confirmed semantic ``user_turn_complete`` — so a
        complete clause's earlier endpoint is measurable in the latency marks.

        Issue 0118 (PRD 0018, Module 2 — EndpointCommit): the endpoint path is
        CONCURRENT. The Thinker freeze and the Draft freeze FAN OUT (they start
        at the same instant instead of running back-to-back), and the STT
        finalization (the full-buffer whisper pass) runs IN PARALLEL with both.
        Each freeze's cooperative grace is capped inside the hook
        (``THINKER_CANCEL_GRACE_CAP_MS``, default 250 ms — past it, hard
        cancel), so the endpoint → say-path latency is bounded by
        ``max(grace cap, stt finalize)`` rather than the old
        ``grace + grace + finalize`` sum. The say-path is spawned as soon as
        the commit-gate decision is available — nothing later on this path
        waits for any further cleanup.
        """

        self._latency.t_endpoint = self._now()
        transition = self._fsm.on_event(TurnEvent.ENDPOINT, turn_id=self._fsm.turn_id)
        if transition is None:
            return
        await self._emit_turn_state(transition)
        # Critical-path metrics (PRD 0018 / issue 0117): the ``endpoint`` mark
        # opens the response path; the marks below time each step it crosses
        # before the say-path launches (``loops_frozen`` / ``stt_finalized``
        # land in whichever order the parallel branches finish).
        metrics = turn_metrics.get_default_collector()
        metrics.mark(transition.turn_id, "endpoint")

        # Detach the STT turn SYNCHRONOUSLY (before any await) so a frame racing
        # the endpoint path can never feed a turn that is being finalized.
        turn = self._turn
        self._turn = None

        async def _freeze_loops() -> None:
            # Annexe H — the endpoint FREEZES the turn: cooperatively cancel the
            # in-flight Thinker pass AND the in-flight Draft pass (cancel +
            # capped grace + hard-kill, inside each hook) so the Speaker
            # consults the snapshot / the commit gate reads the latest LANDED
            # draft rather than racing a late pass. Both survive the cancel
            # (the loops clear only on the next ``start``). Issue 0118: the two
            # stops fan out concurrently — each hook latches its stop flag the
            # moment it starts executing, so a pass that concludes after the
            # freeze began can no longer mutate what the gate/Speaker read.
            stops = [
                self._call_quietly(hook)
                for hook in (self.on_thinker_stop, self.on_draft_stop)
                if hook is not None
            ]
            if stops:
                await asyncio.gather(*stops)
            # ``loops_frozen`` — both anticipation loops are cancelled (their
            # cooperative grace is the dominant cost PRD 0018 Module 2 attacks).
            metrics.mark(transition.turn_id, "loops_frozen")

        async def _finalize_stt() -> str:
            # Freeze the transcript (0099 finalize → stt_final). Runs in
            # parallel with the freezes (issue 0118) — the whisper full-buffer
            # pass shares nothing with the loop cancellation.
            transcript = ""
            if turn is not None:
                final = await turn.finalize()
                if final is not None:
                    transcript = final.text
            # ``stt_finalized`` — the frozen final transcript is available.
            metrics.mark(transition.turn_id, "stt_finalized")
            return transcript

        _, transcript = await asyncio.gather(_freeze_loops(), _finalize_stt())
        # Record the frozen transcript for this turn's persisted row (issue 0109);
        # the say-path's reply (spoken_text) + completion drive the actual write.
        self._final_transcript = transcript

        # Reset the per-reply barge-in state for the turn we are about to speak:
        # a fresh confirmation window + an empty played-text accumulator.
        self._bargein.reset()
        self._spoken_text = ""

        # The COMMIT GATE (PRD 0016 / issue 0104, Annexe F). On the FINAL frozen
        # transcript, decide whether to adopt the pre-written draft: a committed
        # draft is re-injected into the say-path verbatim (``prepared_reply``) so
        # Bob answers near-instantly; a discarded one (divergence / no draft / tool
        # turn) leaves ``prepared_reply`` None and the say-path regenerates COLD.
        prepared_reply = await self._run_commit_gate(transcript, transition.turn_id)
        # ``gate_decided`` — the speculative-draft verdict is in; the say-path
        # can launch (committed draft or cold regeneration).
        metrics.mark(transition.turn_id, "gate_decided")

        # Launch the say-path as a background task so a long generation does not
        # block the frame pump. Frames keep feeding STT while Bob speaks; once a
        # confirmation window of user speech elapses the barge-in path cuts in.
        # Issue 0124 — supervised: ``_run_say_path`` catches driver errors, but
        # an exception escaping its finalize would otherwise die unobserved on
        # the task (Bob "spoke" and nobody heard / the FSM never recovered).
        self._say_task = create_supervised_task(
            self._run_say_path(transcript, transition.turn_id, prepared_reply=prepared_reply),
            name="voice.say_path",
            session_id=self.session_id,
            turn_id=transition.turn_id,
        )

    async def _run_commit_gate(self, transcript: str, turn_id: str) -> str | None:
        """Run the speculative-draft commit gate at the endpoint (issue 0104).

        Returns the COMMITTED draft text to adopt into the say-path, or ``None``
        when the turn must regenerate COLD (no drafter wired, no draft produced, a
        tool turn, or the gate discarded on divergence). Stamps the Annexe F
        marks + flips ``draft_hit`` and emits the terminal ``draft_status`` event
        (Annexe A.2) — keeping the marks and the wire event in one place. Never
        raises: a gate hiccup must never take the turn down (it degrades to cold).

        ``t_draft_ready`` is stamped when a draft actually existed at the gate (the
        anticipation produced something to judge); ``t_commit_decision`` is stamped
        unconditionally (the gate ran). ``draft_hit`` is the Annexe F bool the
        latency summary + the persisted row carry: ``True`` only when Bob will
        speak a committed draft.
        """

        if self.draft_commit_gate is None:
            return None
        decision: DraftDecision | None = None
        try:
            decision = self.draft_commit_gate(transcript)
        except Exception:
            _logger.exception("voice_loop.commit_gate_failed", session_id=self.session_id)
            return None
        if decision is None:
            return None

        # ``t_draft_ready`` — the anticipation had a pre-written reply to judge
        # (committed via prefix/similarity, or discarded only on divergence — NOT
        # on ``no_draft`` / ``tool_turn`` where nothing was drafted).
        if decision.reason not in ("no_draft", "tool_turn"):
            self._latency.t_draft_ready = self._now()
        # ``t_commit_decision`` — the gate ran (whatever the verdict).
        self._latency.t_commit_decision = self._now()
        self._latency.draft_hit = decision.committed

        # Draft hit-rate counters (PRD 0018 / issue 0117): adopted vs discarded
        # only when a draft actually existed at the gate — a ``no_draft`` /
        # ``tool_turn`` verdict judged nothing, so it must not dilute the
        # adoption rate the aggregates derive.
        if decision.committed:
            turn_metrics.get_default_collector().count(turn_id, "draft_adopted")
        elif decision.reason not in ("no_draft", "tool_turn"):
            turn_metrics.get_default_collector().count(turn_id, "draft_discarded")

        if self.draft_emit_decision is not None:
            with contextlib.suppress(Exception):
                await self.draft_emit_decision(turn_id, decision)

        if decision.committed and decision.text.strip():
            return decision.text
        return None

    async def _run_say_path(
        self, transcript: str, turn_id: str, *, prepared_reply: str | None = None
    ) -> None:
        """Drive the existing Jarvis say-path on the frozen transcript, then idle.

        ``on_first_audio`` flips ``thinking`` -> ``bob_speaking`` and stamps
        ``t_first_audio_chunk`` just before the first outbound chunk. When the
        driver returns (audio done, or none produced) we flip the FSM back to
        ``idle`` and emit the latency summary. A driver exception degrades to a
        clean idle (never crashes the session).

        ``prepared_reply`` (PRD 0016 / issue 0104): a COMMITTED speculative draft.
        When the endpoint's commit gate adopted a pre-written reply, the loop
        threads its text to the driver so it speaks the draft verbatim instead of
        cold-generating — the anticipation that lets ``endpoint_to_first_audio_ms``
        land under the committed target (Annexe F). ``None`` keeps the cold path.

        TurnWatchdog (PRD 0018 / issue 0126): the whole driver call (LLM
        generation + TTS streaming) runs under the voice-path TTFT +
        completion budgets. The orchestrator disarms the TTFT timer on the
        first provider chunk; the first outbound audio chunk disarms it too
        (the committed-draft path never runs the LLM). On expiry the body is
        cancelled, a ``turn_timeout`` voice event fires and a short VERBAL
        fallback is spoken (:meth:`_on_turn_timeout`); the ``finally`` below
        then restores the FSM to a healthy idle exactly like any other exit.
        """

        first_audio_seen = False
        # Issue 0126 — distinct (tighter) budgets for the voice path.
        watchdog = TurnWatchdog(
            ttft_timeout_s=self.settings.VOICE_TURN_TTFT_TIMEOUT_SECONDS,
            completion_timeout_s=self.settings.VOICE_TURN_COMPLETION_TIMEOUT_SECONDS,
        )

        async def _on_first_audio() -> None:
            nonlocal first_audio_seen
            if first_audio_seen:
                return
            first_audio_seen = True
            self._latency.t_first_audio_chunk = self._now()
            # Issue 0126 — audio is flowing, so the provider definitely
            # started answering (covers the committed-draft path, which
            # bypasses the orchestrator's first-chunk note).
            watchdog.note_first_token()
            moved = self._fsm.on_event(TurnEvent.SPEAK_START, turn_id=turn_id)
            if moved is not None:
                await self._emit_turn_state(moved)

        async def _on_spoken_progress(played: str) -> None:
            # The say-path reports the cumulative cleaned text Bob has actually
            # played after each fully-streamed sentence; hold the latest so a
            # barge-in cut commits exactly that (issue 0101). Guard against a
            # late callback from a say-path we no longer own.
            if self._fsm.turn_id == turn_id:
                self._spoken_text = played

        async def _on_audio_chunk(pcm: bytes, sample_rate: int) -> None:
            # Accumulate Bob's outbound PCM for this turn's ``tts_out`` recording
            # (PRD 0016 / issue 0109). Guard against a late callback from a
            # say-path we no longer own (a barge-in re-pointed the FSM).
            if self._fsm.turn_id == turn_id and self.persist_turn is not None:
                self._tts_pcm.extend(pcm)
                self._tts_sample_rate = sample_rate

        # Bind the metrics turn id for the say-path task's duration (PRD 0018 /
        # issue 0117): the downstream sites that stamp ``llm_first_token`` /
        # ``tts_first_chunk`` / ``audio_first_byte`` and count validation
        # retries (orchestrator + ws_router) never see the voice turn id — they
        # resolve it from this ContextVar via ``mark_current`` / ``count_current``.
        # The var is task-local (set inside this task's context), so the frame
        # pump and any concurrent text turn are unaffected.
        metrics_token = turn_metrics.current_metrics_turn_id.set(turn_id)
        try:
            await watchdog.guard(
                self.say_path(
                    transcript,
                    turn_id=turn_id,
                    on_first_audio=_on_first_audio,
                    on_spoken_progress=_on_spoken_progress,
                    on_audio_chunk=_on_audio_chunk,
                    prepared_reply=prepared_reply,
                ),
                name="voice.turn_watchdog",
                session_id=self.session_id,
                turn_id=turn_id,
            )
        except asyncio.CancelledError:
            raise
        except TurnTimeoutError as exc:
            # Issue 0126 — budget expired: turn_timeout event + verbal
            # fallback. The finally below still runs ``_finalize_say`` so the
            # FSM returns to a healthy idle and the turn's latency +
            # turn_metrics summaries are emitted like any other exit.
            await self._on_turn_timeout(
                turn_id, exc, transcript=transcript, on_first_audio=_on_first_audio
            )
        except Exception:
            _logger.exception("voice_loop.say_path_failed", session_id=self.session_id)
        finally:
            turn_metrics.current_metrics_turn_id.reset(metrics_token)
            await self._finalize_say(turn_id)

    async def _on_turn_timeout(
        self,
        turn_id: str,
        exc: TurnTimeoutError,
        *,
        transcript: str,
        on_first_audio: Callable[[], Awaitable[None]],
    ) -> None:
        """Expired turn budget: ``turn_timeout`` event + short verbal fallback.

        PRD 0018 / issue 0126. The watchdog already cancelled the in-flight
        say-path body (LLM stream + TTS); here we make the loss OBSERVABLE
        and AUDIBLE instead of leaving the user in eternal silence:

        1. emit the ``turn_timeout`` voice event (phase + budget) on the
           unified bus — the chat client and the debug feed both see it;
        2. bump the per-turn ``turn_timeout`` counter so the issue-0117
           ``turn_metrics`` summary (still emitted by ``_finalize_say``)
           records the cut;
        3. speak the short fallback via the SAME say-path driver with
           ``prepared_reply`` (no LLM call — the provider just proved itself
           unresponsive), itself bounded by ``TURN_FALLBACK_TIMEOUT_SECONDS``
           since the fallback TTS could hang on the same broken engine.

        Never raises (cancellation excepted): the caller's ``finally`` owns
        the FSM teardown via ``_finalize_say`` whatever happens here.
        """

        _logger.error(
            "voice_loop.turn_timeout",
            session_id=self.session_id,
            turn_id=turn_id,
            phase=exc.phase,
            budget_seconds=exc.budget_seconds,
        )
        turn_metrics.get_default_collector().count(turn_id, "turn_timeout")
        await emit_event(
            {
                "type": "turn_timeout",
                "turn_id": turn_id,
                "path": "voice",
                "phase": exc.phase,
                "budget_seconds": exc.budget_seconds,
                "ts": self._now(),
            },
            category="voice",
            severity="error",
            source="bob.voice_loop.turn_timeout",
            summary=f"turn_timeout {exc.phase} ({exc.budget_seconds:g}s) (turn={turn_id})",
        )
        if not transcript.strip():
            return
        fallback_timeout_s = self.settings.TURN_FALLBACK_TIMEOUT_SECONDS
        try:
            async with asyncio.timeout(fallback_timeout_s if fallback_timeout_s > 0 else None):
                await self.say_path(
                    transcript,
                    turn_id=turn_id,
                    on_first_audio=on_first_audio,
                    prepared_reply=TURN_TIMEOUT_FALLBACK_SPEECH,
                )
        except asyncio.CancelledError:
            raise
        except Exception:
            # Best-effort by design: the turn_timeout event above is the
            # guaranteed client signal; a fallback that cannot be voiced
            # (TTS down) is logged, never re-raised.
            _logger.exception(
                "voice_loop.turn_timeout_fallback_failed",
                session_id=self.session_id,
                turn_id=turn_id,
            )

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
        # Annexe F ``t_tts_end`` — the say-path returned (synthesis done) and this
        # task still owns the turn, so Bob just finished speaking. Stamp it only
        # when Bob actually got the floor (``t_first_audio_chunk`` set) so a
        # no-audio degraded turn carries no spurious tts-end mark; a barge-in cut
        # re-points the FSM off this turn, so we don't stamp a tts-end there (that
        # turn ended at ``t_cut``, not a clean synthesis end).
        if owns_turn and self._latency.t_first_audio_chunk is not None:
            self._latency.t_tts_end = self._now()
        completed_normally = owns_turn and self._fsm.state in (
            TurnState.BOB_SPEAKING,
            TurnState.THINKING,
        )
        if owns_turn and self._fsm.state is TurnState.BOB_SPEAKING:
            transition = self._fsm.on_event(TurnEvent.TTS_END, turn_id=turn_id)
        elif owns_turn and self._fsm.state is TurnState.THINKING:
            transition = self._fsm.on_event(TurnEvent.VOICE_STOP)
        else:
            transition = None
        if transition is not None:
            await self._emit_turn_state(transition)
        await self._emit_turn_latency(turn_id)
        await self._emit_turn_metrics(turn_id)
        # Persist the finished turn (PRD 0016 / issue 0109). ``completed_normally``
        # is true only when THIS say-task drove the teardown (the say-path ran to
        # the end while still owning the floor). A barge-in re-points the FSM to
        # ``user_speaking`` on the same turn id BEFORE cancelling this task, so it
        # is NOT ``completed_normally`` here — the barge-in path persists that
        # turn with ``end_reason="bargein"`` instead. The idempotency guard makes
        # a double-call harmless regardless.
        if completed_normally:
            await self._persist_turn(turn_id, "completed")
        # Re-arm STT for the next utterance unless we have been stopped or a
        # resumed turn already opened one.
        if not self._stopped and self._turn is None:
            await self._open_stt_turn()

    async def _on_bargein(self, confirmation: BargeInConfirmation) -> None:
        """Perform the Annexe B ``bargein_confirmed`` actions (issue 0101).

        The order matters and mirrors the ``thinking`` resume edge:

        1. Capture ``committed_spoken_text`` (what Bob actually played) BEFORE we
           cancel anything — once the say task is gone the accumulator is moot.
        2. Stamp ``t_bargein_detected`` (the confirmation's run start) + ``t_cut``
           (now) for the latency summary (``bargein_cut_ms`` derived).
        3. Move the FSM ``bob_speaking`` → ``user_speaking`` FIRST, so the
           cancelled say task's ``_finalize_say`` ownership guard sees the FSM no
           longer in ``bob_speaking`` and does NOT drive a second transition.
        4. Cancel the in-flight say-path (``cancel_llm_stream`` + ``cancel_tts``):
           cancelling the task aborts both the orchestrator generation and the
           TTS stream cooperatively (the say-path bubbles ``CancelledError``).
        5. Commit the played text to history (``commit_spoken_partial``) and
           restart the Thinker (``start_thinker`` — no-op hook until 0102).
        6. Emit the ``bargein`` voice event (Annexe A.2) + re-arm STT for the
           resumed utterance.
        """

        committed_spoken_text = self._spoken_text
        self._latency.t_bargein_detected = confirmation.detected_ts
        self._latency.t_cut = self._now()

        transition = self._fsm.on_event(TurnEvent.BARGEIN_CONFIRMED, turn_id=self._fsm.turn_id)
        if transition is None:
            # Defensive: the FSM moved off bob_speaking between the frame check
            # and here (e.g. tts_end raced). Nothing to cut.
            return
        turn_id = transition.turn_id
        await self._emit_turn_state(transition)

        # cancel_llm_stream + cancel_tts — tearing the say task down cancels both.
        await self._cancel_say_task()
        # The cancelled say-path's finalize emitted the interrupted turn's
        # metrics summary (issue 0117). The barge-in re-uses the SAME id for
        # the resumed utterance (0101 FSM contract), so re-register it with a
        # fresh time origin — its own endpoint path produces its own summary.
        turn_metrics.get_default_collector().begin_turn(turn_id)

        # commit_spoken_partial — persist exactly what left the speaker.
        if committed_spoken_text.strip() and self.commit_spoken is not None:
            with contextlib.suppress(Exception):
                await self.commit_spoken(turn_id, committed_spoken_text)

        # start_thinker — restart the ThinkerLoop on the resumed turn (issue 0102
        # wires this; ``None`` keeps the 0101 no-op).
        if self.on_thinker_restart is not None:
            with contextlib.suppress(Exception):
                await self.on_thinker_restart(turn_id)
        # Re-arm the SpeculativeDraft on the resumed turn (PRD 0016 / issue 0104):
        # the drafter's ``start`` clears the now-stale pre-written reply (Bob was
        # cut off) so the resumed utterance speculates afresh. No-op when unwired.
        if self.on_draft_start is not None:
            with contextlib.suppress(Exception):
                self.on_draft_start(turn_id)

        await self._emit_bargein(turn_id, confirmation, committed_spoken_text)

        # Persist the interrupted turn (PRD 0016 / issue 0109): ``end_reason``
        # ``bargein``, ``spoken_text`` = exactly what Bob played before the cut.
        # The barge-in re-uses this turn id for the RESUMED utterance (0101 FSM
        # contract we don't change), so this is the turn's terminal persist — the
        # idempotency guard then makes the resumed turn's later finalize a no-op.
        await self._persist_turn(turn_id, "bargein", spoken_text=committed_spoken_text)

        # Re-arm STT for the resumed utterance (the user is now speaking on the
        # SAME turn id). The cancelled say task's ``_finalize_say`` may already
        # have opened one; only open if still detached.
        if not self._stopped and self._turn is None:
            await self._open_stt_turn()

    async def _maybe_backchannel(self, turn_id: str) -> None:
        """Maybe place a brief acknowledgement in this ``vad_pause`` (issue 0105).

        The Annexe B ``maybe_backchannel`` action, fired on the
        ``user_speaking --vad_pause--> user_speaking`` self-loop. It is an ACTION,
        not a floor transition: the FSM stays in ``user_speaking`` (we already
        emitted the no-op ``turn_state``) and Bob does NOT take the floor — a
        backchannel rides *over* the pause, never interrupting the user (we are
        in a pause by construction) and never becoming a ``bob_speaking`` turn.

        Gating (mirrors the inner-thoughts "when-to-speak" proactivity, not
        systematic): the :class:`bob.backchannel.BackchannelDecider` requires the
        Thinker's latest ``backchannel`` trigger to be present (relevance) AND the
        silence-decay refractory window to have elapsed since the last
        acknowledgement. A pause the Thinker did not flag, or one inside the
        refractory window, is silently skipped (logged with the reason). When the
        gate says emit: stamp the pause→ack latency window, dispatch the short
        token's synthesis (Kokoro / fake TTS) as a supervised FIRE-AND-FORGET
        task WITHOUT a floor change (PRD 0018 / issue 0120 — the frame loop never
        awaits it; a failure is logged and never touches the turn), emit the
        ``backchannel`` voice event (Annexe A.2), and arm the refractory window.
        A trigger source / TTS that is unwired (bare loop) makes this a no-op.
        """

        if self.backchannel_trigger is None:
            return
        trigger: str | None = None
        with contextlib.suppress(Exception):
            trigger = self.backchannel_trigger()

        now = self._now()
        decision = self._backchannel.decide(trigger=trigger, now=now)
        if not decision.emit:
            if decision.reason != "no_trigger":
                _logger.debug(
                    "voice_loop.backchannel_suppressed",
                    session_id=self.session_id,
                    turn_id=turn_id,
                    reason=decision.reason,
                )
            return

        # Annexe F ``backchannel_ms`` window — the pause that opened the
        # opportunity → the moment the token is produced. Record only the FIRST
        # backchannel of the turn (the representative pause→ack latency); a later
        # backchannel does not overwrite the marks.
        if self._latency.t_backchannel_pause is None:
            self._latency.t_backchannel_pause = now

        # Synthesise + play the short token WITHOUT touching the FSM floor — as
        # a supervised FIRE-AND-FORGET task (PRD 0018 / issue 0120): the frame
        # loop never awaits the synthesis again, so the next mic frame is
        # processed while Kokoro renders the token. A synthesis failure is
        # logged by the supervisor and never perturbs the live user turn (the
        # user keeps the floor regardless).
        backchannel_tts = self.backchannel_tts
        if backchannel_tts is not None:
            token = decision.token

            async def _synthesise() -> None:
                await backchannel_tts(turn_id, token)

            self._backchannel_task = self._spawn_supervised(
                _synthesise(), what="backchannel_tts", turn_id=turn_id
            )

        # The token has been DISPATCHED (end of the ``backchannel_ms`` window —
        # since issue 0120 the synthesis is fire-and-forget, so the mark measures
        # pause→dispatch, not pause→synthesis-done).
        if self._latency.t_backchannel is None:
            self._latency.t_backchannel = self._now()

        # Arm the refractory window (the proactivity budget is now spent) + emit
        # the Annexe A.2 ``backchannel`` event so the HUD / harness observe it.
        self._backchannel.note_emitted(now)
        await self._emit_backchannel(turn_id, decision.token)

    # -- helpers -------------------------------------------------------------

    async def _dispatch(self, event: TurnEvent, *, turn_id: str | None = None) -> None:
        """Apply an FSM event and emit ``turn_state`` if it moved."""

        transition = self._fsm.on_event(event, turn_id=turn_id)
        if transition is not None:
            await self._emit_turn_state(transition)

    def _begin_capture(self) -> None:
        """Reset the per-turn persistence buffers at a turn open (issue 0109)."""

        self._mic_pcm = bytearray()
        self._tts_pcm = bytearray()
        self._tts_sample_rate = 0
        self._final_transcript = ""

    async def _persist_turn(
        self, turn_id: str, end_reason: str, *, spoken_text: str | None = None
    ) -> None:
        """Hand the finished turn's snapshot to the persistence hook (issue 0109).

        Idempotent per ``turn_id`` (the finalize exits race — ``voice_stop`` can
        fire while an endpoint say-path is still unwinding; a barge-in re-uses
        the id for the resumed utterance). The FIRST exit to reach a turn wins;
        later calls are dropped so we never write duplicate audio blobs. The hook
        owns the DB row + WAV files + Jarvis-history link + the persistence event
        + the retention sweep; the loop only assembles the snapshot. A hook
        failure is swallowed so persistence never takes the voice loop down.

        ``spoken_text`` overrides the accumulated played text (the barge-in cut
        passes the committed prefix explicitly); otherwise the loop's running
        ``_spoken_text`` is used. After persisting, the buffers are released so a
        long armed session does not grow unbounded.
        """

        if self.persist_turn is None:
            return
        if turn_id in self._persisted_turn_ids:
            return
        self._persisted_turn_ids.add(turn_id)

        snapshot = PersistedTurn(
            turn_id=turn_id,
            end_reason=end_reason,
            final_transcript=self._final_transcript,
            spoken_text=spoken_text if spoken_text is not None else self._spoken_text,
            marks=self._latency.marks_payload(),
            derived=self._latency.derived(),
            mic_pcm=bytes(self._mic_pcm),
            mic_sample_rate=self.settings.STT_SAMPLE_RATE or 16_000,
            tts_pcm=bytes(self._tts_pcm),
            tts_sample_rate=self._tts_sample_rate,
        )
        try:
            await self.persist_turn(snapshot)
        except Exception as exc:
            _logger.exception(
                "voice_loop.persist_failed", session_id=self.session_id, turn_id=turn_id
            )
            # Issue 0124 — a lost voice turn must be client-visible, not a
            # log-only swallow: the user (and the attest harness) can see that
            # this turn's transcript/audio never landed. Best-effort: the emit
            # itself must never take the voice loop down.
            with contextlib.suppress(Exception):
                await emit_event(
                    {
                        "type": "voice_persist_failed",
                        "turn_id": turn_id,
                        "end_reason": end_reason,
                        "error": f"{type(exc).__name__}: {exc}",
                    },
                    category="voice",
                    severity="error",
                    source="bob.voice_loop.voice_persist_failed",
                    summary=f"voice_persist_failed (turn={turn_id})",
                )
        finally:
            # Release the (potentially large) PCM buffers for this turn.
            self._mic_pcm = bytearray()
            self._tts_pcm = bytearray()

    @staticmethod
    async def _call_quietly(hook: Callable[[], Awaitable[None]]) -> None:
        """Await ``hook()`` suppressing any exception (cancellation excepted).

        The freeze fan-out helper (issue 0118): each stop hook keeps the same
        never-takes-the-turn-down contract it had when awaited inline, while
        :func:`asyncio.gather` runs them concurrently.
        """

        with contextlib.suppress(Exception):
            await hook()

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

    def _spawn_supervised(
        self, coro: Coroutine[Any, Any, None], *, what: str, turn_id: str
    ) -> asyncio.Task[None]:
        """Spawn a fire-and-forget background task whose failure is logged.

        Minimal local supervision (PRD 0018 / issue 0120): a done-callback reads
        the task's exception so it is never silently dropped by the event loop,
        and a failure never propagates into the frame loop / the live turn.
        Deliberately tiny (create_task + done-callback) so the generic
        TaskSupervisor (issue 0124) can swap in later.
        """

        task = asyncio.create_task(coro)

        def _log_failure(done: asyncio.Task[None]) -> None:
            if done.cancelled():
                return
            exc = done.exception()
            if exc is not None:
                _logger.warning(
                    "voice_loop.background_task_failed",
                    session_id=self.session_id,
                    turn_id=turn_id,
                    what=what,
                    error=repr(exc),
                )

        task.add_done_callback(_log_failure)
        return task

    async def _cancel_backchannel_task(self) -> None:
        """Cancel the in-flight fire-and-forget backchannel synthesis, if any."""

        task = self._backchannel_task
        self._backchannel_task = None
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
        """Emit the Annexe F ``turn_latency`` summary for a finished turn.

        The body (``{turn_id, marks, derived}``) comes from
        :meth:`bob.latency.TurnLatency.as_event_body`, the SAME projection the
        persistence hook serialises into ``voice_turns.latency_json`` — so the
        wire event the attest harness reads and the stored blob can never drift.
        ``ts`` is the emit instant (the rest of the marks are the turn's own
        monotone stamps). ``marks`` carries every mark a slice stamped this turn
        (``t_first_mic_frame`` / ``t_first_partial`` / ``t_endpoint`` /
        ``t_first_audio_chunk`` / ``t_tts_end`` from the loop, the barge-in pair
        on a cut; ``t_draft_ready`` / ``t_commit_decision`` from the Draft commit
        gate — issue 0104 — when a drafter is wired, plus the ``draft_hit``
        derived bool).
        """

        await emit_event(
            {
                "type": "turn_latency",
                **self._latency.as_event_body(turn_id),
                "ts": self._now(),
            },
            category="voice",
            severity="debug",
            source="bob.voice_loop.turn_latency",
            summary=f"turn_latency (turn={turn_id})",
        )

    async def _emit_turn_metrics(self, turn_id: str) -> None:
        """Emit the PRD 0018 ``turn_metrics`` summary for a finished turn.

        Issue 0117 — the per-turn critical-path decomposition (stage durations
        + draft/retry counters) from :class:`bob.turn_metrics.TurnLatencyMetrics`,
        with the rolling P50/P95 aggregates attached so the Debug View shows
        the baseline numbers on the existing debug-event channel (no new UI).
        Closing the turn also EVICTS it from the collector (bounded retention);
        a turn already closed by another exit path is a silent no-op, so every
        finalize exit (completed, barge-in, ``voice_stop``) can call this.
        """

        collector = turn_metrics.get_default_collector()
        summary = collector.finish_turn(turn_id)
        if summary is None:
            return
        await emit_event(
            {
                "type": "turn_metrics",
                **summary,
                "aggregates": collector.aggregates(),
                "ts": self._now(),
            },
            category="voice",
            severity="debug",
            source="bob.voice_loop.turn_metrics",
            summary=f"turn_metrics (turn={turn_id})",
        )

    async def _emit_bargein(
        self, turn_id: str, confirmation: BargeInConfirmation, committed_spoken_text: str
    ) -> None:
        """Emit the Annexe A.2 ``bargein`` voice event for a confirmed cut.

        Payload (Annexe A.2): ``{turn_id, detected_ts, cut_ts,
        committed_spoken_text}``. ``cut_ts`` is the ``t_cut`` mark (when the loop
        actually cancelled Bob). Privacy (Annexe A.2): ``committed_spoken_text``
        carries spoken content, so the full text reaches the client while the
        ring-buffer copy scrubs it to the same leading window as ``stt_*`` (the
        attest harness reads that scrubbed copy, so a short fixture survives
        whole).
        """

        cut_ts = self._latency.t_cut if self._latency.t_cut is not None else self._now()
        payload = {
            "type": "bargein",
            "turn_id": turn_id,
            "detected_ts": confirmation.detected_ts,
            "cut_ts": cut_ts,
            "committed_spoken_text": committed_spoken_text,
        }
        debug_payload = {
            **payload,
            "committed_spoken_text": _scrub_text(
                committed_spoken_text, max_chars=self.settings.STT_DEBUG_TEXT_MAX_CHARS
            ),
        }
        await emit_event(
            payload,
            category="voice",
            severity="info",
            source="bob.voice_loop.bargein",
            summary=f"bargein (turn={turn_id})",
            debug_payload=debug_payload,
        )

    async def _emit_backchannel(self, turn_id: str, token: str) -> None:
        """Emit the Annexe A.2 ``backchannel`` voice event for a placed token.

        Payload (Annexe A.2): ``{turn_id, token, ts}`` (category ``voice``). The
        ``ts`` is the emit instant. The ``token`` is the short acknowledgement Bob
        placed in the pause; it is brief by construction (capped by the decider),
        but for symmetry with the other voice events that carry user-derived text
        the ``/ws/debug`` ring-buffer copy is scrubbed to the leading window (a
        short token survives whole, so the attest harness reads it intact).
        """

        ts = self._now()
        payload = {"type": "backchannel", "turn_id": turn_id, "token": token, "ts": ts}
        debug_payload = {
            **payload,
            "token": _scrub_text(token, max_chars=self.settings.STT_DEBUG_TEXT_MAX_CHARS),
        }
        await emit_event(
            payload,
            category="voice",
            severity="info",
            source="bob.voice_loop.backchannel",
            summary=f"backchannel '{token}' (turn={turn_id})",
            debug_payload=debug_payload,
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
