"""Tool-calling codec layer (PRD 0008 / issue 0058).

"Core owns the loop, codec owns the format." This package introduces the seam
that lets every tool-calling call site speak in one canonical
:class:`ToolSpec` and delegate all wire-format concerns to a
:class:`ToolCodec` selected by :func:`select_codec` from a
:class:`BackendCapability`.

Issue 0058 shipped the native function-calling codec
(:class:`NativeToolCodec`), extracted behaviour-identically from
:class:`bob.llm_client.LMStudioClient`. Issue 0061 adds
:class:`HermesToolCodec` — the Nous-Hermes ``<tool_call>`` tag format that
backs the Claude CLI (and a future Hermes/vLLM endpoint). Guided-JSON (0060)
implements the same protocol later; the selection logic already names it as a
reachable extension point.
"""

from __future__ import annotations

from bob.llm.tooling.capability import (
    BackendCapability,
    CodecNotAvailableError,
    ToolMode,
    capability_for_backend,
    select_codec,
)
from bob.llm.tooling.codec import (
    NativeToolCallParseError,
    NativeToolCodec,
    ToolCallStreamParser,
    ToolCodec,
)
from bob.llm.tooling.hermes import HermesToolCodec
from bob.llm.tooling.schema import flatten_schema, order_specs
from bob.llm.tooling.spec import ToolSpec

__all__ = [
    "BackendCapability",
    "CodecNotAvailableError",
    "HermesToolCodec",
    "NativeToolCallParseError",
    "NativeToolCodec",
    "ToolCallStreamParser",
    "ToolCodec",
    "ToolMode",
    "ToolSpec",
    "capability_for_backend",
    "flatten_schema",
    "order_specs",
    "select_codec",
]
