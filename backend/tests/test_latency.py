"""Unit tests for the per-turn latency accumulator (issue 0110, Annexe F).

Exercises :class:`bob.latency.TurnLatency` in isolation — the derived-metric
arithmetic over a synthetic marks dict and the missing-mark rule (a derived is
omitted/None when its inputs are unset) — without a running loop, the whole
point of centralising the arithmetic in one module.
"""

from __future__ import annotations

from bob.latency import TurnLatency


def _full() -> TurnLatency:
    """A fully-stamped (non-barge-in) turn on a synthetic monotone clock."""

    return TurnLatency(
        t_first_mic_frame=100.0,
        t_first_partial=100.2,
        t_endpoint=101.0,
        t_first_audio_chunk=101.5,
        t_tts_end=103.0,
    )


# --- marks payload -----------------------------------------------------------


def test_marks_payload_omits_unstamped_marks() -> None:
    lat = TurnLatency(t_first_mic_frame=100.0, t_endpoint=101.0)
    payload = lat.marks_payload()
    assert payload == {"t_first_mic_frame": 100.0, "t_endpoint": 101.0}
    # The not-yet-produced marks (Draft 0104 / no audio) are absent, not null.
    assert "t_draft_ready" not in payload
    assert "t_commit_decision" not in payload
    assert "t_first_audio_chunk" not in payload


def test_marks_payload_includes_first_partial_and_tts_end() -> None:
    # issue 0110 added these two marks over the 0100 set — they must surface.
    payload = _full().marks_payload()
    assert payload["t_first_partial"] == 100.2
    assert payload["t_tts_end"] == 103.0


# --- derived: endpoint_to_first_audio_ms -------------------------------------


def test_derived_endpoint_to_first_audio_ms() -> None:
    derived = _full().derived()
    # (101.5 - 101.0) * 1000 = 500.0 ms.
    assert derived["endpoint_to_first_audio_ms"] == 500.0


def test_derived_endpoint_to_first_audio_omitted_when_no_audio() -> None:
    # A degraded turn (Bob never spoke) has no t_first_audio_chunk → the metric
    # is OMITTED, never a bogus zero.
    lat = TurnLatency(t_first_mic_frame=100.0, t_endpoint=101.0)
    assert "endpoint_to_first_audio_ms" not in lat.derived()


# --- derived: bargein_cut_ms -------------------------------------------------


def test_derived_bargein_cut_ms() -> None:
    lat = TurnLatency(t_bargein_detected=200.0, t_cut=200.25)
    # (200.25 - 200.0) * 1000 = 250.0 ms.
    assert lat.derived()["bargein_cut_ms"] == 250.0


def test_derived_bargein_cut_omitted_on_non_bargein_turn() -> None:
    # A turn that was never interrupted carries neither barge-in mark → no
    # bargein_cut_ms derived.
    assert "bargein_cut_ms" not in _full().derived()


# --- feature-gated derived (always-present keys) -----------------------------


def test_derived_backchannel_ms_present_but_none_until_0105() -> None:
    derived = _full().derived()
    # The key is ALWAYS present (stable schema) but None until issue 0105 wires
    # the backchannel mark.
    assert "backchannel_ms" in derived
    assert derived["backchannel_ms"] is None


def test_derived_draft_hit_defaults_false_until_0104() -> None:
    derived = _full().derived()
    assert "draft_hit" in derived
    assert derived["draft_hit"] is False


def test_derived_draft_hit_reflects_flag() -> None:
    lat = _full()
    lat.draft_hit = True
    assert lat.derived()["draft_hit"] is True


# --- empty turn (no marks at all) --------------------------------------------


def test_empty_turn_has_empty_marks_and_only_gated_derived() -> None:
    lat = TurnLatency()
    assert lat.marks_payload() == {}
    derived = lat.derived()
    # No mark→mark derived present; only the always-present gated keys.
    assert set(derived) == {"backchannel_ms", "draft_hit"}
    assert derived["backchannel_ms"] is None
    assert derived["draft_hit"] is False


# --- as_event_body (the single wire + stored projection) ---------------------


def test_as_event_body_shape() -> None:
    body = _full().as_event_body("turn-xyz")
    assert body["turn_id"] == "turn-xyz"
    assert body["marks"] == _full().marks_payload()
    assert body["derived"] == _full().derived()
    # The body carries ONLY these three keys (the loop adds ``type`` / ``ts``).
    assert set(body) == {"turn_id", "marks", "derived"}
