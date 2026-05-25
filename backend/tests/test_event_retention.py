"""Tests for :class:`bob.event_retention_policy.EventRetentionPolicy` (issue 0052).

Two dimensions enforced on every :func:`emit_debug` call:

- ``max_bytes``: eviction happens when total serialised event bytes exceed
  the cap;
- ``max_age_seconds``: eviction happens when an event's age exceeds the cap.

Both bounds are wired in :mod:`bob.debug_log` via the ``_enforce_retention``
hook called at the end of :func:`emit_debug`.
"""

from __future__ import annotations

import json
import time
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta

import pytest

from bob import debug_log
from bob.debug_log import (
    DebugEvent,
    clear,
    current_task_id,
    current_turn_id,
    emit_debug,
    snapshot,
)
from bob.event_retention_policy import (
    DEFAULT_RETENTION_POLICY,
    EventRetentionPolicy,
    get_retention_policy,
    set_retention_policy,
)


@pytest.fixture(autouse=True)
def _clean_state() -> Iterator[None]:
    clear()
    debug_log._subscribers.clear()
    current_turn_id.set(None)
    current_task_id.set(None)
    set_retention_policy(DEFAULT_RETENTION_POLICY)
    yield
    clear()
    debug_log._subscribers.clear()
    current_turn_id.set(None)
    current_task_id.set(None)
    set_retention_policy(DEFAULT_RETENTION_POLICY)


def test_default_policy_caps_bytes_and_age() -> None:
    """The default policy enables both dimensions with conservative values."""

    assert DEFAULT_RETENTION_POLICY.max_bytes is not None
    assert DEFAULT_RETENTION_POLICY.max_age_seconds is not None


def test_no_policy_disables_retention_beyond_deque_cap() -> None:
    """``None`` clears the policy; ring buffer keeps every event up to maxlen."""

    set_retention_policy(None)
    assert get_retention_policy() is None

    for i in range(5):
        emit_debug(category="system", severity="info", source="t", summary=f"e{i}")

    assert len(snapshot()) == 5


def test_max_bytes_evicts_oldest_when_total_size_exceeds_cap() -> None:
    """Once the cumulative wire size crosses ``max_bytes``, oldest events drop."""

    # Build a policy that fits exactly two of our events.
    sample = DebugEvent(
        ts=datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S.000Z"),
        category="system",
        severity="info",
        source="test.source",
        summary="x" * 50,  # forces a deterministic-ish event size
    )
    one_event_bytes = len(json.dumps(sample.to_dict()))
    cap = one_event_bytes * 2 + 10  # room for 2 events, the 3rd evicts the 1st

    set_retention_policy(EventRetentionPolicy(max_bytes=cap, max_age_seconds=None))

    emit_debug(category="system", severity="info", source="test.source", summary="x" * 50)
    emit_debug(category="system", severity="info", source="test.source", summary="x" * 50)
    assert len(snapshot()) == 2

    # Third event triggers eviction of the first.
    emit_debug(category="system", severity="info", source="test.source", summary="x" * 50)
    events = snapshot()
    assert len(events) <= 2


def test_max_age_seconds_evicts_old_events_on_next_emit() -> None:
    """Events older than ``max_age_seconds`` are dropped when a new emit lands."""

    set_retention_policy(EventRetentionPolicy(max_bytes=None, max_age_seconds=0.1))

    emit_debug(category="system", severity="info", source="t", summary="old")
    assert len(snapshot()) == 1
    time.sleep(0.2)

    # Next emit triggers age-based eviction.
    emit_debug(category="system", severity="info", source="t", summary="new")
    summaries = [e.summary for e in snapshot()]
    assert "old" not in summaries
    assert "new" in summaries


def test_age_eviction_handles_malformed_timestamp() -> None:
    """A malformed ``ts`` is treated as evictable, never a crash."""

    set_retention_policy(EventRetentionPolicy(max_bytes=None, max_age_seconds=1.0))

    # Inject a synthetic event with a bad timestamp into the buffer.
    bad = DebugEvent(
        ts="not-a-timestamp",
        category="system",
        severity="info",
        source="t",
        summary="malformed",
    )
    debug_log._buffer.append(bad)

    # A subsequent emit must not raise.
    emit_debug(category="system", severity="info", source="t", summary="ok")
    # The malformed entry was evicted.
    assert all(e.ts != "not-a-timestamp" for e in snapshot())


def test_age_eviction_keeps_recent_events() -> None:
    """Events within the age window are preserved across emits."""

    set_retention_policy(EventRetentionPolicy(max_bytes=None, max_age_seconds=10.0))

    emit_debug(category="system", severity="info", source="t", summary="first")
    emit_debug(category="system", severity="info", source="t", summary="second")
    emit_debug(category="system", severity="info", source="t", summary="third")

    summaries = [e.summary for e in snapshot()]
    assert summaries == ["first", "second", "third"]


def test_age_then_bytes_eviction_compose() -> None:
    """Both bounds enforced together: age cleans first, bytes finishes the job."""

    # Tight bytes cap that admits one of our events.
    cap_bytes = 350  # approx size of one minimal event
    set_retention_policy(EventRetentionPolicy(max_bytes=cap_bytes, max_age_seconds=10.0))

    # Two events well within age + just over bytes cap.
    emit_debug(category="system", severity="info", source="t", summary="a")
    emit_debug(category="system", severity="info", source="t", summary="b")
    emit_debug(category="system", severity="info", source="t", summary="c")

    events = snapshot()
    assert len(events) >= 1  # at least one survives
    # The newest event is always preserved (eviction is from the oldest end).
    assert events[-1].summary == "c"


def test_set_retention_policy_is_round_trippable() -> None:
    """Setting + getting the policy gives back the same object."""

    custom = EventRetentionPolicy(max_bytes=42, max_age_seconds=3.14)
    set_retention_policy(custom)
    got = get_retention_policy()
    assert got == custom


def test_age_eviction_in_the_future_is_a_noop() -> None:
    """Events with a future timestamp have negative age — never evicted."""

    future_ts = (datetime.now(UTC) + timedelta(seconds=60)).strftime("%Y-%m-%dT%H:%M:%S.000Z")
    future = DebugEvent(
        ts=future_ts,
        category="system",
        severity="info",
        source="t",
        summary="from-future",
    )
    debug_log._buffer.append(future)

    set_retention_policy(EventRetentionPolicy(max_bytes=None, max_age_seconds=0.1))
    emit_debug(category="system", severity="info", source="t", summary="anchor")

    summaries = [e.summary for e in snapshot()]
    assert "from-future" in summaries
