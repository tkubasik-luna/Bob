"""Tests for :class:`bob.task_completion_debouncer.TaskCompletionDebouncer`.

PRD 0006 / issue 0050. The debouncer is driven by an injectable
scheduler so the tests never sleep. We capture the requested delay /
callback and fire it manually.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import cast

import pytest

from bob.task_completion_debouncer import TaskCompletionDebouncer


@dataclass
class _StubHandle:
    cancelled: bool = False

    def cancel(self) -> None:
        self.cancelled = True


@dataclass
class _StubScheduler:
    """Records ``(delay, callback)`` pairs without actually firing them."""

    pending: list[tuple[float, Callable[[], None]]] = field(default_factory=list)
    handles: list[_StubHandle] = field(default_factory=list)

    def call_after(self, delay: float, callback: Callable[[], None]) -> _StubHandle:
        handle = _StubHandle()
        self.pending.append((delay, callback))
        self.handles.append(handle)
        return handle

    def fire_last(self) -> None:
        """Invoke the most recently scheduled callback (synchronous)."""

        if not self.pending:
            raise AssertionError("no pending callback to fire")
        _, cb = self.pending[-1]
        cb()


@pytest.mark.asyncio
async def test_single_completion_batched_after_window() -> None:
    flushed: list[list[str]] = []

    async def _flush(batch: list[str]) -> None:
        flushed.append(batch)

    scheduler = _StubScheduler()
    debouncer = TaskCompletionDebouncer(
        flush_callback=_flush,
        window_seconds=0.3,
        scheduler=cast("object", scheduler),  # type: ignore[arg-type]
    )

    await debouncer.schedule("task-1")
    # Timer scheduled but not fired.
    assert scheduler.pending
    assert debouncer.pending_count() == 1
    # Drive the firing.
    await debouncer.flush_now()
    assert flushed == [["task-1"]]


@pytest.mark.asyncio
async def test_multiple_completions_within_window_batched() -> None:
    flushed: list[list[str]] = []

    async def _flush(batch: list[str]) -> None:
        flushed.append(batch)

    scheduler = _StubScheduler()
    debouncer = TaskCompletionDebouncer(
        flush_callback=_flush,
        window_seconds=0.3,
        scheduler=cast("object", scheduler),  # type: ignore[arg-type]
    )

    await debouncer.schedule("task-A")
    await debouncer.schedule("task-B")
    await debouncer.schedule("task-C")
    assert debouncer.pending_count() == 3
    await debouncer.flush_now()
    assert flushed == [["task-A", "task-B", "task-C"]]


@pytest.mark.asyncio
async def test_duplicate_schedule_collapsed_to_single_entry() -> None:
    flushed: list[list[str]] = []

    async def _flush(batch: list[str]) -> None:
        flushed.append(batch)

    scheduler = _StubScheduler()
    debouncer = TaskCompletionDebouncer(
        flush_callback=_flush,
        window_seconds=0.3,
        scheduler=cast("object", scheduler),  # type: ignore[arg-type]
    )

    await debouncer.schedule("task-A")
    await debouncer.schedule("task-A")
    await debouncer.flush_now()
    assert flushed == [["task-A"]]


@pytest.mark.asyncio
async def test_window_zero_immediate_flush() -> None:
    flushed: list[list[str]] = []

    async def _flush(batch: list[str]) -> None:
        flushed.append(batch)

    scheduler = _StubScheduler()
    debouncer = TaskCompletionDebouncer(
        flush_callback=_flush,
        window_seconds=0.0,
        scheduler=cast("object", scheduler),  # type: ignore[arg-type]
    )

    await debouncer.schedule("task-A")
    # Even with a zero window the test scheduler still has to be
    # ticked — pin that contract.
    assert scheduler.pending
    await debouncer.flush_now()
    assert flushed == [["task-A"]]


@pytest.mark.asyncio
async def test_negative_window_rejected() -> None:
    async def _flush(batch: list[str]) -> None:
        return None

    with pytest.raises(ValueError):
        TaskCompletionDebouncer(flush_callback=_flush, window_seconds=-1.0)


@pytest.mark.asyncio
async def test_empty_flush_now_is_noop() -> None:
    flushed: list[list[str]] = []

    async def _flush(batch: list[str]) -> None:
        flushed.append(batch)

    debouncer = TaskCompletionDebouncer(flush_callback=_flush, window_seconds=0.3)
    await debouncer.flush_now()
    assert flushed == []
