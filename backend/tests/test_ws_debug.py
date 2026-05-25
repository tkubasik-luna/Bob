"""Tests for the ``/ws/debug`` WebSocket route (slice 0038)."""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient

from bob import debug_log
from bob.debug_log import clear, emit_debug
from bob.main import app


@pytest.fixture(autouse=True)
def _clean_state() -> Iterator[None]:
    clear()
    debug_log._subscribers.clear()
    yield
    clear()
    debug_log._subscribers.clear()


def test_ws_debug_replays_buffered_events_on_connect() -> None:
    """A fresh debug client receives the ring-buffer snapshot first."""

    emit_debug(category="input", severity="info", source="t.seed", summary="seeded")

    with TestClient(app) as client, client.websocket_connect("/ws/debug") as ws:
        frame = ws.receive_json()
        assert frame["category"] == "input"
        assert frame["severity"] == "info"
        assert frame["summary"] == "seeded"
        # Replayed snapshot pass must mark the flag.
        assert frame["replayed"] is True
        # Envelope key set is the PRD `Schema sur le fil` shape.
        # Issue 0052 adds the ``task_id`` field alongside ``parent_task_id``.
        assert set(frame.keys()) == {
            "ts",
            "category",
            "severity",
            "source",
            "summary",
            "payload",
            "turn_id",
            "correlation_id",
            "parent_task_id",
            "task_id",
            "replayed",
        }


def test_ws_debug_streams_live_events_after_snapshot() -> None:
    """After draining the snapshot, new emits arrive as live frames."""

    with TestClient(app) as client, client.websocket_connect("/ws/debug") as ws:
        # No snapshot to drain — buffer is empty thanks to the autouse fixture.
        emit_debug(
            category="input",
            severity="info",
            source="orchestrator.process_user_message",
            summary='User envoie: "hi"',
            payload={"content": "hi"},
        )

        frame = ws.receive_json()
        assert frame["summary"] == 'User envoie: "hi"'
        assert frame["payload"] == {"content": "hi"}
        # Live events do NOT carry the replayed flag.
        assert frame["replayed"] is False


def test_ws_debug_disconnect_does_not_block_emits() -> None:
    """Closing the debug WS must not leave dangling subscribers."""

    with TestClient(app) as client:
        with client.websocket_connect("/ws/debug") as ws:
            emit_debug(category="input", severity="info", source="t", summary="one")
            frame = ws.receive_json()
            assert frame["summary"] == "one"

        # WS context exited → subscriber cleanup should run shortly.
        # New emits continue to accumulate in the ring buffer regardless.
        emit_debug(category="input", severity="info", source="t", summary="post-close")
        assert debug_log.snapshot()[-1].summary == "post-close"


def test_ws_debug_emits_continue_with_no_clients() -> None:
    """The ring buffer accepts events even when no debug client is connected."""

    emit_debug(category="input", severity="info", source="t", summary="alone")
    assert debug_log.subscriber_count() == 0
    assert len(debug_log.snapshot()) == 1
