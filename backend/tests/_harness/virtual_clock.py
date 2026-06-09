"""Virtual-time asyncio event loop — timeout tests with zero real waiting.

Issue 0122 (and any other timeout-shaped behavior) needs to assert things
like "a hung WS emitter is evicted within ``WS_EMITTER_TIMEOUT_SECONDS``"
without the suite actually sleeping for that long. No external dependency
is allowed (PRD 0018 cross-cutting constraint), so this module implements
the classic selector-jump trick in ~60 lines:

- :class:`VirtualTimeLoop` is a regular :class:`asyncio.SelectorEventLoop`
  whose :meth:`~VirtualTimeLoop.time` adds a controllable offset to the
  real monotonic clock.
- Its selector is wrapped so that any *blocking* wait (the loop waiting
  for its earliest timer to come due) is converted into an instant jump
  of the virtual offset. Timers therefore fire immediately in real time
  while the loop still observes the full virtual delay.

Everything scheduled through the loop (``asyncio.sleep``,
``asyncio.wait_for``, ``loop.call_later`` ...) sees consistent virtual
time because both the schedule side (``when = loop.time() + delay``) and
the wait side (``timeout = when - loop.time()``) go through
:meth:`VirtualTimeLoop.time`.

Usage with pytest-asyncio — override the ``event_loop_policy`` fixture in
the test module::

    @pytest.fixture
    def event_loop_policy() -> asyncio.AbstractEventLoopPolicy:
        return VirtualTimePolicy()

Limitation: a coroutine that waits on *real* external I/O with no pending
timer still blocks for real (the selector only jumps when the loop hands
it a finite timeout). That is the right behavior for unit tests — a test
that deadlocks with no timer is a genuine bug, not a clock artifact.
"""

from __future__ import annotations

import asyncio
import selectors
from collections.abc import Mapping
from typing import Any


class _TimeJumpingSelector(selectors.BaseSelector):
    """Selector wrapper: finite blocking waits become virtual-time jumps."""

    def __init__(self, loop: VirtualTimeLoop) -> None:
        self._loop = loop
        self._inner = selectors.DefaultSelector()

    def register(self, fileobj: Any, events: int, data: Any = None) -> selectors.SelectorKey:
        return self._inner.register(fileobj, events, data)

    def unregister(self, fileobj: Any) -> selectors.SelectorKey:
        return self._inner.unregister(fileobj)

    def modify(self, fileobj: Any, events: int, data: Any = None) -> selectors.SelectorKey:
        return self._inner.modify(fileobj, events, data)

    def select(self, timeout: float | None = None) -> list[tuple[selectors.SelectorKey, int]]:
        if timeout is not None and timeout > 0:
            # The loop wants to sleep until its earliest timer: jump the
            # virtual clock there instantly instead of really sleeping.
            self._loop.advance(timeout)
            timeout = 0
        return self._inner.select(timeout)

    def close(self) -> None:
        self._inner.close()

    def get_key(self, fileobj: Any) -> selectors.SelectorKey:
        return self._inner.get_key(fileobj)

    def get_map(self) -> Mapping[Any, selectors.SelectorKey]:
        return self._inner.get_map()


class VirtualTimeLoop(asyncio.SelectorEventLoop):
    """Selector event loop running on a virtual monotonic clock."""

    def __init__(self) -> None:
        self._virtual_offset = 0.0
        super().__init__(_TimeJumpingSelector(self))

    def time(self) -> float:
        return super().time() + self._virtual_offset

    def advance(self, seconds: float) -> None:
        """Jump the virtual clock forward by ``seconds``."""

        self._virtual_offset += seconds


class VirtualTimePolicy(asyncio.DefaultEventLoopPolicy):
    """Event-loop policy producing :class:`VirtualTimeLoop` instances.

    Return one from a module-local ``event_loop_policy`` fixture to run
    every async test of that module under the virtual clock.
    """

    def new_event_loop(self) -> VirtualTimeLoop:
        return VirtualTimeLoop()
