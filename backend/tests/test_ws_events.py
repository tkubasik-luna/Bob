"""Tests for :mod:`bob.ws_events`.

The broadcaster is intentionally tiny: a module-level emitter callable +
``emit()`` that no-ops when none is set. These tests pin the contract.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import pytest

from bob import ws_events


@pytest.fixture(autouse=True)
def _reset_emitter() -> Iterator[None]:
    """Make sure no emitter leaks between tests."""

    ws_events.set_emitter(None)
    yield
    ws_events.set_emitter(None)


@pytest.mark.asyncio
async def test_emit_is_noop_without_emitter() -> None:
    # No registered emitter → emit() must complete without error.
    await ws_events.emit({"type": "task_created", "task_id": "abc"})


@pytest.mark.asyncio
async def test_emit_forwards_to_registered_emitter() -> None:
    received: list[dict[str, Any]] = []

    async def _emitter(event: dict[str, Any]) -> None:
        received.append(event)

    ws_events.set_emitter(_emitter)
    await ws_events.emit({"type": "task_created", "task_id": "abc"})
    await ws_events.emit({"type": "task_updated", "task_id": "abc", "state": "running"})

    assert received == [
        {"type": "task_created", "task_id": "abc"},
        {"type": "task_updated", "task_id": "abc", "state": "running"},
    ]


@pytest.mark.asyncio
async def test_set_emitter_none_reverts_to_noop() -> None:
    received: list[dict[str, Any]] = []

    async def _emitter(event: dict[str, Any]) -> None:
        received.append(event)

    ws_events.set_emitter(_emitter)
    await ws_events.emit({"type": "task_created", "task_id": "x"})
    ws_events.set_emitter(None)
    await ws_events.emit({"type": "task_created", "task_id": "y"})

    # Only the first event was delivered; the second was a no-op.
    assert received == [{"type": "task_created", "task_id": "x"}]


@pytest.mark.asyncio
async def test_set_emitter_replaces_previous() -> None:
    received_a: list[dict[str, Any]] = []
    received_b: list[dict[str, Any]] = []

    async def _emitter_a(event: dict[str, Any]) -> None:
        received_a.append(event)

    async def _emitter_b(event: dict[str, Any]) -> None:
        received_b.append(event)

    ws_events.set_emitter(_emitter_a)
    await ws_events.emit({"type": "task_created", "task_id": "x"})
    ws_events.set_emitter(_emitter_b)
    await ws_events.emit({"type": "task_created", "task_id": "y"})

    assert received_a == [{"type": "task_created", "task_id": "x"}]
    assert received_b == [{"type": "task_created", "task_id": "y"}]
