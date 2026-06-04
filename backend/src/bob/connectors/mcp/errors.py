"""Error taxonomy for the MCP connector.

Mirrors the tavily / gmail connector hierarchies: a single base
(:class:`MCPError`) with specific subclasses the sub-agent tool handler maps to
structured ``mcp_*`` error codes (mirrors ``web_search_*``). Classifying here —
at the protocol boundary — keeps the adapter handler a thin translator rather
than re-deriving "what went wrong" from raw transport exceptions.

A bare :class:`MCPError` is the catch-all (``mcp_tool_failed``). The subclasses
distinguish the three failure shapes the manager / adapter need to tell apart:

- :class:`MCPMissingServerError` — the named server is not configured /
  connected, so the call cannot even be attempted (``mcp_missing_server``).
- :class:`MCPUnreachableError` — the transport failed (subprocess died, HTTP
  timeout, connection reset) — transient, worth a retry (``mcp_unreachable``).
- :class:`MCPToolError` — the server ran the tool and returned ``isError`` /
  malformed content — the call reached the tool but the tool itself failed
  (``mcp_tool_error``).
"""

from __future__ import annotations


class MCPError(Exception):
    """Base class for every MCP connector failure.

    A bare ``MCPError`` (not one of the subclasses below) is the catch-all the
    adapter handler maps to ``mcp_tool_failed``: an unexpected protocol-level
    failure that does not fit the more specific classifications.
    """


class MCPMissingServerError(MCPError):
    """The requested MCP server is not configured / not connected.

    Raised before any I/O so the handler can tell the user the integration is
    not wired (or its server failed to boot) rather than reporting a generic
    tool failure.
    """


class MCPUnreachableError(MCPError):
    """The MCP transport failed — subprocess crash, HTTP timeout, reset.

    Transient by nature; the sub-agent maps this to a "réessaie dans un moment"
    style message.
    """


class MCPToolError(MCPError):
    """The server executed the tool but it reported a failure.

    Raised when ``CallToolResult.isError`` is set or the returned content is
    unusable — the call reached the tool, but the tool itself failed.
    """


__all__ = [
    "MCPError",
    "MCPMissingServerError",
    "MCPToolError",
    "MCPUnreachableError",
]
