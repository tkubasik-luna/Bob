"""Register configured MCP servers' discovered tools into a sub-agent registry.

The manifest-driven glue for issue 0094: for each configured
:class:`MCPManager`, connect it, discover its tools, apply curation (the
``expose`` allowlist + per-tool :class:`bob.connectors.mcp.models.MCPToolOverride`),
:func:`wrap` each survivor into a :class:`SubAgentToolDefinition`, and register
them so the slice is dispatchable through the existing dispatcher / projection
pipeline.

Boot invariant (mirrors the optional-``TAVILY_API_KEY`` pattern): a missing or
unreachable server registers **nothing** and never raises — :meth:`connect`
returns ``False`` / :meth:`list_tools` returns ``[]``, so the backend boots green
with no MCP server configured (empty manifest) or with a down server in the
manifest. :func:`register_mcp_tools` is the single-server primitive;
:func:`register_mcp_managers` drives the multi-server fleet.
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
    unreachable, exposes no tools, or every tool is filtered out by the
    ``expose`` allowlist).

    Curation precedence: an explicit ``curations`` entry (keyed by tool name)
    wins; otherwise the manager's manifest
    (:attr:`MCPServerConfig.tools` / :attr:`MCPServerConfig.expose`) supplies the
    per-tool override and the allowlist. Passing ``curations`` explicitly keeps
    the single-server primitive usable in tests / callers that build curation
    by hand without a full manifest.

    The ``expose`` allowlist on the server config is honoured *before* wrapping:
    a tool whose name is not listed is skipped entirely (never wrapped, never
    advertised), so a verbose upstream server only contributes its chosen subset.

    A tool whose name already exists in the registry is skipped with a warning
    rather than raising — a name collision with a hand-written tool (e.g. a
    server also exposing ``web_search``) must never crash the boot.
    """

    config = manager.config
    explicit_curations = curations or {}

    connected = await manager.connect()
    if not connected:
        return []

    tools = await manager.list_tools()
    registered: list[str] = []
    for tool in tools:
        name = getattr(tool, "name", None)
        if not isinstance(name, str) or not name:
            continue
        # ``expose`` allowlist — only listed tools are wrapped (issue 0094).
        if not config.is_exposed(name):
            _logger.debug("mcp.tool_not_exposed", server=manager.name, tool=name)
            continue
        if registry.get(name) is not None:
            _logger.warning(
                "mcp.tool_name_collision",
                server=manager.name,
                tool=name,
                hint="A tool with this name is already registered; skipping the MCP one.",
            )
            continue
        # Explicit curation wins, else fold the manifest's per-tool override.
        curation = explicit_curations.get(name) or MCPToolCuration.from_override(
            config.override_for(name)
        )
        try:
            definition = wrap(tool, manager, curation=curation)
        except Exception as exc:  # pragma: no cover — defensive on a malformed descriptor
            _logger.warning("mcp.wrap_failed", server=manager.name, tool=name, error=str(exc))
            continue
        registry.register(definition)
        registered.append(name)

    _logger.info("mcp.tools_registered", server=manager.name, count=len(registered))
    return registered


async def register_mcp_managers(
    managers: list[MCPManager],
    registry: SubAgentToolRegistry,
) -> dict[str, list[str]]:
    """Register every manager's exposed, curated tools into ``registry``.

    Drives :func:`register_mcp_tools` per server, applying each server's manifest
    curation (``expose`` + per-tool overrides). Returns a ``{server_name:
    [registered tool names]}`` map (a server that failed to connect or exposed
    nothing maps to ``[]``). Per-server gating: one down server registering
    nothing never stops the others — the loop swallows nothing it should not, but
    a server failing to connect is already handled inside
    :func:`register_mcp_tools` (returns ``[]``), so the whole boot stays green.
    """

    summary: dict[str, list[str]] = {}
    for manager in managers:
        summary[manager.name] = await register_mcp_tools(manager, registry)
    return summary


__all__ = ["register_mcp_managers", "register_mcp_tools"]
