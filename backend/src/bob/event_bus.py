"""In-process pub/sub bus for sub-agent / orchestrator events (slice #0021).

The orchestrator and the sub-agent runner produce *internal* events that
several handlers might want to observe — the WS emitter pushes user-facing
events to the frontend, but the :class:`ProactivityHandler` also needs to
react to a sub-agent's ``ask_user`` transition by triggering a Jarvis
paraphrase turn.

Threading these subscribers through every producer would couple unrelated
layers. The :class:`EventBus` is a tiny asyncio-based pub/sub:

- Subscribers register an async callable against a topic string.
- :meth:`publish` schedules each subscriber via :func:`asyncio.create_task`
  so a slow / failing subscriber cannot block the others (or the producer).
- A strong-ref set keeps the in-flight subscriber tasks alive against the GC
  (PEP-715 / ruff ``RUF006``).

Topics and payloads
-------------------

- ``task_state_changed``: ``{task_id, old_state, new_state, action?}``.
  ``action`` is set when the transition was driven by a sub-agent emit
  (``done`` / ``ask_user`` / ``progress``); absent when the scheduler or
  orchestrator drove it (e.g. ``pending → running``).
- ``task_message_added``: ``{task_id, message_id, role, action?}``. Emitted
  every time :meth:`TaskStore.append_message` lands a new row. Reserved for
  later slices that need to react to message additions (e.g. progress UI).

Subscribers' exceptions are logged and swallowed — a single broken handler
must not poison the bus for the rest.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import Any

import structlog

_logger = structlog.get_logger(__name__)


Subscriber = Callable[[dict[str, Any]], Awaitable[None]]


class EventBus:
    """In-process pub/sub bus for internal orchestrator / sub-agent events."""

    def __init__(self) -> None:
        self._subs: dict[str, list[Subscriber]] = {}
        # Strong refs to in-flight subscriber tasks so the GC does not drop
        # them mid-execution. Each task removes itself via the done-callback.
        self._inflight: set[asyncio.Task[None]] = set()

    def subscribe(self, topic: str, fn: Subscriber) -> None:
        """Register ``fn`` to receive every payload published on ``topic``."""

        self._subs.setdefault(topic, []).append(fn)

    def unsubscribe(self, topic: str, fn: Subscriber) -> None:
        """Remove ``fn`` from ``topic``. No-op if it wasn't subscribed."""

        subs = self._subs.get(topic)
        if not subs:
            return
        try:
            subs.remove(fn)
        except ValueError:
            return
        if not subs:
            self._subs.pop(topic, None)

    async def publish(self, topic: str, payload: dict[str, Any]) -> None:
        """Fire-and-forget broadcast of ``payload`` to every subscriber of ``topic``.

        Each subscriber is wrapped in its own :func:`asyncio.create_task` so a
        slow or failing handler does not block siblings or the producer.
        Exceptions raised inside a handler are logged but not re-raised.
        """

        subs = list(self._subs.get(topic, ()))
        for fn in subs:
            task = asyncio.create_task(self._run_subscriber(topic, fn, payload))
            self._inflight.add(task)
            task.add_done_callback(self._inflight.discard)

    async def _run_subscriber(
        self,
        topic: str,
        fn: Subscriber,
        payload: dict[str, Any],
    ) -> None:
        try:
            await fn(payload)
        except Exception:
            _logger.exception(
                "event_bus.subscriber_failed",
                topic=topic,
                subscriber=getattr(fn, "__qualname__", repr(fn)),
            )


# --- Singleton plumbing -------------------------------------------------------
#
# Mirrors :mod:`bob.task_store`. The boot path in :mod:`bob.main` primes the
# singleton; tests prime it themselves when they need it.

_DEFAULT_BUS: EventBus | None = None


def set_event_bus(bus: EventBus | None) -> None:
    """Install (or clear) the process-wide :class:`EventBus` singleton."""

    global _DEFAULT_BUS
    _DEFAULT_BUS = bus


def get_event_bus() -> EventBus:
    """Return the process-wide singleton, lazily building one if absent.

    The lazy build is important: producers (sub-agent runner, orchestrator)
    can publish unconditionally even when no boot has run yet (unit tests).
    When no subscriber has been wired in, ``publish`` is effectively a no-op.
    """

    global _DEFAULT_BUS
    if _DEFAULT_BUS is None:
        _DEFAULT_BUS = EventBus()
    return _DEFAULT_BUS
