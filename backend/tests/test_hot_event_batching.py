"""Hot event batching — coalesced delta streams (PRD 0018 / issue 0123).

:func:`bob.event_bus_v2.emit_event` coalesces the token-by-token streams
(``speech_delta`` keyed by ``msg_id``, ``reasoning_delta`` keyed by
``agent_ref``) into ONE merged event of the same wire shape per
``WS_HOT_EVENT_BATCH_WINDOW_MS`` window. Low-frequency events are never
delayed — they flush the pending window first so order is preserved.

Tests target EXTERNAL behavior only: which frames a registered WS emitter
receives, in which order, and how many per window. Every async test runs
under the :mod:`tests._harness.virtual_clock` fake clock so the batching
window costs zero real wall time.
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from types import SimpleNamespace
from typing import Any

import pytest

from bob import debug_log, event_bus_v2
from bob.config import Settings, get_settings
from bob.debug_log import clear, current_task_id, current_turn_id, start_task

from ._harness.virtual_clock import VirtualTimePolicy


@pytest.fixture
def event_loop_policy() -> asyncio.AbstractEventLoopPolicy:
    """Run every async test of this module on the virtual clock."""

    return VirtualTimePolicy()


@pytest.fixture(autouse=True)
def _clean_state() -> Iterator[None]:
    clear()
    debug_log._subscribers.clear()
    current_turn_id.set(None)
    current_task_id.set(None)
    event_bus_v2.set_ws_emitter(None)
    yield
    clear()
    debug_log._subscribers.clear()
    current_turn_id.set(None)
    current_task_id.set(None)
    event_bus_v2.set_ws_emitter(None)


def _recorder(sink: list[dict[str, Any]]) -> event_bus_v2.WsEmitter:
    async def _emit(event: dict[str, Any]) -> None:
        sink.append(event)

    return _emit


def _window_seconds() -> float:
    return get_settings().WS_HOT_EVENT_BATCH_WINDOW_MS / 1000.0


async def _emit_speech(msg_id: str, delta: str) -> None:
    await event_bus_v2.emit_event({"type": "speech_delta", "msg_id": msg_id, "delta": delta})


async def _emit_reasoning(agent_ref: str, delta: str) -> None:
    await event_bus_v2.emit_event(
        {"type": "reasoning_delta", "agent_ref": agent_ref, "delta": delta}
    )


# --- Acceptance: the window is a setting -----------------------------------------


def test_batch_window_is_a_setting_with_default_in_the_50_to_100ms_band() -> None:
    default = Settings.model_fields["WS_HOT_EVENT_BATCH_WINDOW_MS"].default
    assert 50 <= default <= 100


# --- Acceptance: a burst produces a bounded number of WS emissions ---------------


async def test_token_burst_coalesces_into_one_ws_frame_per_window() -> None:
    received: list[dict[str, Any]] = []
    event_bus_v2.add_ws_emitter(_recorder(received))

    tokens = [f"tok{i} " for i in range(200)]
    for token in tokens:
        await _emit_speech("m1", token)

    # Nothing on the wire yet — the burst is inside the window.
    assert received == []

    await asyncio.sleep(_window_seconds() * 1.5)  # virtual time — instant for real

    # The whole burst became ONE frame, same wire type, deltas concatenated.
    assert len(received) == 1
    assert received[0]["type"] == "speech_delta"
    assert received[0]["msg_id"] == "m1"
    assert received[0]["delta"] == "".join(tokens)


async def test_continuous_stream_emits_at_most_one_frame_per_window_per_stream() -> None:
    received: list[dict[str, Any]] = []
    event_bus_v2.add_ws_emitter(_recorder(received))

    window = _window_seconds()
    for burst in range(3):
        for i in range(50):
            await _emit_speech("m1", f"b{burst}t{i} ")
        await asyncio.sleep(window * 1.5)

    # 150 deltas over 3 windows → exactly 3 WS frames.
    assert [e["type"] for e in received] == ["speech_delta"] * 3
    assert "".join(e["delta"] for e in received) == "".join(
        f"b{burst}t{i} " for burst in range(3) for i in range(50)
    )


async def test_distinct_streams_never_merge_into_one_frame() -> None:
    received: list[dict[str, Any]] = []
    event_bus_v2.add_ws_emitter(_recorder(received))

    await _emit_speech("m1", "hello ")
    await _emit_speech("m2", "retry ")
    await _emit_reasoning("jarvis", "thinking… ")
    await _emit_reasoning("task-A", "fetching… ")

    await asyncio.sleep(_window_seconds() * 1.5)

    by_key = {(e["type"], e.get("msg_id") or e.get("agent_ref")): e["delta"] for e in received}
    assert by_key == {
        ("speech_delta", "m1"): "hello ",
        ("speech_delta", "m2"): "retry ",
        ("reasoning_delta", "jarvis"): "thinking… ",
        ("reasoning_delta", "task-A"): "fetching… ",
    }


# --- Acceptance: low-frequency events suffer no batching delay -------------------


async def test_cold_events_are_never_delayed() -> None:
    loop = asyncio.get_running_loop()
    received: list[dict[str, Any]] = []
    event_bus_v2.add_ws_emitter(_recorder(received))

    started = loop.time()
    for event_type in ("assistant_msg", "task_updated", "audio_start", "audio_end"):
        await event_bus_v2.emit_event({"type": event_type, "msg_id": "m1", "task_id": "t1"})
    elapsed = loop.time() - started

    assert elapsed < 0.01  # no window wait on the cold path
    assert [e["type"] for e in received] == [
        "assistant_msg",
        "task_updated",
        "audio_start",
        "audio_end",
    ]


async def test_cold_event_flushes_pending_deltas_first_so_order_is_preserved() -> None:
    received: list[dict[str, Any]] = []
    event_bus_v2.add_ws_emitter(_recorder(received))

    await _emit_speech("m1", "Bonjour ")
    await _emit_speech("m1", "Tom")
    # The closing frames arrive immediately after the stream — well inside
    # the window — and must NOT overtake the buffered deltas.
    await event_bus_v2.emit_event({"type": "ui_payload", "msg_id": "m1", "ui": {}})
    await event_bus_v2.emit_event({"type": "assistant_msg", "msg_id": "m1", "text": "Bonjour Tom"})

    assert [e["type"] for e in received] == ["speech_delta", "ui_payload", "assistant_msg"]
    assert received[0]["delta"] == "Bonjour Tom"


# --- Wire shape stays backward-compatible ----------------------------------------


async def test_merged_frame_keeps_the_exact_wire_shape() -> None:
    received: list[dict[str, Any]] = []
    event_bus_v2.add_ws_emitter(_recorder(received))

    await _emit_reasoning("task-A", "a")
    await _emit_reasoning("task-A", "b")
    await event_bus_v2.emit_event({"type": "task_updated", "task_id": "task-A"})

    merged = received[0]
    assert set(merged.keys()) == {"type", "agent_ref", "delta"}
    assert merged == {"type": "reasoning_delta", "agent_ref": "task-A", "delta": "ab"}


# --- Ring buffer (debug feed) sees the merged event, not the burst ----------------


async def test_ring_buffer_volume_is_bounded_too() -> None:
    for i in range(100):
        await _emit_speech("m1", f"t{i} ")
    await asyncio.sleep(_window_seconds() * 1.5)

    speech_events = [
        e
        for e in debug_log.snapshot()
        if e.payload.get("ws_event", {}).get("type") == "speech_delta"
    ]
    assert len(speech_events) == 1
    assert speech_events[0].payload["ws_event"]["delta"] == "".join(f"t{i} " for i in range(100))


async def test_merged_event_inherits_the_producer_task_context() -> None:
    # The flush may run from a foreign context (here: a sibling cold emit
    # outside any task scope). The merged ring-buffer event must still carry
    # the PRODUCER's task id so the per-task overlay sees the stream.
    async def _producer() -> None:
        token = start_task("task-ctx")
        try:
            await _emit_reasoning("task-ctx", "thinking")
        finally:
            current_task_id.reset(token)

    await asyncio.create_task(_producer())
    await event_bus_v2.emit_event({"type": "assistant_msg", "msg_id": "m1"})  # foreign flush

    [reasoning] = [
        e
        for e in debug_log.snapshot()
        if e.payload.get("ws_event", {}).get("type") == "reasoning_delta"
    ]
    assert reasoning.task_id == "task-ctx"


# --- The window dial is honored ----------------------------------------------------


async def test_window_zero_disables_coalescing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        event_bus_v2,
        "get_settings",
        lambda: SimpleNamespace(WS_HOT_EVENT_BATCH_WINDOW_MS=0, WS_EMITTER_TIMEOUT_SECONDS=1.5),
    )
    received: list[dict[str, Any]] = []
    event_bus_v2.add_ws_emitter(_recorder(received))

    await _emit_speech("m1", "a")
    await _emit_speech("m1", "b")

    # Pre-0123 behaviour: one frame per delta, immediately.
    assert [e["delta"] for e in received] == ["a", "b"]


async def test_configured_window_bounds_the_delivery_delay(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        event_bus_v2,
        "get_settings",
        lambda: SimpleNamespace(WS_HOT_EVENT_BATCH_WINDOW_MS=200, WS_EMITTER_TIMEOUT_SECONDS=1.5),
    )
    loop = asyncio.get_running_loop()
    received: list[dict[str, Any]] = []
    event_bus_v2.add_ws_emitter(_recorder(received))

    started = loop.time()
    await _emit_speech("m1", "hello")
    while not received:
        await asyncio.sleep(0.01)
    delivered_after = loop.time() - started

    assert delivered_after <= 0.2 + 0.05
    assert received[0]["delta"] == "hello"


# --- Teardown hook -----------------------------------------------------------------


async def test_flush_hot_events_drains_pending_deltas_on_demand() -> None:
    received: list[dict[str, Any]] = []
    event_bus_v2.add_ws_emitter(_recorder(received))

    await _emit_speech("m1", "partial ")
    await _emit_speech("m1", "tail")
    await event_bus_v2.flush_hot_events()

    assert [e["delta"] for e in received] == ["partial tail"]
