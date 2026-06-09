"""JSONL debug-sink gating + batched writes (PRD 0018 / issue 0123).

:func:`bob.debug_log.emit_debug` only touches the disk when the file sink is
installed (``ORCHESTRATION_LOG_ENABLED``); with the Debug View closed AND the
file log disabled, an emit performs zero JSONL writes. With the sink
installed, lines are buffered in memory and written + flushed as one block
per flush window (interval OR pending-line cap) instead of a write+flush per
event — order on disk is preserved.

External behavior only: what lands in the file, and when. The flush clock is
injectable so the interval threshold is driven without real waiting.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path

import pytest

from bob import debug_log
from bob.debug_log import (
    clear,
    current_task_id,
    current_turn_id,
    emit_debug,
    flush_file_sink,
    install_file_sink,
    uninstall_file_sink,
)


@pytest.fixture(autouse=True)
def _clean_state() -> Iterator[None]:
    uninstall_file_sink()
    clear()
    debug_log._subscribers.clear()
    current_turn_id.set(None)
    current_task_id.set(None)
    yield
    uninstall_file_sink()
    clear()
    debug_log._subscribers.clear()
    current_turn_id.set(None)
    current_task_id.set(None)


class _FakeClock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def _emit(summary: str) -> None:
    emit_debug(category="system", severity="info", source="test.sink", summary=summary)


def _lines(path: Path) -> list[dict[str, object]]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]


def _summaries(path: Path) -> list[object]:
    # The install marker is part of the file contract; tests about event
    # persistence look past it.
    return [line["summary"] for line in _lines(path) if line["summary"] != "=== session start ==="]


# --- Acceptance: no JSONL write when the file log is disabled ---------------------


def test_no_sink_installed_means_zero_disk_writes(tmp_path: Path) -> None:
    target = tmp_path / "orchestration.jsonl"

    # Debug View closed (no subscriber — _clean_state guarantees it) AND the
    # file log disabled (no sink installed): emits must not create the file.
    for i in range(50):
        _emit(f"event-{i}")

    assert not target.exists()
    assert debug_log.subscriber_count() == 0


# --- Acceptance: with the log enabled, events are persisted (batched, ordered) ----


def test_writes_are_batched_per_flush_interval_not_per_event(tmp_path: Path) -> None:
    target = tmp_path / "orchestration.jsonl"
    clock = _FakeClock()
    install_file_sink(target, flush_interval_seconds=1.0, flush_max_lines=1000, clock=clock)

    _emit("first")
    _emit("second")
    # Inside the window and below the line cap: nothing written yet beyond
    # the session marker (the whole point — no write+flush per event).
    assert _summaries(target) == []

    clock.advance(1.5)
    _emit("third")  # crossing the interval trips the block write

    assert _summaries(target) == ["first", "second", "third"]


def test_line_cap_trips_a_flush_without_waiting_for_the_interval(tmp_path: Path) -> None:
    target = tmp_path / "orchestration.jsonl"
    clock = _FakeClock()
    install_file_sink(target, flush_interval_seconds=3600.0, flush_max_lines=10, clock=clock)

    for i in range(9):
        _emit(f"event-{i}")
    assert _summaries(target) == []

    _emit("event-9")  # the 10th pending line reaches the cap

    assert _summaries(target) == [f"event-{i}" for i in range(10)]


def test_order_on_disk_matches_emit_order_across_multiple_flushes(tmp_path: Path) -> None:
    target = tmp_path / "orchestration.jsonl"
    clock = _FakeClock()
    install_file_sink(target, flush_interval_seconds=1.0, flush_max_lines=1000, clock=clock)

    expected: list[str] = []
    for window in range(3):
        for i in range(5):
            summary = f"w{window}e{i}"
            expected.append(summary)
            _emit(summary)
        clock.advance(1.5)
        _emit(f"w{window}-closer")
        expected.append(f"w{window}-closer")

    flush_file_sink()
    assert _summaries(target) == expected


def test_uninstall_drains_pending_lines(tmp_path: Path) -> None:
    target = tmp_path / "orchestration.jsonl"
    clock = _FakeClock()
    install_file_sink(target, flush_interval_seconds=3600.0, flush_max_lines=1000, clock=clock)

    _emit("buffered-at-shutdown")
    assert _summaries(target) == []

    uninstall_file_sink()
    assert _summaries(target) == ["buffered-at-shutdown"]


def test_flush_file_sink_forces_pending_lines_to_disk(tmp_path: Path) -> None:
    target = tmp_path / "orchestration.jsonl"
    clock = _FakeClock()
    install_file_sink(target, flush_interval_seconds=3600.0, flush_max_lines=1000, clock=clock)

    _emit("forced")
    assert _summaries(target) == []

    flush_file_sink()
    assert _summaries(target) == ["forced"]


def test_session_start_marker_still_written_immediately_at_install(tmp_path: Path) -> None:
    target = tmp_path / "orchestration.jsonl"
    install_file_sink(target, flush_interval_seconds=3600.0, flush_max_lines=1000)

    [marker] = _lines(target)
    assert marker["summary"] == "=== session start ==="
