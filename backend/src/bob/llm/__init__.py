"""Public LLM-layer types and helpers.

Re-exports the shared tool-calling data classes used by callers that want to
talk to any :class:`bob.llm_client.LLMClient` implementation via the
:meth:`bob.llm_client.LLMClient.complete` API.
"""

from __future__ import annotations

from bob.llm.types import LLMResponse, StreamChunk, StreamChunkKind, ToolCall, ToolDefinition

__all__ = ["LLMResponse", "StreamChunk", "StreamChunkKind", "ToolCall", "ToolDefinition"]
