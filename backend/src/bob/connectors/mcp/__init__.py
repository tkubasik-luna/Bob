"""MCP connector package — Bob as a Model Context Protocol *client* (PRD 0015).

Lets Bob branch an external capability by connecting to an MCP server and
wrapping its discovered tools as native sub-agent tools, with **no per-tool
connector code**. The LLM never speaks MCP — it is strictly a backend↔tool-server
protocol, so everything works identically on full LM Studio.

- :mod:`bob.connectors.mcp.manager` — :class:`MCPManager`, the session
  lifecycle owner (stdio subprocess / streamable-HTTP), with the transport as
  the test mock seam (mirrors gmail ``service_factory`` / tavily
  ``client_factory``).
- :mod:`bob.connectors.mcp.adapter` — :func:`wrap`, turning a discovered MCP
  tool into a :class:`SubAgentToolDefinition` (dynamic ``args_model`` from the
  tool's JSON Schema; handler folds the result + ``mcp_*`` errors).
- :mod:`bob.connectors.mcp.projector` — :func:`project_mcp_default`, the generic
  text→Markdown-card projection reused by every uncurated MCP tool.
- :mod:`bob.connectors.mcp.registration` — :func:`register_mcp_tools`, the
  single-server end-to-end glue.
- :mod:`bob.connectors.mcp.errors` — the ``mcp_*`` failure taxonomy (mirrors
  ``web_search_*``).

The package is independent of :mod:`bob.tools` and :mod:`bob.ui_registry`;
wiring happens in the sub-agent tool layer, not here (mirrors the gmail / tavily
connector boundary).
"""

from __future__ import annotations

from bob.connectors.mcp.adapter import MCPToolCuration, wrap
from bob.connectors.mcp.errors import (
    MCPError,
    MCPMissingServerError,
    MCPToolError,
    MCPUnreachableError,
)
from bob.connectors.mcp.manager import MCPManager, MCPSession, SessionFactory
from bob.connectors.mcp.models import MCPServerConfig, MCPTransport, extract_text_content
from bob.connectors.mcp.projector import project_mcp_default
from bob.connectors.mcp.registration import register_mcp_tools

__all__ = [
    "MCPError",
    "MCPManager",
    "MCPMissingServerError",
    "MCPServerConfig",
    "MCPSession",
    "MCPToolCuration",
    "MCPToolError",
    "MCPTransport",
    "MCPUnreachableError",
    "SessionFactory",
    "extract_text_content",
    "project_mcp_default",
    "register_mcp_tools",
    "wrap",
]
