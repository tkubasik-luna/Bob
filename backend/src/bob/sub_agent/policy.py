"""`:class:`SubAgentPolicy` — centralised caps for the sub-agent runner.

PRD 0006 / issue 0045 mandates that every magic number controlling a
sub-agent's runtime budget surfaces as a named field on a single config
object. This file is that one-stop dial.

Three global caps are enforced by the runner:

- ``max_iterations``: the runner exits with ``done(degraded,
  iteration_cap)`` after this many ``progress`` + ``tool_call``
  iterations. Defaults to ``10`` (mirrors the legacy
  ``MAX_PROGRESS_ITERATIONS`` constant).
- ``wall_clock_seconds``: total wall-clock budget for a single
  :meth:`SubAgentRunner.run` invocation. Exceeding it triggers a
  cooperative cancel; the runner emits ``done(timeout,
  wall_clock_cap)``. Defaults to ``120.0``.
- ``token_cap``: aggregate token spend across LLM calls inside a single
  run. The runner adds the prompt + completion token counts from each
  LLM call and exits with ``done(degraded, token_cap)`` once the cap
  is exceeded. Defaults to ``8000``.

Per-task-type overrides
-----------------------

Some sub-agent task types want very different budgets (e.g. a quick
``memory_extraction`` task should not get 120 s, a long ``research``
task wants more). The :attr:`per_task_type` mapping carries a partial
override dict keyed by ``task_type``. The runner reads
``policy.for_task_type(task_type)`` which returns a fully-resolved
:class:`SubAgentPolicy` merging the global defaults with the overrides.

The ``task_type`` field on the task itself is not wired in this slice
(the task row only carries ``title`` + ``goal`` today). 0050 adds it.
Until then ``for_task_type(None)`` returns the global policy.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from typing import Any


@dataclass(frozen=True)
class SubAgentPolicy:
    """Immutable budget config consumed by :class:`SubAgentRunner`.

    Construct once at app boot via :func:`default_policy` (or in tests
    with explicit fields) and pass to the runner. Tests overriding one
    field use :meth:`replace` to keep the rest at default.

    ``cancel_grace_seconds`` governs the cooperative cancellation
    window: when the runner is asked to cancel it has this many seconds
    to reach the next checkpoint cleanly. Past that it is hard-killed
    via :meth:`asyncio.Task.cancel` while the runner is mid-await.
    Default ``2.0`` matches the PRD spec.
    """

    max_iterations: int = 10
    wall_clock_seconds: float = 120.0
    token_cap: int = 8000
    cancel_grace_seconds: float = 2.0
    per_task_type: Mapping[str, Mapping[str, Any]] = field(default_factory=dict)

    def for_task_type(self, task_type: str | None) -> SubAgentPolicy:
        """Resolve the effective policy for ``task_type``.

        Returns ``self`` when ``task_type`` is None or no override is
        registered. Overrides merge as a shallow dict: each field
        present in the override replaces the global default.
        """

        if task_type is None:
            return self
        override = self.per_task_type.get(task_type)
        if not override:
            return self
        allowed = {
            "max_iterations",
            "wall_clock_seconds",
            "token_cap",
            "cancel_grace_seconds",
        }
        return replace(
            self,
            **{key: value for key, value in override.items() if key in allowed},
        )


def default_policy() -> SubAgentPolicy:
    """Return the process-wide default :class:`SubAgentPolicy`.

    Centralised so the orchestrator boot and the test harness both
    construct the runner with the same baseline; tweaking the dials in
    one place exercises every call site.
    """

    return SubAgentPolicy()


__all__ = ["SubAgentPolicy", "default_policy"]
