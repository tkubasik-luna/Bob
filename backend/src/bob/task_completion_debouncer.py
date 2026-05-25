"""Debounce queue for batched task-completion announcements.

PRD 0006 / issue 0050 — when several sub-tasks finish within a tight
window (~300 ms), Jarvis should batch them into a single utterance
rather than interrupt himself for every individual done event. The
PRD calls out the user-facing benefit: "voilà tes 3 trucs" feels
human; three back-to-back announcements feels robotic.

Mechanics:

* Each ``schedule(task_id)`` call registers a completion. The
  scheduler resets a deadline ``window_seconds`` from the *first*
  pending entry — additional completions within the window are folded
  into the same batch.
* After the window elapses the registered ``flush_callback`` fires
  with the ordered list of batched task ids and the batch is reset.
* The clock + scheduling primitives are injectable so tests can
  drive the debouncer deterministically without ``asyncio.sleep``.

The orchestrator is the canonical caller: it subscribes to
``task_state_changed`` events from the runner, invokes
``schedule(task_id)`` on every ``done`` transition, and the flush
callback materialises the synthetic ``task_completed`` :class:`ContextEntry`
and pushes the spoken announcement.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import Protocol

import structlog

_logger = structlog.get_logger(__name__)


#: Default debounce window (PRD 0006 / issue 0050 — ~300 ms).
DEFAULT_DEBOUNCE_SECONDS = 0.3


class _Clock(Protocol):
    def __call__(self) -> float: ...  # pragma: no cover — protocol member.


class _Scheduler(Protocol):
    """Adapter wrapping the loop-time deferred-call primitive.

    Production wires :func:`asyncio.get_event_loop().call_later`; the
    tests inject a deterministic stand-in that records the requested
    delay and lets the test drive the firing manually.
    """

    def call_after(
        self,
        delay: float,
        callback: Callable[[], None],
    ) -> _Handle: ...  # pragma: no cover — protocol member.


class _Handle(Protocol):
    def cancel(self) -> None: ...  # pragma: no cover — protocol member.


FlushCallback = Callable[[list[str]], Awaitable[None]]


class _AsyncioScheduler:
    """Production :class:`_Scheduler` adapter over the running event loop."""

    def call_after(self, delay: float, callback: Callable[[], None]) -> _Handle:
        loop = asyncio.get_event_loop()
        return _AsyncioHandle(loop.call_later(delay, callback))


class _AsyncioHandle:
    def __init__(self, handle: asyncio.TimerHandle) -> None:
        self._handle = handle

    def cancel(self) -> None:
        self._handle.cancel()


class TaskCompletionDebouncer:
    """Coalesce a burst of completed task ids into a single flush.

    Construction args:

    - ``flush_callback`` — async callable invoked with the batched
      ``task_id`` list once the window elapses. The orchestrator wires
      its synthesis-and-announce path here.
    - ``window_seconds`` — debounce window. Defaults to
      :data:`DEFAULT_DEBOUNCE_SECONDS`.
    - ``clock`` — read-only clock (returns ``float``). Used by
      :meth:`pending_count` introspection; the firing mechanism uses
      the injected scheduler, not the clock.
    - ``scheduler`` — :class:`_Scheduler` adapter. Defaults to
      :class:`_AsyncioScheduler` so production calls
      :func:`asyncio.get_event_loop().call_later`. Tests inject a
      deterministic stand-in.
    """

    def __init__(
        self,
        *,
        flush_callback: FlushCallback,
        window_seconds: float = DEFAULT_DEBOUNCE_SECONDS,
        clock: _Clock | None = None,
        scheduler: _Scheduler | None = None,
    ) -> None:
        if window_seconds < 0:
            raise ValueError(
                f"TaskCompletionDebouncer.window_seconds must be >= 0, got {window_seconds}"
            )
        self._flush_callback = flush_callback
        self._window = window_seconds
        self._clock = clock or (lambda: 0.0)
        self._scheduler = scheduler or _AsyncioScheduler()
        self._pending: list[str] = []
        self._handle: _Handle | None = None
        self._lock = asyncio.Lock()

    @property
    def window_seconds(self) -> float:
        return self._window

    def pending_count(self) -> int:
        """Return the number of task ids waiting to be flushed."""

        return len(self._pending)

    async def schedule(self, task_id: str) -> None:
        """Register ``task_id`` for the next batched flush.

        Idempotent on duplicates: registering the same ``task_id``
        twice keeps a single entry (the second call resets the timer
        but does not duplicate the announcement).
        """

        async with self._lock:
            if task_id in self._pending:
                # Reset the timer but keep ordering — the user expects
                # the most recent burst window to apply.
                _logger.debug(
                    "task_completion_debouncer.duplicate_schedule",
                    task_id=task_id,
                )
            else:
                self._pending.append(task_id)
            self._reschedule_locked()

    async def flush_now(self) -> None:
        """Force an immediate flush (used at shutdown or by tests)."""

        async with self._lock:
            if self._handle is not None:
                self._handle.cancel()
                self._handle = None
            if not self._pending:
                return
            batch = list(self._pending)
            self._pending.clear()
        await self._flush_callback(batch)

    def _reschedule_locked(self) -> None:
        """Reset the deferred firing handle. Called under :attr:`_lock`."""

        if self._handle is not None:
            self._handle.cancel()
        if self._window <= 0:
            # Immediate-flush mode (test convenience).
            self._handle = self._scheduler.call_after(0.0, self._on_timer)
            return
        self._handle = self._scheduler.call_after(self._window, self._on_timer)

    def _on_timer(self) -> None:
        """Fire the flush callback. Synchronous trampoline → async task."""

        # Drain into a local list before scheduling the coroutine so a
        # late ``schedule`` race lands in the next batch, not this one.
        batch = list(self._pending)
        self._pending.clear()
        self._handle = None
        if not batch:
            return

        async def _runner() -> None:
            await self._flush_callback(batch)

        try:
            asyncio.get_running_loop().create_task(_runner())
        except RuntimeError:
            _logger.warning(
                "task_completion_debouncer.no_running_loop",
                batch_size=len(batch),
            )


__all__ = [
    "DEFAULT_DEBOUNCE_SECONDS",
    "FlushCallback",
    "TaskCompletionDebouncer",
]
