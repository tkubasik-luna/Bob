"""Unit tests for the Endpointer (PRD 0016 / issue 0100 floor + issue 0103 semantic).

Issue 0100 (silence floor): arms only after speech, fires once the silence run
reaches the floor, fires exactly once, resets the run on resumed speech, and the
raw-PCM ``feed_frame`` parity path.

Issue 0103 (semantic endpoint + Annexe H confirmation): a ``user_turn_complete``
signal fires the endpoint EARLY only once a stable partial confirms it; an
unconfirmed signal does not fire (the silence floor stays the net); a resumed
speech frame / a withdrawn signal disarms; the floor still fires when no semantic
signal ever arrives.
"""

from __future__ import annotations

import struct

from bob.endpointer import Endpointer


def _frame(amplitude: int, *, samples: int = 480) -> bytes:
    return struct.pack(f"<{samples}h", *([amplitude] * samples))


_LOUD = _frame(8000)
_QUIET = _frame(0)


# --- arming ------------------------------------------------------------------


def test_silence_before_speech_never_ends() -> None:
    ep = Endpointer(silence_floor_frames=3)
    for _ in range(100):
        assert ep.observe(is_speech=False) is False
    assert ep.armed is False


def test_arms_on_first_speech() -> None:
    ep = Endpointer(silence_floor_frames=3)
    assert ep.observe(is_speech=True) is False
    assert ep.armed is True


# --- floor -------------------------------------------------------------------


def test_endpoint_fires_at_floor() -> None:
    ep = Endpointer(silence_floor_frames=3)
    ep.observe(is_speech=True)
    assert ep.observe(is_speech=False) is False  # 1
    assert ep.observe(is_speech=False) is False  # 2
    assert ep.observe(is_speech=False) is True  # 3 → endpoint


def test_endpoint_fires_exactly_once() -> None:
    ep = Endpointer(silence_floor_frames=2)
    ep.observe(is_speech=True)
    ep.observe(is_speech=False)
    assert ep.observe(is_speech=False) is True
    # Latched — further silence does not re-fire.
    for _ in range(10):
        assert ep.observe(is_speech=False) is False


def test_resumed_speech_resets_silence_run() -> None:
    ep = Endpointer(silence_floor_frames=3)
    ep.observe(is_speech=True)
    ep.observe(is_speech=False)  # 1
    ep.observe(is_speech=False)  # 2
    ep.observe(is_speech=True)  # resume → run cleared
    # Needs a fresh full run of 3 to fire.
    assert ep.observe(is_speech=False) is False
    assert ep.observe(is_speech=False) is False
    assert ep.observe(is_speech=False) is True


def test_floor_floored_at_one() -> None:
    ep = Endpointer(silence_floor_frames=0)
    assert ep.silence_floor_frames == 1
    ep.observe(is_speech=True)
    assert ep.observe(is_speech=False) is True


# --- raw-PCM path ------------------------------------------------------------


def test_feed_frame_computes_is_speech() -> None:
    ep = Endpointer(silence_floor_frames=2, speech_rms=0.02)
    assert ep.feed_frame(_LOUD) is False  # arms
    assert ep.feed_frame(_QUIET) is False  # 1
    assert ep.feed_frame(_QUIET) is True  # 2 → endpoint


def test_reset_disarms() -> None:
    ep = Endpointer(silence_floor_frames=2)
    ep.observe(is_speech=True)
    ep.observe(is_speech=False)
    ep.observe(is_speech=False)  # fired
    ep.reset()
    assert ep.armed is False
    # After reset, pre-speech silence is inert again.
    assert ep.observe(is_speech=False) is False


# --- semantic endpoint + confirmation (issue 0103, Annexe B + H) -------------


def test_semantic_complete_confirmed_fires_before_floor() -> None:
    # A high silence floor so ONLY the semantic source can end the turn early.
    ep = Endpointer(silence_floor_frames=100)
    ep.observe(is_speech=True)  # arm the floor
    # The user is still adding words → a stable prefix exists.
    ep.note_stable_prefix(5)
    # The Thinker decides the clause is complete (arms a pending endpoint at the
    # current watermark = 5). Not yet confirmed → no fire on the next frame.
    ep.note_user_turn_complete(True)
    assert ep.semantic_pending is True
    assert ep.observe(is_speech=False) is False
    # The next STABLE partial advances the prefix past the watermark → confirmed.
    ep.note_stable_prefix(9)
    assert ep.observe(is_speech=False) is True  # early endpoint, well before frame 100


def test_semantic_complete_without_confirmation_does_not_fire() -> None:
    # Anti-false-positive: user_turn_complete WITHOUT an advancing stable partial
    # never fires the semantic endpoint — the silence floor remains the net.
    ep = Endpointer(silence_floor_frames=3)
    ep.observe(is_speech=True)
    ep.note_stable_prefix(7)
    ep.note_user_turn_complete(True)  # armed at watermark 7, never confirmed
    # No advancing partial arrives; the prefix only restates the same length.
    ep.note_stable_prefix(7)
    assert ep.observe(is_speech=False) is False  # 1 (no semantic fire)
    assert ep.observe(is_speech=False) is False  # 2
    # The silence floor still fires as the net.
    assert ep.observe(is_speech=False) is True  # 3 → floor endpoint


def test_incomplete_midsentence_pause_holds() -> None:
    # The Thinker never signals completeness (a mid-sentence hesitation). Even a
    # short pause does not end the turn until the silence floor crosses.
    ep = Endpointer(silence_floor_frames=4)
    ep.observe(is_speech=True)
    ep.note_stable_prefix(4)
    # Advancing stable prefixes arrive but user_turn_complete stays false.
    ep.note_user_turn_complete(False)
    ep.note_stable_prefix(8)
    for _ in range(3):  # 3 silent frames < floor of 4 → still held
        assert ep.observe(is_speech=False) is False
    assert ep.semantic_pending is False
    assert ep.observe(is_speech=False) is True  # 4 → floor is the only net


def test_resumed_speech_disarms_unconfirmed_semantic() -> None:
    # A speech frame drops an UNCONFIRMED pending semantic endpoint (the user is
    # still mid-clause → a premature user_turn_complete was a false positive).
    ep = Endpointer(silence_floor_frames=100)
    ep.observe(is_speech=True)
    ep.note_stable_prefix(3)
    ep.note_user_turn_complete(True)  # armed at watermark 3, NOT yet confirmed
    assert ep.semantic_pending is True
    # User resumes before any confirming partial → the pending endpoint is gone.
    assert ep.observe(is_speech=True) is False
    assert ep.semantic_pending is False
    # A following silence does NOT fire (floor is 100, semantic disarmed).
    assert ep.observe(is_speech=False) is False


def test_confirmed_semantic_survives_speech_then_fires_on_silence() -> None:
    # Once CONFIRMED (the stable prefix settled), a transient speech frame does
    # NOT un-confirm the clause — the endpoint fires on the next silence frame.
    # (A genuine resume is handled by the Thinker withdrawing the signal — see
    # test_withdrawn_signal_disarms_then_floor_is_net.)
    ep = Endpointer(silence_floor_frames=100)
    ep.observe(is_speech=True)
    ep.note_stable_prefix(3)
    ep.note_user_turn_complete(True)
    ep.note_stable_prefix(6)  # confirmed
    assert ep.observe(is_speech=True) is False  # transient speech — survives
    assert ep.observe(is_speech=False) is True  # fires early on silence


def test_withdrawn_signal_disarms_then_floor_is_net() -> None:
    # The Thinker arms, a stable partial confirms, but the Thinker then WITHDRAWS
    # the signal (a later snapshot says not-complete) → the semantic endpoint is
    # dropped and only the silence floor can end the turn.
    ep = Endpointer(silence_floor_frames=2)
    ep.observe(is_speech=True)
    ep.note_stable_prefix(3)
    ep.note_user_turn_complete(True)
    ep.note_stable_prefix(6)  # confirmed
    ep.note_user_turn_complete(False)  # withdrawn → disarm
    assert ep.semantic_pending is False
    assert ep.observe(is_speech=False) is False  # 1 (no semantic fire)
    assert ep.observe(is_speech=False) is True  # 2 → floor


def test_late_signal_after_advance_confirms_immediately() -> None:
    # The async snapshot can land AFTER the stable prefix already advanced; the
    # signal then confirms immediately against the latest watermark.
    ep = Endpointer(silence_floor_frames=100)
    ep.observe(is_speech=True)
    ep.note_stable_prefix(4)
    ep.note_stable_prefix(10)  # prefix advanced well past where it will arm
    ep.note_user_turn_complete(True)  # arms at 10, but 10 is not > 10 → pending
    assert ep.observe(is_speech=False) is False
    ep.note_stable_prefix(13)  # now advances past the arm watermark → confirmed
    assert ep.observe(is_speech=False) is True


def test_silence_floor_only_unchanged_when_no_semantic() -> None:
    # No semantic signal ever routed → behaviour is exactly the 0100 floor.
    ep = Endpointer(silence_floor_frames=3)
    ep.observe(is_speech=True)
    ep.note_stable_prefix(5)  # partials advance, but no user_turn_complete
    ep.note_stable_prefix(9)
    assert ep.observe(is_speech=False) is False  # 1
    assert ep.observe(is_speech=False) is False  # 2
    assert ep.observe(is_speech=False) is True  # 3 → floor


def test_semantic_signal_pre_speech_inert() -> None:
    # A confirmed semantic endpoint cannot end a turn that never had speech — the
    # detector shares the floor's "no speech → no end" invariant. (The loop only
    # routes these signals while USER_SPEAKING anyway, but the detector is robust
    # to it on its own.)
    ep = Endpointer(silence_floor_frames=3)
    ep.note_stable_prefix(2)
    ep.note_user_turn_complete(True)
    ep.note_stable_prefix(5)  # confirmation gate satisfied, but no speech yet
    assert ep.armed is False
    for _ in range(10):
        assert ep.observe(is_speech=False) is False  # never fires without speech


def test_semantic_endpoint_never_fires_with_zero_stable_transcript() -> None:
    """The formal zero-transcript guard: no semantic fire before any settled text.

    Even if a ``user_turn_complete`` raced in and a confirmation somehow latched
    while the stable watermark is still 0 (no STT partial settled this turn),
    the semantic source must not end the turn — only the silence floor may.
    """

    ep = Endpointer(silence_floor_frames=50)
    ep.observe(is_speech=True)  # arm the floor (speech seen)
    ep.note_user_turn_complete(True)
    # Force the latched-confirmed state with a zero watermark (defensive: the
    # public confirmation path requires a stable ADVANCE, which implies > 0 —
    # the guard keeps the invariant local instead of relying on that ordering).
    ep._semantic_confirmed = True
    assert ep.observe(is_speech=False) is False  # semantic blocked
    ep.note_stable_prefix(3)  # transcript settled → semantic may now fire
    assert ep.observe(is_speech=False) is True
