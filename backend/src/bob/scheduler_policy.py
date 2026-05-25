"""SchedulerPolicy — tuning dial for :class:`bob.task_scheduler.TaskScheduler`.

PRD 0006 / issue 0050 introduces hard caps on concurrent sub-tasks so
Jarvis can degrade gracefully under bursty user requests (user story
#33). The policy ships two knobs:

* ``max_running`` — the number of sub-agents allowed to execute under
  the shared :class:`asyncio.TaskGroup` at any one time. Defaults to
  ``3``.
* ``max_queued`` — the cap on tasks parked in ``spawned`` / ``pending``
  waiting for a slot. Defaults to ``5``. ``enqueue`` rejects with a
  structured overflow error once the queue is saturated; the dispatched
  tool surfaces that error to Jarvis, which emits a clarifying speech
  asking the user to cancel one (PRD user story #33).

The policy is a dataclass rather than module-level constants so future
tuning (per-task-type override, tighter dev caps) is a wiring change,
not a code rewrite. Tests construct their own policy when they need to
exercise the overflow path with smaller numbers.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class SchedulerPolicy:
    """How many sub-tasks the scheduler may juggle concurrently.

    Fields:

    - ``max_running`` — concurrent ``running`` slot cap. Must be ``>=
      1`` (the scheduler refuses to bootstrap a cap of zero — it would
      stall every spawn). Defaults to the PRD's ``3``.
    - ``max_queued`` — count of tasks waiting in ``spawned`` /
      ``pending`` for a slot to free up. Defaults to ``5``. A value of
      ``0`` means "every spawn must execute immediately or the call
      fails", which is the unit-test default in
      :mod:`tests.test_task_scheduler` overflow scenarios.
    """

    max_running: int = 3
    max_queued: int = 5

    def __post_init__(self) -> None:
        if self.max_running < 1:
            raise ValueError(f"SchedulerPolicy.max_running must be >= 1, got {self.max_running}")
        if self.max_queued < 0:
            raise ValueError(f"SchedulerPolicy.max_queued must be >= 0, got {self.max_queued}")


#: Structured error code surfaced by the dispatcher when ``enqueue``
#: refuses a spawn because the queue is saturated.
SCHEDULER_QUEUE_FULL_ERROR_CODE = "scheduler_queue_full"


class SchedulerQueueFull(RuntimeError):
    """Raised by :meth:`TaskScheduler.enqueue` when ``max_queued`` is exceeded.

    The dispatcher catches this and surfaces ``scheduler_queue_full`` to
    Jarvis, which emits the clarifying-speech fallback via the say tool
    (PRD 0006 user story #33).
    """

    def __init__(self, *, running: int, queued: int, max_running: int, max_queued: int) -> None:
        super().__init__(
            f"scheduler queue full: running={running}/{max_running}, queued={queued}/{max_queued}"
        )
        self.running = running
        self.queued = queued
        self.max_running = max_running
        self.max_queued = max_queued
