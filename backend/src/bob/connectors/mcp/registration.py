"""Register one MCP server's discovered tools into a sub-agent registry.

The end-to-end glue for issue 0093: connect a configured :class:`MCPManager`,
discover its tools, :func:`wrap` each into a :class:`SubAgentToolDefinition`,
and register them so the slice is dispatchable through the existing
dispatcher / projection pipeline.

Boot invariant (mirrors the optional-``TAVILY_API_KEY`` pattern): a missing or
unreachable server registers **nothing** and never raises — :meth:`connect`
returns ``False`` / :meth:`list_tools` returns ``[]``, so the backend boots
green with no MCP server configured. The multi-server manifest + startup /
shutdown lifecycle wiring is issue 0094; this helper is the single-server
primitive that wiring will drive.
"""

from __future__ import annotations

import structlog

from bob.connectors.mcp.adapter import MCPToolCuration, wrap
from bob.connectors.mcp.manager import MCPManager
from bob.sub_agent.tool_registry import SubAgentToolRegistry

_logger = structlog.get_logger(__name__)


async def register_mcp_tools(
    manager: MCPManager,
    registry: SubAgentToolRegistry,
    *,
    curations: dict[str, MCPToolCuration] | None = None,
) -> list[str]:
    """Connect ``manager``, discover its tools, and register them into ``registry``.

    Returns the list of registered tool names (empty when the server is
    unreachable or exposes no tools). Per-tool ``curations`` (keyed by tool
    name) override the description / argument subset; uncurated tools get the
    raw schema + the generic Markdown projector.

    A tool whose name already exists in the registry is skipped with a warning
    rather than raising — a name collision with a hand-written tool (e.g. a
    server also exposing ``web_search``) must never crash the boot.
    """

    curations = curations or {}

    connected = await manager.connect()
    if not connected:
        return []

    tools = await manager.list_tools()
    registered: list[str] = []
    for tool in tools:
        name = getattr(tool, "name", None)
        if not isinstance(name, str) or not name:
            continue
        if registry.get(name) is not None:
            _logger.warning(
                "mcp.tool_name_collision",
                server=manager.name,
                tool=name,
                hint="A tool with this name is already registered; skipping the MCP one.",
            )
            continue
        try:
            definition = wrap(tool, manager, curation=curations.get(name))
        except Exception as exc:  # pragma: no cover — defensive on a malformed descriptor
            _logger.warning("mcp.wrap_failed", server=manager.name, tool=name, error=str(exc))
            continue
        registry.register(definition)
        registered.append(name)

    _logger.info("mcp.tools_registered", server=manager.name, count=len(registered))
    return registered


__all__ = ["register_mcp_tools"]
