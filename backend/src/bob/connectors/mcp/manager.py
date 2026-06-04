"""MCP session manager — owns the lifecycle of one configured MCP server.

Bob acts as an MCP *client*. :class:`MCPManager` connects to a server (local
subprocess over stdio, or remote over streamable-HTTP), discovers its tools
(:meth:`list_tools`), and runs them (:meth:`call_tool`) with a per-call timeout.

Two invariants borrowed from the optional-``TAVILY_API_KEY`` pattern:

- a missing / unreachable server **never breaks the boot** —
  :meth:`connect` swallows transport failures, logs an actionable message, and
  leaves the manager in a "not connected" state where :meth:`list_tools`
  returns ``[]`` (so registration adds nothing) and :meth:`call_tool` raises a
  structured :class:`MCPMissingServerError`;
- every failure mode crosses the boundary as the :mod:`bob.connectors.mcp.errors`
  taxonomy, never a raw transport exception, so the adapter handler stays a thin
  translator to ``mcp_*`` codes.

The **transport is the test mock seam** (mirrors Gmail's ``service_factory`` /
Tavily's ``client_factory``): a :data:`SessionFactory` is an async context
manager yielding an :class:`MCPSession`-shaped object. Production builds it from
the ``mcp`` SDK's ``stdio_client`` / ``streamablehttp_client``; tests inject a
factory yielding a fake session with canned ``list_tools`` / ``call_tool``.
"""

from __future__ import annotations

from collections.abc import Callable
from contextlib import AbstractAsyncContextManager, AsyncExitStack
from datetime import timedelta
from typing import Any, Protocol

import structlog

from bob.connectors.mcp.errors import (
    MCPError,
    MCPMissingServerError,
    MCPToolError,
    MCPUnreachableError,
)
from bob.connectors.mcp.models import MCPServerConfig

_logger = structlog.get_logger(__name__)


class MCPSession(Protocol):
    """The subset of ``mcp.ClientSession`` the manager depends on.

    Declared as a Protocol so the test fake never has to subclass the SDK
    session — it just needs ``list_tools`` and ``call_tool`` with these shapes
    (both already match ``mcp.ClientSession``).
    """

    async def list_tools(self) -> Any: ...

    async def call_tool(
        self,
        name: str,
        arguments: dict[str, Any] | None = None,
        read_timeout_seconds: timedelta | None = None,
    ) -> Any: ...


SessionFactory = Callable[[MCPServerConfig], AbstractAsyncContextManager[MCPSession]]
"""Builds the connected :class:`MCPSession` for a server, as an async CM.

Production uses :func:`_default_session_factory` (stdio subprocess or
streamable-HTTP, then an initialised ``mcp.ClientSession``). Tests pass a
factory whose context manager yields a fake session so no real transport,
subprocess, or network is touched (mirrors the gmail ``service_factory`` and
tavily ``client_factory`` seams).
"""


class MCPManager:
    """Lifecycle owner for one configured MCP server session.

    Single-server, minimal-config by design (issue 0093). The full multi-server
    manifest + startup/shutdown wiring is issue 0094; this manager exposes the
    primitives that wiring will drive: :meth:`connect`, :meth:`list_tools`,
    :meth:`call_tool`, :meth:`aclose`.
    """

    def __init__(
        self,
        config: MCPServerConfig,
        *,
        call_timeout_seconds: float = 30.0,
        session_factory: SessionFactory | None = None,
    ) -> None:
        self._config = config
        self._call_timeout = timedelta(seconds=call_timeout_seconds)
        self._session_factory = session_factory or _default_session_factory
        self._exit_stack: AsyncExitStack | None = None
        self._session: MCPSession | None = None

    @property
    def name(self) -> str:
        return self._config.name

    @property
    def connected(self) -> bool:
        return self._session is not None

    async def connect(self) -> bool:
        """Open the session; return ``True`` on success, ``False`` on failure.

        NEVER raises: a transport failure (subprocess missing, HTTP refused,
        handshake error) is logged with an actionable message and the manager
        stays disconnected — the optional-``TAVILY_API_KEY`` invariant. The
        boot path calls this and proceeds regardless; :meth:`list_tools` then
        returns ``[]`` so a dead server registers no tools.
        """

        if self._session is not None:
            return True

        stack = AsyncExitStack()
        try:
            session = await stack.enter_async_context(self._session_factory(self._config))
        except Exception as exc:
            await stack.aclose()
            _logger.warning(
                "mcp.connect_failed",
                server=self._config.name,
                transport=self._config.transport,
                error=str(exc),
                hint=(
                    "MCP server unreachable — check the command/url in its config; "
                    "no tools will be registered for it."
                ),
            )
            return False

        self._exit_stack = stack
        self._session = session
        _logger.info("mcp.connected", server=self._config.name, transport=self._config.transport)
        return True

    async def list_tools(self) -> list[Any]:
        """Discover the server's tools; return ``[]`` when not connected.

        Returns the raw ``mcp.types.Tool`` descriptors (the adapter turns each
        into a :class:`SubAgentToolDefinition`). A not-connected manager returns
        an empty list — the boot path never registers tools for a dead server.
        A transport failure mid-discovery is logged and downgraded to ``[]`` for
        the same reason.
        """

        if self._session is None:
            return []
        try:
            result = await self._session.list_tools()
        except Exception as exc:
            _logger.warning("mcp.list_tools_failed", server=self._config.name, error=str(exc))
            return []
        tools = getattr(result, "tools", None)
        return list(tools) if tools else []

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> Any:
        """Invoke ``name`` with ``arguments`` and return the ``CallToolResult``.

        Failure taxonomy (all raised, never returned):

        - :class:`MCPMissingServerError` — the manager is not connected (the
          server failed to boot or was never wired);
        - :class:`MCPUnreachableError` — the call raised a transport exception
          (subprocess died, timeout, connection reset);
        - the ``isError`` classification is left to the adapter handler, which
          owns the result→outcome folding (it inspects ``CallToolResult.isError``
          via :func:`extract_text_content`).
        """

        if self._session is None:
            raise MCPMissingServerError(
                f"MCP server '{self._config.name}' is not connected; cannot call '{name}'."
            )
        try:
            return await self._session.call_tool(
                name,
                arguments,
                read_timeout_seconds=self._call_timeout,
            )
        except (MCPError, MCPToolError):
            # A fake/test session may already speak the taxonomy — pass through.
            raise
        except Exception as exc:
            _logger.warning("mcp.call_failed", server=self._config.name, tool=name, error=str(exc))
            raise MCPUnreachableError(
                f"MCP call to '{name}' on '{self._config.name}' failed: {exc}"
            ) from exc

    async def aclose(self) -> None:
        """Close the session + transport. Idempotent; safe when never connected."""

        stack = self._exit_stack
        self._exit_stack = None
        self._session = None
        if stack is not None:
            try:
                await stack.aclose()
            except Exception as exc:  # pragma: no cover — defensive on teardown
                _logger.warning("mcp.close_failed", server=self._config.name, error=str(exc))


def _default_session_factory(
    config: MCPServerConfig,
) -> AbstractAsyncContextManager[MCPSession]:
    """Build the production transport + initialised ``ClientSession`` for ``config``.

    Returns an async context manager that, on enter, opens the transport
    (stdio subprocess or streamable-HTTP), constructs an ``mcp.ClientSession``
    over its streams, runs the MCP ``initialize`` handshake, and yields the
    session. Imported lazily so this module's import never pulls in the ``mcp``
    SDK transport stack for unrelated unit tests (the gmail/tavily pattern).
    """

    from contextlib import asynccontextmanager

    @asynccontextmanager
    async def _open() -> Any:
        from mcp import ClientSession

        async with AsyncExitStack() as stack:
            if config.transport == "stdio":
                if not config.command:
                    raise MCPMissingServerError(
                        f"MCP server '{config.name}' has transport=stdio but no command."
                    )
                from mcp import StdioServerParameters, stdio_client

                params = StdioServerParameters(
                    command=config.command,
                    args=list(config.args),
                    env=config.env,
                )
                read, write = await stack.enter_async_context(stdio_client(params))
            elif config.transport == "http":
                if not config.url:
                    raise MCPMissingServerError(
                        f"MCP server '{config.name}' has transport=http but no url."
                    )
                from mcp.client.streamable_http import streamablehttp_client

                read, write, _ = await stack.enter_async_context(streamablehttp_client(config.url))
            else:  # pragma: no cover — guarded by the Literal type
                raise MCPMissingServerError(
                    f"MCP server '{config.name}' has unknown transport '{config.transport}'."
                )

            session = await stack.enter_async_context(ClientSession(read, write))
            await session.initialize()
            yield session

    return _open()


__all__ = [
    "MCPManager",
    "MCPSession",
    "SessionFactory",
]
