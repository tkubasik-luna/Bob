"""Tests for :mod:`bob.event_bus`."""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from typing import Any

import pytest

from bob.event_bus import EventBus, get_event_bus, set_event_bus


@pytest.fixture(autouse=True)
def _reset_singleton() -> Iterator[None]:
    set_event_bus(None)
    yield
    set_event_bus(None)


async def _drain() -> None:
    """Yield enough event-loop ticks for fire-and-forget subscribers to run."""

    for _ in range(5):
        await asyncio.sleep(0)


@pytest.mark.asyncio
async def test_publish_with_no_subscribers_is_noop() -> None:
    bus = EventBus()
    await bus.publish("task_state_changed", {"task_id": "abc"})
    # Nothing to assert — just confirms ``publish`` doesn't raise.


@pytest.mark.asyncio
async def test_subscriber_receives_published_payload() -> None:
    bus = EventBus()
    received: list[dict[str, Any]] = []

    async def sub(payload: dict[str, Any]) -> None:
        received.append(payload)

    bus.subscribe("task_state_changed", sub)
    await bus.publish("task_state_changed", {"task_id": "abc", "new_state": "done"})
    await _drain()

    assert received == [{"task_id": "abc", "new_state": "done"}]


@pytest.mark.asyncio
async def test_multiple_subscribers_all_invoked() -> None:
    bus = EventBus()
    received_a: list[dict[str, Any]] = []
    received_b: list[dict[str, Any]] = []

    async def sub_a(payload: dict[str, Any]) -> None:
        received_a.append(payload)

    async def sub_b(payload: dict[str, Any]) -> None:
        received_b.append(payload)

    bus.subscribe("task_state_changed", sub_a)
    bus.subscribe("task_state_changed", sub_b)
    await bus.publish("task_state_changed", {"task_id": "x"})
    await _drain()

    assert received_a == [{"task_id": "x"}]
    assert received_b == [{"task_id": "x"}]


@pytest.mark.asyncio
async def test_topic_isolation() -> None:
    bus = EventBus()
    received_a: list[dict[str, Any]] = []
    received_b: list[dict[str, Any]] = []

    async def sub_a(payload: dict[str, Any]) -> None:
        received_a.append(payload)

    async def sub_b(payload: dict[str, Any]) -> None:
        received_b.append(payload)

    bus.subscribe("task_state_changed", sub_a)
    bus.subscribe("task_message_added", sub_b)
    await bus.publish("task_state_changed", {"task_id": "x"})
    await bus.publish("task_message_added", {"task_id": "y"})
    await _drain()

    assert received_a == [{"task_id": "x"}]
    assert received_b == [{"task_id": "y"}]


@pytest.mark.asyncio
async def test_failing_subscriber_does_not_break_others() -> None:
    """Critical contract: a buggy handler must not poison the bus."""

    bus = EventBus()
    received: list[dict[str, Any]] = []

    async def boom(_payload: dict[str, Any]) -> None:
        raise RuntimeError("kaboom")

    async def good(payload: dict[str, Any]) -> None:
        received.append(payload)

    bus.subscribe("task_state_changed", boom)
    bus.subscribe("task_state_changed", good)

    # ``publish`` itself must not raise — fire-and-forget.
    await bus.publish("task_state_changed", {"task_id": "x"})
    await _drain()

    assert received == [{"task_id": "x"}]


@pytest.mark.asyncio
async def test_unsubscribe_stops_delivery() -> None:
    bus = EventBus()
    received: list[dict[str, Any]] = []

    async def sub(payload: dict[str, Any]) -> None:
        received.append(payload)

    bus.subscribe("task_state_changed", sub)
    await bus.publish("task_state_changed", {"task_id": "x"})
    await _drain()

    bus.unsubscribe("task_state_changed", sub)
    await bus.publish("task_state_changed", {"task_id": "y"})
    await _drain()

    assert received == [{"task_id": "x"}]


@pytest.mark.asyncio
async def test_get_event_bus_returns_singleton() -> None:
    bus_a = get_event_bus()
    bus_b = get_event_bus()
    assert bus_a is bus_b


@pytest.mark.asyncio
async def test_set_event_bus_overrides_singleton() -> None:
    custom = EventBus()
    set_event_bus(custom)
    assert get_event_bus() is custom
