"""Sub-agent execution layer — PRD 0006 / issue 0045.

Public surface other slices depend on:

- :class:`SubAgentRunner` — re-export from :mod:`.runner`.
- :class:`SubAgentPolicy` + :func:`default_policy` — re-export from
  :mod:`.policy`. Centralises max iterations, wall-clock budget,
  per-call token cap, cancel grace, and per-task-type overrides.
- :class:`AddendumQueue` + :class:`AddendumEntry` — re-export from
  :mod:`.addendum_queue`. Per-task ``asyncio.Queue`` drained only at
  iteration boundaries (0050 fills the producer side).
- :class:`SubAgentToolRegistry`, :class:`SubAgentToolDefinition`,
  :class:`SubAgentToolDispatcher`, :class:`SubAgentToolDispatchResult`,
  :class:`SubAgentToolArgsValidationError`,
  :func:`build_default_subagent_registry` — re-exports from
  :mod:`.tool_registry`. Disjoint from :mod:`bob.tools` (Jarvis-side
  registry). :meth:`SubAgentToolDefinition.to_spec` (issue 0059) projects a
  tool to the canonical :class:`bob.llm.tooling.ToolSpec`.
- :class:`SubAgentAction`, :class:`ProgressAction`,
  :class:`ToolCallAction`, :class:`DoneAction`,
  :data:`SUB_AGENT_SCHEMA_VERSION`, :func:`parse_action`,
  :class:`SubAgentActionParseError` — re-exports from :mod:`.actions`.
"""

from __future__ import annotations

from bob.sub_agent.actions import (
    SUB_AGENT_SCHEMA_VERSION,
    DoneAction,
    ProgressAction,
    SubAgentAction,
    SubAgentActionParseError,
    SubAgentDoneStatus,
    ToolCallAction,
    parse_action,
)
from bob.sub_agent.addendum_queue import AddendumEntry, AddendumQueue
from bob.sub_agent.policy import SubAgentPolicy, default_policy
from bob.sub_agent.runner import (
    REASON_HARD_KILLED,
    REASON_INVALID_OUTPUT,
    REASON_ITERATION_CAP,
    REASON_LLM_FAILED,
    REASON_OK,
    REASON_STALLED,
    REASON_TOKEN_CAP,
    REASON_TOOL_FAILED,
    REASON_USER_CANCELLED,
    REASON_WALL_CLOCK_CAP,
    SubAgentRunner,
)
from bob.sub_agent.tool_registry import (
    GmailSearchArgs,
    SubAgentToolArgsValidationError,
    SubAgentToolDefinition,
    SubAgentToolDispatcher,
    SubAgentToolDispatchResult,
    SubAgentToolHandlerContext,
    SubAgentToolHandlerOutcome,
    SubAgentToolRegistry,
    WebFetchArgs,
    WebSearchArgs,
    build_default_subagent_registry,
    build_gmail_search_tool,
    build_web_fetch_tool,
    build_web_search_tool,
)

__all__ = [
    "REASON_HARD_KILLED",
    "REASON_INVALID_OUTPUT",
    "REASON_ITERATION_CAP",
    "REASON_LLM_FAILED",
    "REASON_OK",
    "REASON_STALLED",
    "REASON_TOKEN_CAP",
    "REASON_TOOL_FAILED",
    "REASON_USER_CANCELLED",
    "REASON_WALL_CLOCK_CAP",
    "SUB_AGENT_SCHEMA_VERSION",
    "AddendumEntry",
    "AddendumQueue",
    "DoneAction",
    "GmailSearchArgs",
    "ProgressAction",
    "SubAgentAction",
    "SubAgentActionParseError",
    "SubAgentDoneStatus",
    "SubAgentPolicy",
    "SubAgentRunner",
    "SubAgentToolArgsValidationError",
    "SubAgentToolDefinition",
    "SubAgentToolDispatchResult",
    "SubAgentToolDispatcher",
    "SubAgentToolHandlerContext",
    "SubAgentToolHandlerOutcome",
    "SubAgentToolRegistry",
    "ToolCallAction",
    "WebFetchArgs",
    "WebSearchArgs",
    "build_default_subagent_registry",
    "build_gmail_search_tool",
    "build_web_fetch_tool",
    "build_web_search_tool",
    "default_policy",
    "parse_action",
]
