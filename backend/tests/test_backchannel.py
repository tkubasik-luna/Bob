"""Unit tests for the backchannel proactivity gate (PRD 0016 / issue 0105).

Exercises :class:`bob.backchannel.BackchannelDecider` in isolation — the pure
gate (relevance = a Thinker trigger present; silence-decay = the refractory
window) without a running loop or a TTS engine, the whole point of keeping the
decision pure. Mirrors the spirit of the inner-thoughts "when-to-speak"
proactivity: a backchannel is NOT systematic — it fires only on a relevant
trigger and not within the refractory window of the last one.
"""

from __future__ import annotations

from bob.backchannel import BackchannelDecider

# --- relevance gate (Thinker trigger present) --------------------------------


def test_no_trigger_no_emit() -> None:
    decider = BackchannelDecider(min_interval_s=1.0)
    decision = decider.decide(trigger=None, now=10.0)
    assert decision.emit is False
    assert decision.reason == "no_trigger"
    assert decision.token == ""


def test_blank_trigger_no_emit() -> None:
    decider = BackchannelDecider(min_interval_s=1.0)
    # A whitespace-only trigger is not a real acknowledgement.
    assert decider.decide(trigger="   ", now=10.0).emit is False


def test_trigger_cold_emits() -> None:
    decider = BackchannelDecider(min_interval_s=1.0)
    decision = decider.decide(trigger="mm", now=10.0)
    assert decision.emit is True
    assert decision.token == "mm"
    assert decision.reason == "emit"


# --- silence-decay refractory window -----------------------------------------


def test_second_trigger_within_refractory_suppressed() -> None:
    decider = BackchannelDecider(min_interval_s=1.5)
    first = decider.decide(trigger="mm", now=10.0)
    assert first.emit is True
    decider.note_emitted(10.0)
    # A second relevant trigger 0.5 s later is inside the 1.5 s window → suppressed.
    second = decider.decide(trigger="ok je vois", now=10.5)
    assert second.emit is False
    assert second.reason == "refractory"


def test_trigger_after_refractory_emits_again() -> None:
    decider = BackchannelDecider(min_interval_s=1.5)
    decider.decide(trigger="mm", now=10.0)
    decider.note_emitted(10.0)
    # 2.0 s later the budget has recovered (>= 1.5 s) → a fresh trigger emits.
    later = decider.decide(trigger="ok", now=12.0)
    assert later.emit is True
    assert later.reason == "emit"


def test_zero_interval_disables_refractory() -> None:
    decider = BackchannelDecider(min_interval_s=0.0)
    decider.decide(trigger="mm", now=10.0)
    decider.note_emitted(10.0)
    # With the refractory disabled, every relevant pause is allowed (back-to-back).
    assert decider.decide(trigger="mm", now=10.001).emit is True


def test_note_emitted_only_spends_budget_when_loop_played() -> None:
    # ``decide`` is pure: it does NOT arm the refractory by itself. Two cold
    # decisions without a ``note_emitted`` in between both emit (the loop only
    # calls note_emitted after it actually played the token).
    decider = BackchannelDecider(min_interval_s=1.5)
    assert decider.decide(trigger="mm", now=10.0).emit is True
    assert decider.decide(trigger="mm", now=10.2).emit is True


# --- token cap (a backchannel is brief by construction) ----------------------


def test_long_trigger_is_capped() -> None:
    decider = BackchannelDecider(min_interval_s=0.0, max_token_chars=5)
    decision = decider.decide(trigger="absolutely fascinating, do go on", now=10.0)
    assert decision.emit is True
    assert decision.token == "absol"  # capped to max_token_chars


# --- per-turn reset ----------------------------------------------------------


def test_reset_clears_refractory_budget() -> None:
    decider = BackchannelDecider(min_interval_s=1.5)
    decider.decide(trigger="mm", now=10.0)
    decider.note_emitted(10.0)
    # Within the window it would be suppressed...
    assert decider.decide(trigger="mm", now=10.3).emit is False
    # ...but a turn boundary reset drops the watermark so the next turn's first
    # relevant trigger is cold-allowed (no carry-over).
    decider.reset()
    assert decider.decide(trigger="mm", now=10.4).emit is True
