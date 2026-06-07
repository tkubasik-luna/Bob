"""Unit tests for the barge-in confirmation controller (PRD 0016 / issue 0101).

Drives :class:`bob.bargein.BargeInController` on a *simulated timeline* (we feed
``(is_speech, now)`` pairs directly, no audio): the contract is that a short
burst of speech below the confirmation window does NOT cut, while continuous
speech past the window DOES — with ``detected_ts`` pinned to the run start.
"""

from __future__ import annotations

from bob.bargein import DEFAULT_CONFIRM_MS, BargeInConfirmation, BargeInController


def _feed(
    controller: BargeInController, frames: list[tuple[bool, float]]
) -> list[BargeInConfirmation]:
    """Feed a timeline of ``(is_speech, now_seconds)`` and collect confirmations."""

    out: list[BargeInConfirmation] = []
    for is_speech, now in frames:
        result = controller.observe(is_speech=is_speech, now=now)
        if result is not None:
            out.append(result)
    return out


# --- the core contract: window gates the cut --------------------------------


def test_continuous_speech_past_window_confirms() -> None:
    c = BargeInController(confirm_ms=200)
    # Speech every 30 ms from t=0; crosses 200 ms at t=0.210.
    frames = [(True, t / 1000.0) for t in range(0, 300, 30)]
    confirmations = _feed(c, frames)
    assert len(confirmations) == 1
    conf = confirmations[0]
    # detected_ts is the run START (t=0), not the late crossing frame.
    assert conf.detected_ts == 0.0
    # confirmed_ts is the first frame at/after the window (>= 0.200).
    assert conf.confirmed_ts >= 0.200
    assert (conf.confirmed_ts - conf.detected_ts) * 1000.0 >= 200


def test_short_burst_below_window_does_not_cut() -> None:
    c = BargeInController(confirm_ms=200)
    # 150 ms of speech (a backchannel), then silence — never reaches 200 ms.
    frames = [(True, t / 1000.0) for t in range(0, 150, 30)]
    frames += [(False, 0.180), (False, 0.210)]
    assert _feed(c, frames) == []
    assert c.armed is True


def test_silence_resets_run_so_two_short_bursts_do_not_accumulate() -> None:
    c = BargeInController(confirm_ms=200)
    # Burst 1: 120 ms speech, then a silence frame (resets), then burst 2: 120 ms.
    frames = [(True, t / 1000.0) for t in (0, 30, 60, 90, 120)]
    frames += [(False, 0.150)]  # reset
    frames += [(True, t / 1000.0) for t in (180, 210, 240, 270, 300)]
    confirmations = _feed(c, frames)
    # Burst 2 started at 0.180; it must accumulate 200 ms FROM THERE (>= 0.380),
    # not from burst 1 — so within this 300 ms window nothing fires.
    assert confirmations == []


def test_second_burst_eventually_confirms_from_its_own_start() -> None:
    c = BargeInController(confirm_ms=200)
    frames = [(True, 0.0), (True, 0.090), (False, 0.150)]  # burst 1 + reset
    # Burst 2 starts at 0.300, crosses window at 0.510.
    frames += [(True, t / 1000.0) for t in range(300, 560, 30)]
    confirmations = _feed(c, frames)
    assert len(confirmations) == 1
    # detected_ts is burst-2's start, NOT burst-1's.
    assert confirmations[0].detected_ts == 0.300


# --- latch / reset behaviour -------------------------------------------------


def test_fires_exactly_once_then_latches() -> None:
    c = BargeInController(confirm_ms=100)
    frames = [(True, t / 1000.0) for t in range(0, 400, 30)]
    confirmations = _feed(c, frames)
    assert len(confirmations) == 1  # latched after the first crossing
    assert c.armed is False
    # Further frames produce nothing until reset.
    assert c.observe(is_speech=True, now=1.0) is None


def test_reset_rearms_for_next_window() -> None:
    c = BargeInController(confirm_ms=100)
    _feed(c, [(True, t / 1000.0) for t in range(0, 200, 30)])
    assert c.armed is False
    c.reset()
    assert c.armed is True
    assert c.run_started is None
    # A fresh continuous run confirms again (timeline restarted at t=10.0).
    conf = _feed(c, [(True, t) for t in (10.0, 10.05, 10.12)])
    assert len(conf) == 1
    assert conf[0].detected_ts == 10.0


# --- edge cases --------------------------------------------------------------


def test_exact_window_boundary_confirms() -> None:
    # A frame exactly at the window edge should confirm (>= comparison).
    c = BargeInController(confirm_ms=200)
    conf = _feed(c, [(True, 0.0), (True, 0.200)])
    assert len(conf) == 1
    assert conf[0].confirmed_ts == 0.200


def test_non_positive_window_floored_to_one_ms() -> None:
    c = BargeInController(confirm_ms=0)
    assert c.confirm_ms == 1
    # The first speech frame opens the run but does not instantly confirm (a
    # second frame past 1 ms does), so a single noise frame is still filtered.
    assert c.observe(is_speech=True, now=0.0) is None
    conf = c.observe(is_speech=True, now=0.010)
    assert conf is not None


def test_run_started_tracks_inflight_run() -> None:
    c = BargeInController(confirm_ms=500)
    assert c.run_started is None
    c.observe(is_speech=True, now=0.3)
    assert c.run_started == 0.3
    c.observe(is_speech=False, now=0.4)
    assert c.run_started is None


def test_default_confirm_ms_constant() -> None:
    assert DEFAULT_CONFIRM_MS == 200
    assert BargeInController().confirm_ms == 200
