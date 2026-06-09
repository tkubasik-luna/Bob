"""Unit tests for the per-turn metrics collector (PRD 0018 / issue 0117).

Exercises :class:`bob.turn_metrics.TurnLatencyMetrics` strictly through its
public boundary — begin/mark/count in, summary + aggregates out — under a
fake monotone clock (deterministic durations), plus the module-level
ContextVar helpers the say-path instrumentation sites use. No internal call
sequences, no private attributes.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any, cast

import pytest

from bob import turn_metrics
from bob.turn_metrics import COUNTER_NAMES, TurnLatencyMetrics


class _FakeClock:
    """A manually-advanced monotone clock (seconds)."""

    def __init__(self, start: float = 100.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance_ms(self, ms: float) -> None:
        self.now += ms / 1000.0


def _collector(clock: _FakeClock, *, max_turns: int = 64, window: int = 256) -> TurnLatencyMetrics:
    return TurnLatencyMetrics(clock=clock, max_turns=max_turns, window=window)


def _stages(summary: dict[str, object]) -> dict[str, float]:
    return cast(dict[str, float], summary["stages_ms"])


def _counters(summary: dict[str, object]) -> dict[str, int]:
    return cast(dict[str, int], summary["counters"])


def _run_turn(
    metrics: TurnLatencyMetrics,
    clock: _FakeClock,
    turn_id: str,
    stage_durations_ms: dict[str, float],
) -> dict[str, object]:
    """Begin → (advance, mark) per stage → finish; return the summary."""

    metrics.begin_turn(turn_id)
    for stage, duration in stage_durations_ms.items():
        clock.advance_ms(duration)
        metrics.mark(turn_id, stage)
    summary = metrics.finish_turn(turn_id)
    assert summary is not None
    return summary


# --- per-turn summary ----------------------------------------------------------


def test_summary_decomposes_stage_durations() -> None:
    clock = _FakeClock()
    metrics = _collector(clock)
    summary = _run_turn(
        metrics,
        clock,
        "t1",
        {
            "endpoint": 2000.0,  # user speaking time (begin → endpoint)
            "loops_frozen": 150.0,
            "stt_finalized": 50.0,
            "gate_decided": 10.0,
            "llm_first_token": 700.0,
            "tts_first_chunk": 300.0,
            "audio_first_byte": 5.0,
        },
    )

    assert summary["turn_id"] == "t1"
    stages = _stages(summary)
    # Each stage = delta from the PREVIOUS stamped mark (the first from begin).
    assert stages["endpoint"] == 2000.0
    assert stages["loops_frozen"] == 150.0
    assert stages["stt_finalized"] == 50.0
    assert stages["gate_decided"] == 10.0
    assert stages["llm_first_token"] == 700.0
    assert stages["tts_first_chunk"] == 300.0
    assert stages["audio_first_byte"] == 5.0
    # total = first mark (endpoint) → last mark (audio_first_byte).
    assert summary["total_ms"] == 150.0 + 50.0 + 10.0 + 700.0 + 300.0 + 5.0


def test_summary_counters_always_carry_stable_schema() -> None:
    clock = _FakeClock()
    metrics = _collector(clock)
    summary = _run_turn(metrics, clock, "t1", {"endpoint": 100.0})
    # Never-bumped counters still appear at 0 (stable schema).
    assert _counters(summary) == dict.fromkeys(COUNTER_NAMES, 0)


def test_counters_accumulate_including_custom_increment() -> None:
    clock = _FakeClock()
    metrics = _collector(clock)
    metrics.begin_turn("t1")
    metrics.count("t1", "validation_retry")
    metrics.count("t1", "validation_retry")
    metrics.count("t1", "draft_adopted")
    metrics.count("t1", "extra_counter", n=3)
    summary = metrics.finish_turn("t1")
    assert summary is not None
    counters = _counters(summary)
    assert counters["validation_retry"] == 2
    assert counters["draft_adopted"] == 1
    assert counters["draft_discarded"] == 0
    assert counters["extra_counter"] == 3


def test_mark_is_first_write_wins() -> None:
    clock = _FakeClock()
    metrics = _collector(clock)
    metrics.begin_turn("t1")
    clock.advance_ms(100.0)
    metrics.mark("t1", "llm_first_token")
    clock.advance_ms(5000.0)
    # A validation retry streaming a second attempt must not move the mark.
    metrics.mark("t1", "llm_first_token")
    summary = metrics.finish_turn("t1")
    assert summary is not None
    assert _stages(summary)["llm_first_token"] == 100.0


def test_rebegin_resets_the_turn_origin_and_marks() -> None:
    # The barge-in path re-uses the interrupted turn's id for the resumed
    # utterance — re-beginning must produce a fresh, self-consistent summary.
    clock = _FakeClock()
    metrics = _collector(clock)
    metrics.begin_turn("t1")
    clock.advance_ms(500.0)
    metrics.mark("t1", "endpoint")
    metrics.count("t1", "validation_retry")

    metrics.begin_turn("t1")  # resumed utterance, fresh origin
    clock.advance_ms(250.0)
    metrics.mark("t1", "endpoint")
    summary = metrics.finish_turn("t1")
    assert summary is not None
    assert _stages(summary) == {"endpoint": 250.0}
    assert _counters(summary)["validation_retry"] == 0


def test_summary_with_no_marks_is_empty_but_well_formed() -> None:
    clock = _FakeClock()
    metrics = _collector(clock)
    metrics.begin_turn("t1")
    summary = metrics.finish_turn("t1")
    assert summary is not None
    assert _stages(summary) == {}
    assert summary["total_ms"] == 0.0


# --- unknown / finished turn ids are safe no-ops --------------------------------


def test_mark_count_finish_on_unknown_turn_are_noops() -> None:
    metrics = _collector(_FakeClock())
    metrics.mark("ghost", "endpoint")  # must not raise
    metrics.count("ghost", "validation_retry")  # must not raise
    assert metrics.finish_turn("ghost") is None


def test_finish_twice_returns_none_second_time() -> None:
    clock = _FakeClock()
    metrics = _collector(clock)
    metrics.begin_turn("t1")
    assert metrics.finish_turn("t1") is not None
    assert metrics.finish_turn("t1") is None
    # And marks after the finish are no-ops too.
    metrics.mark("t1", "endpoint")
    metrics.count("t1", "validation_retry")


# --- bounded retention -----------------------------------------------------------


def test_oldest_unfinished_turn_evicted_past_max_turns() -> None:
    clock = _FakeClock()
    metrics = _collector(clock, max_turns=3)
    for i in range(5):
        metrics.begin_turn(f"t{i}")
    # The two oldest were evicted: finishing them yields nothing.
    assert metrics.finish_turn("t0") is None
    assert metrics.finish_turn("t1") is None
    # The newest three are intact.
    for i in (2, 3, 4):
        assert metrics.finish_turn(f"t{i}") is not None


def test_percentile_window_is_bounded() -> None:
    clock = _FakeClock()
    metrics = _collector(clock, window=4)
    # 10 turns at 1000 ms, then 4 turns at 100 ms — the window only retains
    # the last 4 samples, so the old 1000 ms population is fully evicted.
    for i in range(10):
        _run_turn(metrics, clock, f"slow{i}", {"endpoint": 1000.0})
    for i in range(4):
        _run_turn(metrics, clock, f"fast{i}", {"endpoint": 100.0})
    aggregates = metrics.aggregates()
    stages = cast(dict[str, dict[str, float]], aggregates["stages"])
    assert stages["endpoint"]["count"] == 4
    assert stages["endpoint"]["p50_ms"] == 100.0
    assert stages["endpoint"]["p95_ms"] == 100.0


# --- aggregates ------------------------------------------------------------------


def test_p50_p95_nearest_rank_over_known_population() -> None:
    clock = _FakeClock()
    metrics = _collector(clock)
    for i, duration in enumerate([100.0, 200.0, 300.0, 400.0]):
        _run_turn(metrics, clock, f"t{i}", {"endpoint": duration})
    aggregates = metrics.aggregates()
    stages = cast(dict[str, dict[str, float]], aggregates["stages"])
    row = stages["endpoint"]
    assert row["count"] == 4
    # Nearest-rank: P50 of 4 samples = 2nd, P95 = 4th.
    assert row["p50_ms"] == 200.0
    assert row["p95_ms"] == 400.0
    assert aggregates["turns_measured"] == 4


def test_aggregates_track_each_stage_independently() -> None:
    clock = _FakeClock()
    metrics = _collector(clock)
    _run_turn(metrics, clock, "t1", {"endpoint": 500.0, "llm_first_token": 800.0})
    _run_turn(metrics, clock, "t2", {"endpoint": 700.0})
    stages = cast(dict[str, dict[str, float]], metrics.aggregates()["stages"])
    assert stages["endpoint"]["count"] == 2
    assert stages["llm_first_token"]["count"] == 1
    assert stages["llm_first_token"]["p50_ms"] == 800.0
    # A stage never marked has no row at all (no bogus zeros).
    assert "tts_first_chunk" not in stages


def test_non_canonical_stage_in_summary_but_not_aggregated() -> None:
    clock = _FakeClock()
    metrics = _collector(clock)
    summary = _run_turn(metrics, clock, "t1", {"endpoint": 100.0, "custom_probe": 50.0})
    assert _stages(summary)["custom_probe"] == 50.0
    stages = cast(dict[str, dict[str, float]], metrics.aggregates()["stages"])
    assert "custom_probe" not in stages


def test_draft_adoption_rate_derives_from_counter_totals() -> None:
    clock = _FakeClock()
    metrics = _collector(clock)
    # No judged draft yet → no rate (never a bogus 0/0).
    assert metrics.aggregates()["draft_adoption_rate"] is None

    metrics.begin_turn("t1")
    metrics.count("t1", "draft_adopted")
    metrics.finish_turn("t1")
    metrics.begin_turn("t2")
    metrics.count("t2", "draft_discarded")
    metrics.finish_turn("t2")
    metrics.begin_turn("t3")
    metrics.count("t3", "draft_adopted")
    metrics.finish_turn("t3")

    aggregates = metrics.aggregates()
    totals = cast(dict[str, int], aggregates["counters_total"])
    assert totals["draft_adopted"] == 2
    assert totals["draft_discarded"] == 1
    assert aggregates["draft_adoption_rate"] == round(2 / 3, 3)


# --- ContextVar helpers (the say-path instrumentation entry points) --------------


@pytest.fixture
def _installed_collector() -> Iterator[tuple[TurnLatencyMetrics, _FakeClock]]:
    clock = _FakeClock()
    collector = _collector(clock)
    turn_metrics.set_default_collector(collector)
    try:
        yield collector, clock
    finally:
        turn_metrics.set_default_collector(None)


def test_mark_current_noop_outside_a_metered_turn(
    _installed_collector: tuple[TurnLatencyMetrics, _FakeClock],
) -> None:
    # No ContextVar bound (the text path) — both helpers must silently no-op.
    turn_metrics.mark_current("llm_first_token")
    turn_metrics.count_current("validation_retry")
    collector, _clock = _installed_collector
    assert cast(dict[str, Any], collector.aggregates()["stages"]) == {}


def test_mark_and_count_current_resolve_the_bound_turn(
    _installed_collector: tuple[TurnLatencyMetrics, _FakeClock],
) -> None:
    collector, clock = _installed_collector
    collector.begin_turn("t1")
    token = turn_metrics.current_metrics_turn_id.set("t1")
    try:
        clock.advance_ms(300.0)
        turn_metrics.mark_current("llm_first_token")
        turn_metrics.count_current("validation_retry", n=2)
    finally:
        turn_metrics.current_metrics_turn_id.reset(token)
    summary = collector.finish_turn("t1")
    assert summary is not None
    assert _stages(summary)["llm_first_token"] == 300.0
    assert _counters(summary)["validation_retry"] == 2
