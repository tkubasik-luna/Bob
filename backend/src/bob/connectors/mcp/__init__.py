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
- :mod:`bob.connectors.mcp.registration` — :func:`register_mcp_tools`
  (single-server) + :func:`register_mcp_managers` (multi-server fleet) glue.
- :mod:`bob.connectors.mcp.lifecycle` — :class:`MCPRuntime`, the
  FastAPI-lifespan-facing owner of the configured fleet (connect-at-startup /
  close-at-shutdown), built from the parsed ``mcp_servers`` manifest.
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
from bob.connectors.mcp.lifecycle import MCPRuntime
from bob.connectors.mcp.manager import MCPManager, MCPSession, SessionFactory
from bob.connectors.mcp.models import (
    MCPServerConfig,
    MCPToolOverride,
    MCPTransport,
    extract_text_content,
    parse_mcp_servers,
)
from bob.connectors.mcp.projector import make_mcp_projector, project_mcp_default
from bob.connectors.mcp.registration import register_mcp_managers, register_mcp_tools

__all__ = [
    "MCPError",
    "MCPManager",
    "MCPMissingServerError",
    "MCPRuntime",
    "MCPServerConfig",
    "MCPSession",
    "MCPToolCuration",
    "MCPToolError",
    "MCPToolOverride",
    "MCPTransport",
    "MCPUnreachableError",
    "SessionFactory",
    "extract_text_content",
    "make_mcp_projector",
    "parse_mcp_servers",
    "project_mcp_default",
    "register_mcp_managers",
    "register_mcp_tools",
    "wrap",
]
