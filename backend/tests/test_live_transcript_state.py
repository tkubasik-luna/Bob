"""Unit tests for :class:`bob.live_transcript_state.LiveTranscriptState`.

PRD 0016 / issue 0102 (Annexe H). The store's load-bearing invariant is
anti-stale ``seq`` ordering: an out-of-order (lower-or-equal ``seq``) snapshot is
IGNORED so the freshest understanding never regresses to a stale late arrival.
"""

from __future__ import annotations

from bob.live_transcript_state import LiveTranscriptState, ThinkerSnapshot


def _snap(seq: int, *, turn_id: str = "t1", text: str = "") -> ThinkerSnapshot:
    return ThinkerSnapshot(turn_id=turn_id, seq=seq, corrected_text=text or f"text-{seq}")


def test_empty_store_returns_none() -> None:
    assert LiveTranscriptState().latest() is None


def test_first_snapshot_accepted() -> None:
    store = LiveTranscriptState()
    snap = _snap(0)
    assert store.update(snap) is True
    assert store.latest() is snap


def test_strictly_increasing_seq_accepted() -> None:
    store = LiveTranscriptState()
    store.update(_snap(0))
    s1 = _snap(1)
    assert store.update(s1) is True
    assert store.latest() is s1
    s2 = _snap(2)
    assert store.update(s2) is True
    assert store.latest() is s2


def test_out_of_order_seq_ignored() -> None:
    """A snapshot with a lower seq than the last accepted is dropped (anti-stale)."""

    store = LiveTranscriptState()
    fresh = _snap(5, text="fresh")
    store.update(fresh)
    stale = _snap(3, text="stale")
    assert store.update(stale) is False
    # The fresh snapshot survives — the stale late arrival never overwrites it.
    latest = store.latest()
    assert latest is fresh
    assert latest.corrected_text == "fresh"


def test_equal_seq_ignored() -> None:
    """``seq`` must be STRICTLY greater — a duplicate seq is a stale repeat."""

    store = LiveTranscriptState()
    first = _snap(2, text="first")
    store.update(first)
    dup = _snap(2, text="second")
    assert store.update(dup) is False
    assert store.latest() is first


def test_clear_resets_store_and_watermark() -> None:
    """Clear drops the snapshot AND the seq watermark so the next turn's seq=0 lands."""

    store = LiveTranscriptState()
    store.update(_snap(7))
    store.clear()
    assert store.latest() is None
    # A fresh turn restarts its seq from 0 — without the watermark reset this
    # would be ignored (0 <= 7). It must be accepted.
    new_turn = _snap(0, turn_id="t2", text="new turn")
    assert store.update(new_turn) is True
    assert store.latest() is new_turn


def test_snapshot_carries_carry_only_signals() -> None:
    """``user_turn_complete`` / ``backchannel`` round-trip (consumed in 0103/0105)."""

    store = LiveTranscriptState()
    snap = ThinkerSnapshot(
        turn_id="t1",
        seq=1,
        corrected_text="quel temps fait-il",
        variables={"intent": "weather", "city": "Paris"},
        next_step_plan="donner la météo de Paris",
        user_turn_complete=True,
        backchannel="mm",
    )
    store.update(snap)
    latest = store.latest()
    assert latest is not None
    assert latest.user_turn_complete is True
    assert latest.backchannel == "mm"
    assert latest.variables == {"intent": "weather", "city": "Paris"}
    assert latest.next_step_plan == "donner la météo de Paris"
