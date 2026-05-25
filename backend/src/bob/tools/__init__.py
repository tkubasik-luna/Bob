"""Tool registry + dispatcher for the Jarvis-side tool surface (PRD 0006).

Issue 0044 replaces the hardcoded ``spawn_subtask`` / ``forward_to_subtask`` /
``cancel_subtask`` ``elif`` chain in :mod:`bob.orchestrator` with a versioned
:class:`ToolDefinition` registry and a single :class:`ToolDispatcher`. Every
dispatch validates the argument shape with Pydantic before invoking the
handler, returns a structured :class:`DispatchResult`, and emits a
``jarvis.route`` structured debug event so future "why did Jarvis chat
instead of spawning?" introspection is grep-friendly.

Public surface used by the orchestrator and by later slices (0045 sub-agent
contract rewrite, 0047 unified ``say`` tool, 0048 retry policy, 0050
``replan_task``):

- :class:`ToolDefinition` — name + version + ``args_model`` (Pydantic v2) +
  async handler closure.
- :class:`ToolRegistry` — collection lookup by ``name``. Construction is
  centralised in :func:`build_default_registry` so the orchestrator boots
  with a known-good set of tools.
- :class:`ToolDispatcher` — orchestrator-facing entry point. ``dispatch``
  takes a :class:`bob.llm.types.ToolCall` and returns a
  :class:`DispatchResult`. Unknown tool name and Pydantic validation
  failures both surface as ``DispatchResult(outcome="error", ...)`` — the
  orchestrator currently surfaces those the same way the legacy code did
  (turn falls through to the structured-output path). Retry/degrade
  behavior on validation errors ships in 0048 (PRD 0006).
- :class:`ToolHandlerContext` — small DI bag handed to every tool handler
  so individual tool files do not depend on the orchestrator import graph.
- :data:`JARVIS_ROUTE_EVENT_SOURCE` — canonical source string for the
  ``jarvis.route`` debug events emitted by the dispatcher.
"""

from __future__ import annotations

from bob.tools.dispatcher import (
    JARVIS_ROUTE_EVENT_SOURCE,
    DispatchOutcome,
    DispatchResult,
    ToolDispatcher,
    ToolHandlerContext,
)
from bob.tools.registry import (
    ToolArgsValidationError,
    ToolDefinition,
    ToolRegistry,
    build_default_registry,
)

__all__ = [
    "JARVIS_ROUTE_EVENT_SOURCE",
    "DispatchOutcome",
    "DispatchResult",
    "ToolArgsValidationError",
    "ToolDefinition",
    "ToolDispatcher",
    "ToolHandlerContext",
    "ToolRegistry",
    "build_default_registry",
]
