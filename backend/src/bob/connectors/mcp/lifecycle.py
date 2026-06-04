"""MCP runtime — connect the configured fleet at boot, close it at shutdown.

:class:`MCPRuntime` is the FastAPI-lifespan-facing object (issue 0094). It owns
one :class:`MCPManager` per configured server, drives connect + curation +
registration at startup (:meth:`startup`), and closes every session at shutdown
(:meth:`aclose`) so no zombie subprocess survives the process.

Boot invariant (mirrors the optional-``TAVILY_API_KEY`` pattern): an empty
manifest builds an empty runtime — :meth:`startup` registers nothing and the
backend boots green. A server that fails to connect logs an actionable line and
registers nothing while its peers register normally (per-server gating).

The ``session_factory`` is threaded through to every manager so a test can inject
a fake transport for the whole fleet (mirrors the per-manager mock seam) without
touching a real subprocess or network.
"""

from __future__ import annotations

import structlog

from bob.connectors.mcp.manager import MCPManager, SessionFactory
from bob.connectors.mcp.models import MCPServerConfig
from bob.connectors.mcp.registration import register_mcp_managers
from bob.sub_agent.tool_registry import SubAgentToolRegistry

_logger = structlog.get_logger(__name__)


class MCPRuntime:
    """Owns the configured MCP server fleet for one process lifetime.

    Construct from the parsed ``mcp_servers`` manifest, then call
    :meth:`startup` once (registers tools into the supplied registry) and
    :meth:`aclose` once at shutdown (closes every session). Both are safe with an
    empty manifest.
    """

    def __init__(
        self,
        servers: tuple[MCPServerConfig, ...] | list[MCPServerConfig],
        *,
        call_timeout_seconds: float = 30.0,
        session_factory: SessionFactory | None = None,
    ) -> None:
        self._managers: list[MCPManager] = [
            MCPManager(
                config,
                call_timeout_seconds=call_timeout_seconds,
                session_factory=session_factory,
            )
            for config in servers
        ]

    @property
    def managers(self) -> list[MCPManager]:
        return list(self._managers)

    async def startup(self, registry: SubAgentToolRegistry) -> dict[str, list[str]]:
        """Connect every server and register its exposed, curated tools.

        Returns a ``{server_name: [registered tool names]}`` map. Never raises:
        a down server registers ``[]`` (logged actionably inside the manager) and
        its peers register normally — the backend boots green either way. An
        empty manifest returns ``{}`` and registers nothing.
        """

        if not self._managers:
            _logger.info("mcp.runtime.no_servers")
            return {}

        summary = await register_mcp_managers(self._managers, registry)
        total = sum(len(names) for names in summary.values())
        _logger.info(
            "mcp.runtime.started",
            servers=len(self._managers),
            tools_registered=total,
            per_server={name: len(names) for name, names in summary.items()},
        )
        return summary

    async def aclose(self) -> None:
        """Close every manager's session + transport — no zombie subprocesses.

        Idempotent and best-effort: each :meth:`MCPManager.aclose` swallows its
        own teardown errors, so one stuck server never blocks the others or the
        shutdown path.
        """

        for manager in self._managers:
            await manager.aclose()
        if self._managers:
            _logger.info("mcp.runtime.closed", servers=len(self._managers))


__all__ = ["MCPRuntime"]
