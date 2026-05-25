"""Per-task addendum :class:`asyncio.Queue` (PRD 0006 / issue 0045).

The PRD's ``addendum_task`` Jarvis tool (which lands in 0050) lets the
user inject mid-flight information into a running sub-agent without
restarting it. The wire shape is "Jarvis appends a free-form string to
a queue tied to the task; the sub-agent drains it at the next iteration
boundary and folds the message into its next LLM prompt".

This module ships the *queue half* of that contract: one
:class:`AddendumQueue` per running task, owned by the runner. The
**user-facing producer side ships in 0050** — at this slice the queue is
created and drained but no caller fills it. Tests inject items directly
to assert the drain semantics.

Why "iteration boundary only"
-----------------------------

Draining mid-iteration would let an addendum interrupt a tool call or
an LLM response, which is exactly the unpredictability the PRD set
out to kill. The runner therefore checks :meth:`drain` at one explicit
point only: between iterations, after the previous action was handled
and before the next LLM call is issued.

Threading model
---------------

The queue is :class:`asyncio.Queue`, single-consumer (the runner) and
multi-producer (Jarvis, tests). FIFO order is preserved. There is no
back-pressure: producers always succeed because the queue is unbounded
— the runner is fast enough to drain a handful of addenda per
iteration and the per-task budget cap (0048) is the real guard against
runaway producers.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import UTC, datetime


@dataclass(frozen=True)
class AddendumEntry:
    """One enqueued addendum.

    Fields:

    - ``text``: the user-supplied addition. Surfaced verbatim to the LLM
      so callers are expected to keep it short (the 0050 tool will cap
      the length on the producer side).
    - ``enqueued_at``: ISO 8601 UTC timestamp set by :meth:`AddendumQueue.put`.
      Useful for ordering in the LLM prompt ("…the user added at
      14:23:01: …") and for later debug-event correlation.
    """

    text: str
    enqueued_at: str


class AddendumQueue:
    """FIFO addendum queue for a single task.

    Producers call :meth:`put` (fire-and-forget). The runner calls
    :meth:`drain` at every iteration boundary and folds the returned
    list of entries into the next LLM prompt.
    """

    def __init__(self) -> None:
        self._queue: asyncio.Queue[AddendumEntry] = asyncio.Queue()

    def put(self, text: str) -> None:
        """Enqueue ``text`` for the next iteration boundary (synchronous).

        The text is trimmed of leading/trailing whitespace; empty
        strings are silently dropped (the 0050 tool will already
        reject empty input on the producer side, but defending in
        depth keeps the queue clean).

        Synchronous because the underlying :class:`asyncio.Queue` is
        unbounded — :meth:`asyncio.Queue.put_nowait` never raises here.
        That removes the need for callers (Jarvis tools, tests) to
        await an addendum injection.
        """

        clean = text.strip()
        if not clean:
            return
        self._queue.put_nowait(AddendumEntry(text=clean, enqueued_at=_now_iso()))

    def drain(self) -> list[AddendumEntry]:
        """Return every pending entry and empty the queue.

        Synchronous — :meth:`asyncio.Queue.get_nowait` is non-blocking
        and we want a single call to capture the whole batch atomically
        from the runner's POV (no extra ``await`` point inside the
        drain → no new iteration boundary).
        """

        drained: list[AddendumEntry] = []
        while True:
            try:
                drained.append(self._queue.get_nowait())
            except asyncio.QueueEmpty:
                break
        return drained

    def qsize(self) -> int:
        """Current pending count — for tests + observability."""

        return self._queue.qsize()


def _now_iso() -> str:
    now = datetime.now(UTC)
    return now.strftime("%Y-%m-%dT%H:%M:%S.") + f"{now.microsecond // 1000:03d}Z"


__all__ = ["AddendumEntry", "AddendumQueue"]
