"""Internal types shared between :mod:`bob.tools.registry` and definitions.

Kept in a leaf module to avoid circular imports between :class:`ToolDefinition`
(which references the handler signature) and the individual tool-definition
modules (which import :class:`ToolHandlerContext` from the dispatcher).
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal

if TYPE_CHECKING:  # pragma: no cover — typing-only.
    from pydantic import BaseModel

    from bob.tools.dispatcher import ToolHandlerContext


#: Status reported by a tool handler after running its side effect. A
#: handler that successfully created / resumed / cancelled a task returns
#: ``"ok"``; a handler that decided the call should be treated as an
#: error (e.g. ``forward_to_subtask`` against a task not in
#: ``waiting_input``) returns ``"error"`` so the dispatcher routes the
#: outcome through the same code path as unknown-tool / invalid-args.
ToolHandlerStatus = Literal["ok", "error"]


@dataclass(frozen=True)
class ToolHandlerOutcome:
    """Result of a tool handler invocation.

    Fields:

    - ``status`` — see :data:`ToolHandlerStatus`.
    - ``task_id`` — the task id touched by the call (created, resumed,
      cancelled). ``None`` for the unified ``say`` tool (issue 0047).
    - ``error_code`` — short machine-readable identifier for the error
      branch (``"unknown_task"``, ``"task_not_waiting_input"`` …). Mirrors
      the ``reason_code`` shape used elsewhere in PRD 0006.
    - ``error_message`` — optional human-readable explanation suitable
      for logs / debug events. Never surfaced to the user verbatim today
      (the orchestrator falls back to the structured-output path in v1).
    - ``speech`` — populated by the unified ``say`` tool (issue 0047)
      so the orchestrator can hand the spoken text straight to the
      :class:`OrchestratorResponse`. ``None`` for every other tool.
    - ``ui`` — optional structured UI payload accompanying ``speech``
      (also populated by ``say``). Free-form ``dict`` / ``list`` — the
      orchestrator normalises it into the existing :class:`ComponentDescriptor`
      list shape consumed by the WS router. ``None`` when ``say`` was
      called with ``ui=null`` or by every other tool.
    """

    status: ToolHandlerStatus
    task_id: str | None = None
    error_code: str | None = None
    error_message: str | None = None
    speech: str | None = None
    ui: Any | None = None


#: Async handler signature used by :class:`bob.tools.registry.ToolDefinition`.
#: Each tool's argument shape is its own Pydantic model; we forward it as
#: the generic :class:`pydantic.BaseModel` parent so the registry stays
#: covariant on the model type.
ToolHandler = Callable[["ToolHandlerContext", "BaseModel"], Awaitable[ToolHandlerOutcome]]
