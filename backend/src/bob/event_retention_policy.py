"""Retention policy for the unified event ring buffer (issue 0052).

PRD 0006 collapses ``ws_events.emit`` and ``emit_debug`` into a single
producer with one ring buffer. The ring buffer already has a count cap
(``_RING_BUFFER_MAXLEN`` in :mod:`bob.debug_log`). Issue 0052 adds two
additional bounds enforced on every emit:

- ``max_bytes``: total estimated wire-shape bytes of the buffered events.
  Estimated via ``len(json.dumps(event.to_dict()))`` so the size accounts
  for what an overlay client actually receives.
- ``max_age_seconds``: events older than this are dropped before the
  buffer is consulted. Wall-clock based.

Both fields are nullable — ``None`` means "do not enforce this dimension".
A policy with both fields ``None`` is effectively a no-op (the deque
``maxlen`` is the only remaining cap).

Why not count + kind awareness? That alternative was rejected during PRD
review: count alone is too crude (a single fat payload can starve the
buffer), and kind-awareness coupled the producer to consumer semantics.
Bytes + age stays consumer-agnostic and lines up with the actual wire
cost an overlay subscriber pays.

Singleton plumbing matches :mod:`bob.event_bus_v2`: the boot path
(:mod:`bob.main`) installs the default policy; tests can swap a tighter
one in to assert eviction behaviour without producing thousands of events.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class EventRetentionPolicy:
    """Two-dimensional cap on the event ring buffer.

    ``max_bytes`` and ``max_age_seconds`` are both nullable so a caller
    can enforce a single dimension without bolting on a placeholder for
    the other. The default policy installed by the boot path enables
    both with conservative values.
    """

    max_bytes: int | None = None
    max_age_seconds: float | None = None


#: Default policy installed by :mod:`bob.main`. 1 MiB and 1 h were chosen
#: empirically: the producer emits at most a few hundred events per turn,
#: and Bob is a single-user local app — a long session of a few hours is
#: the worst case, and we want the overlay snapshot to remain useful for
#: that whole window.
DEFAULT_RETENTION_POLICY = EventRetentionPolicy(
    max_bytes=1 * 1024 * 1024,
    max_age_seconds=60 * 60,
)


_DEFAULT_POLICY: EventRetentionPolicy | None = DEFAULT_RETENTION_POLICY


def set_retention_policy(policy: EventRetentionPolicy | None) -> None:
    """Install (or clear) the process-wide retention policy singleton.

    ``None`` disables retention enforcement beyond the deque ``maxlen``.
    """

    global _DEFAULT_POLICY
    _DEFAULT_POLICY = policy


def get_retention_policy() -> EventRetentionPolicy | None:
    """Return the currently installed policy, or ``None`` if disabled."""

    return _DEFAULT_POLICY


__all__ = [
    "DEFAULT_RETENTION_POLICY",
    "EventRetentionPolicy",
    "get_retention_policy",
    "set_retention_policy",
]
