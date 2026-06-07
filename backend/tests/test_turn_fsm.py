"""Exhaustive unit tests for the real-time TurnFsm (PRD 0016 / issue 0100).

Covers every basic Annexe B transition, the rejection of illegal (state, event)
pairs, the turn-id lifecycle (mint on start, match on subsequent events, stale
rejection), the universal ``voice_stop`` teardown, and the load-bearing
invariant — never two turn ids in ``bob_speaking`` simultaneously.
"""

from __future__ import annotations

from bob.turn_fsm import Transition, TurnEvent, TurnFsm, TurnState


def _start(fsm: TurnFsm, turn_id: str = "t1") -> Transition | None:
    return fsm.on_event(TurnEvent.VAD_SPEECH_START, turn_id=turn_id)


def _walk_to_bob_speaking(fsm: TurnFsm, turn_id: str = "t1") -> None:
    _start(fsm, turn_id)
    fsm.on_event(TurnEvent.ENDPOINT, turn_id=turn_id)
    fsm.on_event(TurnEvent.SPEAK_START, turn_id=turn_id)


# --- happy-path full cycle ---------------------------------------------------


def test_full_cycle_idle_to_idle() -> None:
    fsm = TurnFsm()
    assert fsm.state is TurnState.IDLE
    assert fsm.turn_id is None

    t1 = fsm.on_event(TurnEvent.VAD_SPEECH_START, turn_id="turn-A")
    assert t1 is not None
    assert (t1.from_state, t1.to_state) == (TurnState.IDLE, TurnState.USER_SPEAKING)
    assert t1.reason == "vad_speech_start"
    assert t1.actions == ("start_turn", "start_thinker")
    assert fsm.turn_id == "turn-A"

    t2 = fsm.on_event(TurnEvent.ENDPOINT, turn_id="turn-A")
    assert t2 is not None
    assert (t2.from_state, t2.to_state) == (TurnState.USER_SPEAKING, TurnState.THINKING)
    assert t2.actions == ("freeze_transcript", "request_commit_or_generate")

    t3 = fsm.on_event(TurnEvent.SPEAK_START, turn_id="turn-A")
    assert t3 is not None
    assert (t3.from_state, t3.to_state) == (TurnState.THINKING, TurnState.BOB_SPEAKING)
    assert t3.actions == ("speak",)
    assert fsm.is_speaking() is True

    t4 = fsm.on_event(TurnEvent.TTS_END, turn_id="turn-A")
    assert t4 is not None
    assert (t4.from_state, t4.to_state) == (TurnState.BOB_SPEAKING, TurnState.IDLE)
    assert t4.actions == ("finalize_turn", "persist_transcript")
    # turn id is named on the final transition, then cleared.
    assert t4.turn_id == "turn-A"
    assert fsm.turn_id is None
    assert fsm.is_speaking() is False


# --- self-loops in user_speaking ---------------------------------------------


def test_stt_partial_is_user_speaking_self_loop() -> None:
    fsm = TurnFsm()
    _start(fsm)
    t = fsm.on_event(TurnEvent.STT_PARTIAL, turn_id="t1")
    assert t is not None
    assert (t.from_state, t.to_state) == (TurnState.USER_SPEAKING, TurnState.USER_SPEAKING)
    assert t.actions == ("feed_thinker", "feed_draft")


def test_vad_pause_is_user_speaking_self_loop() -> None:
    fsm = TurnFsm()
    _start(fsm)
    t = fsm.on_event(TurnEvent.VAD_PAUSE, turn_id="t1")
    assert t is not None
    assert t.to_state is TurnState.USER_SPEAKING
    assert t.actions == ("maybe_backchannel",)


# --- thinking → user_speaking (user resumes, NOT barge-in) -------------------


def test_user_resumes_during_thinking() -> None:
    fsm = TurnFsm()
    _start(fsm)
    fsm.on_event(TurnEvent.ENDPOINT, turn_id="t1")
    assert fsm.state is TurnState.THINKING
    t = fsm.on_event(TurnEvent.VAD_SPEECH_START, turn_id="t1")
    assert t is not None
    assert (t.from_state, t.to_state) == (TurnState.THINKING, TurnState.USER_SPEAKING)
    assert t.actions == ("cancel_generation", "resume_thinker")


# --- voice_stop teardown from every non-idle state ---------------------------


def test_voice_stop_from_user_speaking() -> None:
    fsm = TurnFsm()
    _start(fsm)
    t = fsm.on_event(TurnEvent.VOICE_STOP)
    assert t is not None
    assert t.to_state is TurnState.IDLE
    assert t.actions == ("teardown_turn",)
    assert fsm.turn_id is None


def test_voice_stop_from_thinking() -> None:
    fsm = TurnFsm()
    _start(fsm)
    fsm.on_event(TurnEvent.ENDPOINT, turn_id="t1")
    t = fsm.on_event(TurnEvent.VOICE_STOP)
    assert t is not None
    assert t.to_state is TurnState.IDLE


def test_voice_stop_from_bob_speaking() -> None:
    fsm = TurnFsm()
    _walk_to_bob_speaking(fsm)
    t = fsm.on_event(TurnEvent.VOICE_STOP)
    assert t is not None
    assert t.to_state is TurnState.IDLE


def test_voice_stop_from_idle_is_noop() -> None:
    fsm = TurnFsm()
    assert fsm.on_event(TurnEvent.VOICE_STOP) is None
    assert fsm.state is TurnState.IDLE


# --- illegal (state, event) pairs are rejected, not raised -------------------


def test_illegal_events_rejected_in_each_state() -> None:
    # idle: only vad_speech_start is legal.
    fsm = TurnFsm()
    for event in (
        TurnEvent.STT_PARTIAL,
        TurnEvent.VAD_PAUSE,
        TurnEvent.ENDPOINT,
        TurnEvent.SPEAK_START,
        TurnEvent.TTS_END,
    ):
        assert fsm.on_event(event, turn_id="t1") is None
        assert fsm.state is TurnState.IDLE

    # user_speaking: speak_start / tts_end illegal.
    _start(fsm)
    for event in (TurnEvent.SPEAK_START, TurnEvent.TTS_END):
        assert fsm.on_event(event, turn_id="t1") is None
        assert fsm.state is TurnState.USER_SPEAKING

    # thinking: stt_partial / vad_pause / tts_end illegal.
    fsm.on_event(TurnEvent.ENDPOINT, turn_id="t1")
    for event in (TurnEvent.STT_PARTIAL, TurnEvent.VAD_PAUSE, TurnEvent.TTS_END):
        assert fsm.on_event(event, turn_id="t1") is None
        assert fsm.state is TurnState.THINKING

    # bob_speaking: everything but tts_end (+ voice_stop) illegal.
    fsm.on_event(TurnEvent.SPEAK_START, turn_id="t1")
    for event in (
        TurnEvent.VAD_SPEECH_START,
        TurnEvent.STT_PARTIAL,
        TurnEvent.VAD_PAUSE,
        TurnEvent.ENDPOINT,
        TurnEvent.SPEAK_START,
    ):
        assert fsm.on_event(event, turn_id="t1") is None
        assert fsm.state is TurnState.BOB_SPEAKING


# --- turn-id lifecycle -------------------------------------------------------


def test_start_requires_turn_id() -> None:
    fsm = TurnFsm()
    assert fsm.on_event(TurnEvent.VAD_SPEECH_START) is None  # no id → refuse
    assert fsm.state is TurnState.IDLE


def test_stale_turn_id_rejected() -> None:
    fsm = TurnFsm()
    _start(fsm, "t1")
    # A late partial tagged with a different (stale) turn id is ignored.
    assert fsm.on_event(TurnEvent.STT_PARTIAL, turn_id="other") is None
    assert fsm.state is TurnState.USER_SPEAKING
    # The correct id still advances.
    assert fsm.on_event(TurnEvent.STT_PARTIAL, turn_id="t1") is not None


def test_event_without_turn_id_is_allowed_on_active_turn() -> None:
    # ``turn_id=None`` on a non-start event means "don't check" → applies.
    fsm = TurnFsm()
    _start(fsm, "t1")
    t = fsm.on_event(TurnEvent.ENDPOINT)
    assert t is not None
    assert t.turn_id == "t1"


def test_reset_returns_to_idle() -> None:
    fsm = TurnFsm()
    _walk_to_bob_speaking(fsm)
    fsm.reset()
    assert fsm.state is TurnState.IDLE
    assert fsm.turn_id is None


# --- the load-bearing invariant ----------------------------------------------


def test_never_two_turn_ids_in_bob_speaking() -> None:
    """A single FSM can hold at most one turn id in bob_speaking at a time.

    Drive turn A to bob_speaking, then prove a stray start for turn B does NOT
    open a second speaking turn (it's rejected — bob_speaking has no
    vad_speech_start edge), so the invariant holds structurally.
    """

    fsm = TurnFsm()
    _walk_to_bob_speaking(fsm, "turn-A")
    assert fsm.is_speaking() is True
    assert fsm.turn_id == "turn-A"

    # An attempt to start turn-B while A speaks is rejected.
    assert fsm.on_event(TurnEvent.VAD_SPEECH_START, turn_id="turn-B") is None
    assert fsm.turn_id == "turn-A"
    assert fsm.is_speaking() is True

    # Only after A ends (tts_end → idle) can a new turn open.
    fsm.on_event(TurnEvent.TTS_END, turn_id="turn-A")
    assert fsm.is_speaking() is False
    t = fsm.on_event(TurnEvent.VAD_SPEECH_START, turn_id="turn-B")
    assert t is not None
    assert fsm.turn_id == "turn-B"
