"""Real-time turn finite-state machine (PRD 0016 / Annexe B, issues 0100 + 0101).

The full-duplex loop is governed by a compact FSM over four states —
``idle`` / ``user_speaking`` / ``thinking`` / ``bob_speaking`` — driven by the
audio + STT + TTS edges (Annexe B transition table). This module implements the
basic 0100 transitions PLUS the issue-0101 **barge-in** edge (``bob_speaking``
+ confirmed VAD → ``user_speaking``); NO Thinker/Draft (0102/0104). The
``thinking`` → ``bob_speaking`` edge is therefore always the *cold* path
(``draft_miss`` in Annexe B vocabulary): there is no speculative draft to
commit yet.

Why a pure FSM object (no I/O here)?
------------------------------------

The transition logic must be exhaustively unit-testable and impossible to get
subtly wrong (the PRD's load-bearing invariant — *never two ``turn_id`` in
``bob_speaking`` simultaneously* — is an FSM property). So :class:`TurnFsm`
owns ONLY the state + the legal transitions and returns a :class:`Transition`
record describing each move (``from`` / ``to`` / ``reason`` + the symbolic
``actions`` Annexe B lists). The *effects* of those actions (freeze transcript,
drive the say-path, emit ``turn_state``, cancel generation) live in the WS
layer (:mod:`bob.ws_router`), which calls :meth:`on_event` and interprets the
returned actions. That keeps this module free of asyncio / event-bus / TTS
concerns and makes the table the single source of truth.

Annexe B (subset implemented here)
----------------------------------

| from           | event             | to             | actions                                   |
|----------------|-------------------|----------------|-------------------------------------------|
| idle           | vad_speech_start  | user_speaking  | start_turn, start_thinker                 |
| user_speaking  | stt_partial       | user_speaking  | feed_thinker, feed_draft                  |
| user_speaking  | vad_pause         | user_speaking  | maybe_backchannel                         |
| user_speaking  | endpoint          | thinking       | freeze_transcript, request_commit_or_gen  |
| thinking       | speak_start       | bob_speaking   | speak                                     |
| thinking       | vad_speech_start  | user_speaking  | cancel_generation, resume_thinker         |
| bob_speaking   | bargein_confirmed | user_speaking  | (barge-in actions — see below)            |
| bob_speaking   | tts_end           | idle           | finalize_turn, persist_transcript         |
| *              | voice_stop        | idle           | teardown_turn                             |

``speak_start`` is this slice's name for Annexe B's ``draft_miss`` / "no draft"
edge (``thinking`` → ``bob_speaking``): the loop wiring fires it once the
existing Jarvis say-path has produced its first outbound audio.

``bargein_confirmed`` (issue 0101) is the ``bob_speaking`` → ``user_speaking``
interrupt: the loop fires it once :class:`bob.bargein.BargeInController` confirms
a continuous-speech window over the inbound mic frames. Its actions are the
Annexe B set the loop interprets: cancel the in-flight LLM stream + the TTS,
commit the text Bob actually *played* (``committed_spoken_text``) to history,
restart the Thinker. It is legal ONLY from ``bob_speaking`` (a stray
``bargein_confirmed`` in any other state is rejected like every illegal pair).

Events that are not legal in the current state are **rejected** (no transition,
``Transition`` is ``None``) rather than raising — a stray ``stt_partial`` after
``voice_stop`` is noise, not a crash. The caller can log the rejected pair; the
FSM stays in its state. ``voice_stop`` from ``idle`` is a no-op self-loop (also
returns ``None`` — nothing tore down) so a double stop is harmless.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum


class TurnState(StrEnum):
    """The four FSM states (Annexe B). ``value`` is the wire string."""

    IDLE = "idle"
    USER_SPEAKING = "user_speaking"
    THINKING = "thinking"
    BOB_SPEAKING = "bob_speaking"


class TurnEvent(StrEnum):
    """The events that drive the FSM (Annexe B subset, issues 0100 + 0101).

    The draft-commit edge (``draft_committed``) and the explicit backchannel
    emission are deferred to later slices; ``speak_start`` is the cold
    ``thinking`` → ``bob_speaking`` edge used here. ``bargein_confirmed`` is the
    issue-0101 ``bob_speaking`` → ``user_speaking`` interrupt.
    """

    VAD_SPEECH_START = "vad_speech_start"
    STT_PARTIAL = "stt_partial"
    VAD_PAUSE = "vad_pause"
    ENDPOINT = "endpoint"
    SPEAK_START = "speak_start"
    BARGEIN_CONFIRMED = "bargein_confirmed"
    TTS_END = "tts_end"
    VOICE_STOP = "voice_stop"


@dataclass(frozen=True)
class Transition:
    """A single legal FSM move (the result of :meth:`TurnFsm.on_event`).

    Mirrors the Annexe A.2 ``turn_state`` payload minus the event-bus framing:
    ``turn_id`` is the turn the move belongs to, ``from_state`` / ``to_state``
    are the endpoints, ``reason`` is the triggering event's wire name, and
    ``actions`` is the ordered tuple of symbolic Annexe B actions the caller
    must perform (e.g. ``("freeze_transcript", "request_commit_or_generate")``).
    """

    turn_id: str
    from_state: TurnState
    to_state: TurnState
    reason: str
    actions: tuple[str, ...] = ()


# Annexe B basic table, keyed (state, event) -> (next_state, actions).
# ``voice_stop`` is handled out-of-band (legal from ANY non-idle state) so it is
# not enumerated per row; everything else is an explicit, exhaustive entry.
_TABLE: Mapping[tuple[TurnState, TurnEvent], tuple[TurnState, tuple[str, ...]]] = {
    (TurnState.IDLE, TurnEvent.VAD_SPEECH_START): (
        TurnState.USER_SPEAKING,
        ("start_turn", "start_thinker"),
    ),
    (TurnState.USER_SPEAKING, TurnEvent.STT_PARTIAL): (
        TurnState.USER_SPEAKING,
        ("feed_thinker", "feed_draft"),
    ),
    (TurnState.USER_SPEAKING, TurnEvent.VAD_PAUSE): (
        TurnState.USER_SPEAKING,
        ("maybe_backchannel",),
    ),
    (TurnState.USER_SPEAKING, TurnEvent.ENDPOINT): (
        TurnState.THINKING,
        ("freeze_transcript", "request_commit_or_generate"),
    ),
    (TurnState.THINKING, TurnEvent.SPEAK_START): (
        TurnState.BOB_SPEAKING,
        ("speak",),
    ),
    (TurnState.THINKING, TurnEvent.VAD_SPEECH_START): (
        TurnState.USER_SPEAKING,
        ("cancel_generation", "resume_thinker"),
    ),
    # Issue 0101 — the barge-in interrupt. ``user_speaking`` (not ``idle``) so
    # the turn id is retained: the user is resuming the SAME turn Bob was
    # answering. The loop interprets these symbolic actions (cancel the LLM
    # stream + TTS, commit what Bob actually played, restart the Thinker).
    (TurnState.BOB_SPEAKING, TurnEvent.BARGEIN_CONFIRMED): (
        TurnState.USER_SPEAKING,
        ("cancel_llm_stream", "cancel_tts", "commit_spoken_partial", "start_thinker"),
    ),
    (TurnState.BOB_SPEAKING, TurnEvent.TTS_END): (
        TurnState.IDLE,
        ("finalize_turn", "persist_transcript"),
    ),
}


class TurnFsm:
    """Pure turn FSM (Annexe B basic transitions).

    One instance governs one WS session's turn lifecycle. It starts in
    :attr:`TurnState.IDLE` with no active turn id; the ``idle`` →
    ``user_speaking`` transition mints/installs a fresh ``turn_id`` (the caller
    passes it on the :meth:`on_event` call that carries
    :attr:`TurnEvent.VAD_SPEECH_START`). All later events on the same turn must
    carry that id; an event whose ``turn_id`` does not match the active turn is
    rejected (stale — e.g. a late partial from a turn that already ended).

    :meth:`on_event` returns the :class:`Transition` performed, or ``None`` when
    the event is not legal in the current state (or is stale): the FSM never
    raises on an unexpected event, it simply does not move. This makes it safe
    to feed every observed edge without pre-filtering.
    """

    def __init__(self) -> None:
        self._state: TurnState = TurnState.IDLE
        self._turn_id: str | None = None

    @property
    def state(self) -> TurnState:
        """The current FSM state."""

        return self._state

    @property
    def turn_id(self) -> str | None:
        """The id of the active turn, or ``None`` when :attr:`state` is idle."""

        return self._turn_id

    def is_speaking(self) -> bool:
        """True iff Bob currently holds the floor (``bob_speaking``).

        The PRD's load-bearing invariant — *never two ``turn_id`` in
        ``bob_speaking`` simultaneously* — is enforced structurally: a single
        :class:`TurnFsm` has exactly one state, so at most one turn id can be in
        ``bob_speaking`` at any instant. A coordinator that owns one FSM per
        session uses this to assert no two sessions' FSMs are speaking the same
        turn id (see :mod:`bob.ws_router`).
        """

        return self._state is TurnState.BOB_SPEAKING

    def on_event(self, event: TurnEvent, *, turn_id: str | None = None) -> Transition | None:
        """Apply ``event``; return the :class:`Transition` performed, or ``None``.

        ``turn_id`` semantics:

        - On the ``idle`` → ``user_speaking`` start edge, ``turn_id`` is
          REQUIRED and becomes the active turn id.
        - On every other event it is optional; when provided it must equal the
          active turn id or the event is rejected as stale (returns ``None``).
        - ``voice_stop`` is honoured from any non-idle state regardless of id
          (the kill-switch tears down whatever turn is live).

        Illegal (state, event) pairs return ``None`` and leave the state
        unchanged.
        """

        # ``voice_stop`` is the universal teardown — legal from any state that
        # has a live turn. From idle it is a harmless no-op (nothing to tear
        # down) so it returns ``None`` like any other rejected event.
        if event is TurnEvent.VOICE_STOP:
            if self._state is TurnState.IDLE:
                return None
            return self._apply(
                to_state=TurnState.IDLE,
                reason=event.value,
                actions=("teardown_turn",),
                clears_turn=True,
            )

        entry = _TABLE.get((self._state, event))
        if entry is None:
            return None

        # The start edge mints the turn; all others must match the live turn.
        if self._state is TurnState.IDLE and event is TurnEvent.VAD_SPEECH_START:
            if not turn_id:
                # No id to start a turn with — refuse rather than mint a phantom.
                return None
            self._turn_id = turn_id
        elif turn_id is not None and turn_id != self._turn_id:
            # Stale / cross-turn event (e.g. a late partial). Ignore.
            return None

        to_state, actions = entry
        clears_turn = to_state is TurnState.IDLE
        return self._apply(
            to_state=to_state,
            reason=event.value,
            actions=actions,
            clears_turn=clears_turn,
        )

    def reset(self) -> None:
        """Force the FSM back to idle with no active turn (socket teardown)."""

        self._state = TurnState.IDLE
        self._turn_id = None

    # -- internals -----------------------------------------------------------

    def _apply(
        self,
        *,
        to_state: TurnState,
        reason: str,
        actions: tuple[str, ...],
        clears_turn: bool,
    ) -> Transition:
        from_state = self._state
        # Capture the id BEFORE clearing so the emitted ``turn_state`` event
        # still names the turn that just ended.
        turn_id = self._turn_id or ""
        self._state = to_state
        if clears_turn:
            self._turn_id = None
        return Transition(
            turn_id=turn_id,
            from_state=from_state,
            to_state=to_state,
            reason=reason,
            actions=actions,
        )
