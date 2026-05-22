"""Shared data types for the unified tool-calling API.

Used by :meth:`bob.llm_client.LLMClient.complete` so callers can pass tool
definitions and consume tool calls without caring whether the backend is
LM Studio (OpenAI-compatible function calling) or the Claude CLI
(JSON-in-system-prompt protocol).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class ToolDefinition:
    """Description of a tool the model is allowed to call.

    ``parameters`` is a JSON Schema object (the same shape OpenAI uses for the
    ``function.parameters`` field).
    """

    name: str
    description: str
    parameters: dict[str, Any]


@dataclass(frozen=True)
class ToolCall:
    """A single tool invocation emitted by the model.

    ``id`` is the provider-assigned call id. Bob uses it to route tool results
    back into the next turn. When the underlying provider does not supply an
    id (Claude CLI is a JSON-only protocol), the client generates one.
    """

    id: str
    name: str
    arguments: dict[str, Any]


@dataclass(frozen=True)
class LLMResponse:
    """Outcome of an :meth:`LLMClient.complete` call.

    Exactly one of ``text`` and ``tool_calls`` is meaningful per call:

    - ``text != None`` and ``tool_calls == []`` → the model answered with plain
      text.
    - ``text is None`` and ``tool_calls`` non-empty → the model wants to invoke
      one or more tools.
    """

    text: str | None
    tool_calls: list[ToolCall]

    @property
    def is_tool_call(self) -> bool:
        return bool(self.tool_calls)
