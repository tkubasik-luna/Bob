"""Unit tests for :class:`bob.connectors.mcp.manager.MCPManager`.

The transport is mocked at the connector boundary via the ``session_factory``
seam (mirrors gmail ``service_factory`` / tavily ``client_factory``): a fake
async-context-manager yields a fake :class:`MCPSession` with canned
``list_tools`` / ``call_tool``. No real subprocess, no httpx, no ``mcp`` SDK
transport stack is exercised.

Locks the manager's contract:

- connect / list / call happy path returns the session's tools + result;
- a missing / unreachable server registers nothing and does NOT raise at boot
  (``connect`` returns ``False``, ``list_tools`` returns ``[]``) — the
  optional-``TAVILY_API_KEY`` invariant;
- an unreachable call surfaces a structured :class:`MCPUnreachableError`;
- a call before connect raises :class:`MCPMissingServerError`;
- ``aclose`` is idempotent and closes the underlying transport.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import timedelta
from typing import Any

import pytest

from bob.connectors.mcp import (
    MCPManager,
    MCPMissingServerError,
    MCPServerConfig,
    MCPUnreachableError,
)


class _FakeTool:
    def __init__(self, name: str) -> None:
        self.name = name
        self.description = f"desc {name}"
        self.inputSchema = {"type": "object", "properties": {}}


class _FakeListToolsResult:
    def __init__(self, tools: list[_FakeTool]) -> None:
        self.tools = tools


class _FakeSession:
    """Canned :class:`MCPSession` recording the calls it received."""

    def __init__(
        self,
        *,
        tools: list[_FakeTool] | None = None,
        call_result: Any = None,
        call_exc: Exception | None = None,
    ) -> None:
        self._tools = tools or []
        self._call_result = call_result
        self._call_exc = call_exc
        self.calls: list[tuple[str, dict[str, Any] | None, timedelta | None]] = []

    async def list_tools(self) -> _FakeListToolsResult:
        return _FakeListToolsResult(self._tools)

    async def call_tool(
        self,
        name: str,
        arguments: dict[str, Any] | None = None,
        read_timeout_seconds: timedelta | None = None,
    ) -> Any:
        self.calls.append((name, arguments, read_timeout_seconds))
        if self._call_exc is not None:
            raise self._call_exc
        return self._call_result


def _factory(session: _FakeSession, *, closed: list[bool] | None = None) -> Any:
    """Build a ``session_factory`` whose CM yields ``session``."""

    @asynccontextmanager
    async def _cm(_config: MCPServerConfig) -> Any:
        try:
            yield session
        finally:
            if closed is not None:
                closed.append(True)

    return lambda config: _cm(config)


def _failing_factory() -> Any:
    """A ``session_factory`` whose CM raises on enter (transport down)."""

    @asynccontextmanager
    async def _cm(_config: MCPServerConfig) -> Any:
        raise ConnectionError("transport refused")
        yield  # pragma: no cover — unreachable, makes this an async generator

    return lambda config: _cm(config)


_CONFIG = MCPServerConfig(name="demo", transport="stdio", command="demo-server")


async def test_connect_list_call_happy_path() -> None:
    session = _FakeSession(
        tools=[_FakeTool("alpha"), _FakeTool("beta")],
        call_result={"ok": True},
    )
    manager = MCPManager(_CONFIG, session_factory=_factory(session))

    assert await manager.connect() is True
    assert manager.connected is True

    tools = await manager.list_tools()
    assert [t.name for t in tools] == ["alpha", "beta"]

    result = await manager.call_tool("alpha", {"x": 1})
    assert result == {"ok": True}
    # The per-call timeout is forwarded to the session.
    assert session.calls[0][0] == "alpha"
    assert session.calls[0][1] == {"x": 1}
    assert isinstance(session.calls[0][2], timedelta)


async def test_connect_is_idempotent() -> None:
    session = _FakeSession(tools=[])
    manager = MCPManager(_CONFIG, session_factory=_factory(session))
    assert await manager.connect() is True
    # Second connect is a no-op success, not a re-enter.
    assert await manager.connect() is True


async def test_missing_server_registers_nothing_and_never_raises() -> None:
    """A transport failure on connect must NOT break the boot."""

    manager = MCPManager(_CONFIG, session_factory=_failing_factory())

    # connect() swallows the failure and reports it via the return value.
    assert await manager.connect() is False
    assert manager.connected is False
    # A dead server discovers no tools — registration adds nothing.
    assert await manager.list_tools() == []


async def test_call_before_connect_is_missing_server() -> None:
    manager = MCPManager(_CONFIG, session_factory=_failing_factory())
    assert await manager.connect() is False
    with pytest.raises(MCPMissingServerError):
        await manager.call_tool("alpha", {})


async def test_unreachable_call_surfaces_structured_error() -> None:
    session = _FakeSession(call_exc=TimeoutError("read timed out"))
    manager = MCPManager(_CONFIG, session_factory=_factory(session))
    assert await manager.connect() is True
    with pytest.raises(MCPUnreachableError) as excinfo:
        await manager.call_tool("alpha", {})
    assert "demo" in str(excinfo.value)


async def test_list_tools_failure_downgrades_to_empty() -> None:
    """A transport failure mid-discovery degrades to [] rather than raising."""

    class _BoomSession(_FakeSession):
        async def list_tools(self) -> Any:
            raise ConnectionResetError("reset")

    manager = MCPManager(_CONFIG, session_factory=_factory(_BoomSession()))
    assert await manager.connect() is True
    assert await manager.list_tools() == []


async def test_aclose_closes_transport_and_is_idempotent() -> None:
    closed: list[bool] = []
    session = _FakeSession(tools=[])
    manager = MCPManager(_CONFIG, session_factory=_factory(session, closed=closed))
    await manager.connect()
    await manager.aclose()
    assert closed == [True]
    assert manager.connected is False
    # Second close is a no-op (never connected again).
    await manager.aclose()
    assert closed == [True]
