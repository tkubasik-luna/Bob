"""Tests for the ``/ws/task/{task_id}`` filtered subscription (issue 0052).

The route serves a snapshot-then-tail protocol in a single WS session:
the first frame carries every currently-buffered debug event tagged with
the requested ``task_id``, subsequent frames carry live events as they
arrive. The filter is applied over the existing ring buffer — no new
topic, no new persistent store.

Tests cover:
- ordering: ``snapshot`` first, then ``tail`` frames in arrival order;
- filter: only events whose ``task_id`` matches the requested id;
- multi-client: two concurrent overlays on different tasks do not cross-leak;
- empty snapshot for an unknown task id surfaces as ``snapshot`` with []
  followed by a live tail (so the client can render its empty-state).
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient

from bob import debug_log
from bob.debug_log import (
    clear,
    current_task_id,
    current_turn_id,
    emit_debug,
    start_task,
)
from bob.main import app


@pytest.fixture(autouse=True)
def _clean_state() -> Iterator[None]:
    clear()
    debug_log._subscribers.clear()
    current_turn_id.set(None)
    current_task_id.set(None)
    yield
    clear()
    debug_log._subscribers.clear()
    current_turn_id.set(None)
    current_task_id.set(None)


def test_ws_task_replays_snapshot_then_tails_live_events() -> None:
    """First frame is ``snapshot`` carrying buffered events; subsequent are ``tail``."""

    # Seed two events for task-S before any client connects.
    token = start_task("task-S")
    try:
        emit_debug(category="task", severity="info", source="t", summary="seed-1")
        emit_debug(category="task", severity="info", source="t", summary="seed-2")
    finally:
        current_task_id.reset(token)

    with TestClient(app) as client, client.websocket_connect("/ws/task/task-S") as ws:
        # Phase 1: snapshot.
        frame = ws.receive_json()
        assert frame["type"] == "snapshot"
        assert frame["task_id"] == "task-S"
        summaries = [e["summary"] for e in frame["events"]]
        assert summaries == ["seed-1", "seed-2"]
        # Snapshot events carry the task_id field for downstream rendering.
        for event in frame["events"]:
            assert event["task_id"] == "task-S"

        # Phase 2: live tail.
        token2 = start_task("task-S")
        try:
            emit_debug(category="task", severity="info", source="t", summary="live-1")
        finally:
            current_task_id.reset(token2)

        live = ws.receive_json()
        assert live["type"] == "tail"
        assert live["event"]["summary"] == "live-1"
        assert live["event"]["task_id"] == "task-S"


def test_ws_task_filter_excludes_other_task_events() -> None:
    """Events for task-A are NOT delivered to a /ws/task/task-B subscription."""

    with TestClient(app) as client, client.websocket_connect("/ws/task/task-B") as ws:
        # Snapshot frame first (empty — no events tagged with task-B yet).
        frame = ws.receive_json()
        assert frame["type"] == "snapshot"
        assert frame["events"] == []

        # Emit one event for task-A and one for task-B.
        token_a = start_task("task-A")
        try:
            emit_debug(category="task", severity="info", source="t", summary="for-A")
        finally:
            current_task_id.reset(token_a)
        token_b = start_task("task-B")
        try:
            emit_debug(category="task", severity="info", source="t", summary="for-B")
        finally:
            current_task_id.reset(token_b)

        # Only the task-B event arrives — the task-A one is filtered out.
        live = ws.receive_json()
        assert live["type"] == "tail"
        assert live["event"]["summary"] == "for-B"


def test_two_overlays_do_not_cross_leak() -> None:
    """Concurrent overlays on different task ids each see only their own events."""

    with (
        TestClient(app) as client,
        client.websocket_connect("/ws/task/task-X") as ws_x,
        client.websocket_connect("/ws/task/task-Y") as ws_y,
    ):
        # Drain initial empty snapshots.
        snap_x = ws_x.receive_json()
        snap_y = ws_y.receive_json()
        assert snap_x["type"] == "snapshot" and snap_x["events"] == []
        assert snap_y["type"] == "snapshot" and snap_y["events"] == []

        # Interleave emits.
        token_x = start_task("task-X")
        try:
            emit_debug(category="task", severity="info", source="t", summary="X1")
        finally:
            current_task_id.reset(token_x)
        token_y = start_task("task-Y")
        try:
            emit_debug(category="task", severity="info", source="t", summary="Y1")
        finally:
            current_task_id.reset(token_y)
        token_x2 = start_task("task-X")
        try:
            emit_debug(category="task", severity="info", source="t", summary="X2")
        finally:
            current_task_id.reset(token_x2)

        # Each socket sees only its own task's events, in order.
        x_first = ws_x.receive_json()
        assert x_first["event"]["summary"] == "X1"
        x_second = ws_x.receive_json()
        assert x_second["event"]["summary"] == "X2"

        y_first = ws_y.receive_json()
        assert y_first["event"]["summary"] == "Y1"


def test_unknown_task_id_yields_empty_snapshot_and_open_tail() -> None:
    """A subscription to an unknown id sends an empty snapshot frame and stays open."""

    with TestClient(app) as client, client.websocket_connect("/ws/task/unknown-id") as ws:
        frame = ws.receive_json()
        assert frame["type"] == "snapshot"
        assert frame["task_id"] == "unknown-id"
        assert frame["events"] == []

        # If a matching event later arrives, the socket forwards it.
        token = start_task("unknown-id")
        try:
            emit_debug(category="task", severity="info", source="t", summary="late")
        finally:
            current_task_id.reset(token)

        live = ws.receive_json()
        assert live["type"] == "tail"
        assert live["event"]["summary"] == "late"
