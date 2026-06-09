"""WS fan-out hardening — per-emitter timeout + eviction (issue 0122).

The broadcast loop in :func:`bob.event_bus_v2.emit_event` bounds every
``await emitter(payload)`` with ``WS_EMITTER_TIMEOUT_SECONDS`` and evicts
an emitter that times out or raises: it receives nothing further and can
no longer block the others. A zombie HUD or debug window must never
freeze the orchestrator again.

Tests target EXTERNAL behavior only — which events each window receives,
in which order, and how much (virtual) time the producer was blocked.
Every async test runs under the :mod:`tests._harness.virtual_clock`
fake clock, so the 1.5 s default timeout costs zero real wall time.
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from types import SimpleNamespace
from typing import Any

import pytest
from structlog.testing import capture_logs

from bob import debug_log, event_bus_v2
from bob.config import Settings, get_settings
from bob.debug_log import clear, current_task_id, current_turn_id

from ._harness.virtual_clock import VirtualTimeLoop, VirtualTimePolicy


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


def _event(seq: int) -> dict[str, Any]:
    return {"type": "task_updated", "task_id": "task-A", "seq": seq}


# --- Acceptance: timeout is a setting, default 1-2 s --------------------------


def test_emitter_timeout_is_a_setting_with_default_between_one_and_two_seconds() -> None:
    default = Settings.model_fields["WS_EMITTER_TIMEOUT_SECONDS"].default
    assert 1.0 <= default <= 2.0


# --- Acceptance: hung emitter ---------------------------------------------------


async def test_hung_emitter_is_evicted_within_timeout_and_healthy_window_still_served() -> None:
    loop = asyncio.get_running_loop()
    assert isinstance(loop, VirtualTimeLoop)  # fake clock active — no real waiting

    healthy: list[dict[str, Any]] = []
    hung_received: list[dict[str, Any]] = []

    async def _hung(event: dict[str, Any]) -> None:
        hung_received.append(event)
        await asyncio.Event().wait()  # never completes

    event_bus_v2.add_ws_emitter(_recorder(healthy))
    event_bus_v2.add_ws_emitter(_hung)

    timeout = get_settings().WS_EMITTER_TIMEOUT_SECONDS

    started = loop.time()
    await event_bus_v2.emit_event(_event(1))
    blocked_for = loop.time() - started

    # The producer is released within the timeout — never unbounded — and the
    # healthy window got the event despite its hung sibling.
    assert blocked_for <= timeout + 0.1
    assert healthy == [_event(1)]

    # Subsequent events flow to the healthy window with no added delay, and
    # the evicted emitter receives nothing further.
    started = loop.time()
    await event_bus_v2.emit_event(_event(2))
    await event_bus_v2.emit_event(_event(3))
    assert loop.time() - started < 0.1

    assert healthy == [_event(1), _event(2), _event(3)]
    assert hung_received == [_event(1)]


async def test_hung_emitter_does_not_delay_the_healthy_sibling_on_the_same_event() -> None:
    # Forwards fan out concurrently: the healthy window receives the event
    # immediately, not after the hung sibling's timeout expires.
    healthy: list[dict[str, Any]] = []
    healthy_got_event = asyncio.Event()

    async def _healthy(event: dict[str, Any]) -> None:
        healthy.append(event)
        healthy_got_event.set()

    async def _hung(event: dict[str, Any]) -> None:
        await asyncio.Event().wait()

    event_bus_v2.add_ws_emitter(_healthy)
    event_bus_v2.add_ws_emitter(_hung)

    loop = asyncio.get_running_loop()
    assert isinstance(loop, VirtualTimeLoop)
    emit = asyncio.ensure_future(event_bus_v2.emit_event(_event(1)))
    started = loop.time()
    await healthy_got_event.wait()
    assert loop.time() - started < 0.1  # healthy window served before any timeout
    await emit

    assert healthy == [_event(1)]


# --- Acceptance: throwing emitter ------------------------------------------------


async def test_raising_emitter_is_evicted_after_first_failure() -> None:
    loop = asyncio.get_running_loop()
    healthy: list[dict[str, Any]] = []
    broken_received: list[dict[str, Any]] = []

    async def _broken(event: dict[str, Any]) -> None:
        broken_received.append(event)
        raise RuntimeError("boom")

    event_bus_v2.add_ws_emitter(_recorder(healthy))
    event_bus_v2.add_ws_emitter(_broken)

    started = loop.time()
    await event_bus_v2.emit_event(_event(1))
    await event_bus_v2.emit_event(_event(2))
    await event_bus_v2.emit_event(_event(3))

    # Eviction is immediate (no timeout burned on an exception) and the broken
    # emitter never sees another event; the healthy window misses nothing.
    assert loop.time() - started < 0.1
    assert broken_received == [_event(1)]
    assert healthy == [_event(1), _event(2), _event(3)]


# --- Acceptance: nominal path — concurrent healthy windows ----------------------


async def test_two_healthy_windows_receive_all_events_in_order_with_no_delay() -> None:
    loop = asyncio.get_running_loop()
    window_a: list[dict[str, Any]] = []
    window_b: list[dict[str, Any]] = []

    event_bus_v2.add_ws_emitter(_recorder(window_a))
    event_bus_v2.add_ws_emitter(_recorder(window_b))

    events = [_event(seq) for seq in range(1, 6)]
    started = loop.time()
    for event in events:
        await event_bus_v2.emit_event(event)

    assert loop.time() - started < 0.1  # no timeout machinery on the happy path
    assert window_a == events
    assert window_b == events


# --- Acceptance: eviction is logged with context ---------------------------------


async def test_timeout_eviction_is_logged_with_emitter_and_reason() -> None:
    async def _hung(event: dict[str, Any]) -> None:
        await asyncio.Event().wait()

    event_bus_v2.add_ws_emitter(_hung)

    with capture_logs() as logs:
        await event_bus_v2.emit_event(_event(1))

    [eviction] = [log for log in logs if log["event"] == "event_bus_v2.ws_emitter_evicted"]
    assert eviction["reason"] == "timeout"
    assert "_hung" in eviction["emitter"]
    assert eviction["event_type"] == "task_updated"


async def test_raise_eviction_is_logged_with_emitter_and_reason() -> None:
    async def _broken(event: dict[str, Any]) -> None:
        raise RuntimeError("boom")

    event_bus_v2.add_ws_emitter(_broken)

    with capture_logs() as logs:
        await event_bus_v2.emit_event(_event(1))

    [eviction] = [log for log in logs if log["event"] == "event_bus_v2.ws_emitter_evicted"]
    assert eviction["reason"] == "raised"
    assert "_broken" in eviction["emitter"]
    assert eviction["event_type"] == "task_updated"


# --- The timeout dial is honored --------------------------------------------------


async def test_configured_timeout_bounds_the_eviction(monkeypatch: pytest.MonkeyPatch) -> None:
    # The fan-out reads the dial from settings on every emit; a custom value
    # bounds how long a hung emitter can hold the producer.
    monkeypatch.setattr(
        event_bus_v2,
        "get_settings",
        lambda: SimpleNamespace(WS_EMITTER_TIMEOUT_SECONDS=0.25),
    )

    async def _hung(event: dict[str, Any]) -> None:
        await asyncio.Event().wait()

    event_bus_v2.add_ws_emitter(_hung)

    loop = asyncio.get_running_loop()
    started = loop.time()
    await event_bus_v2.emit_event(_event(1))
    blocked_for = loop.time() - started

    assert blocked_for <= 0.25 + 0.1
