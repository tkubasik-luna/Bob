"""Real-transport integration tests for the MCP connector (P0).

Every *unit* test injects a fake ``session_factory``, so the production
``_default_session_factory`` path — real stdio subprocess, real
``mcp.ClientSession``, real ``initialize`` handshake, real ``list_tools`` /
``call_tool`` round-trips — is otherwise never exercised. These tests close that
gap by spawning the tiny ``_echo_server.py`` FastMCP server as an actual
subprocess and driving it through the public :class:`MCPManager` /
:class:`MCPRuntime` surface with NO mock seam.

``asyncio_mode = "auto"`` (pyproject) runs the ``async def test_*`` functions
without a per-test marker. They fork a Python subprocess and rely on the real
``mcp`` SDK transport stack.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any, ClassVar

from bob.connectors.mcp import (
    MCPManager,
    MCPServerConfig,
    extract_text_content,
)
from bob.connectors.mcp.lifecycle import MCPRuntime
from bob.sub_agent.tool_registry import (
    SubAgentToolDispatcher,
    SubAgentToolRegistry,
)

_SERVER = str(Path(__file__).parent / "_echo_server.py")


class _StubContext:
    task_id = "task-transport-test"
    state: ClassVar[dict[str, Any]] = {}


def _config(**overrides: Any) -> MCPServerConfig:
    """A stdio config that boots the in-repo echo server via this interpreter."""

    base: dict[str, Any] = dict(
        name="echo",
        transport="stdio",
        command=sys.executable,
        args=(_SERVER,),
    )
    base.update(overrides)
    return MCPServerConfig(**base)


class TestRealTransportManager:
    """MCPManager against a real stdio subprocess — no session_factory."""

    async def test_connect_lists_real_tools(self) -> None:
        manager = MCPManager(_config())
        try:
            assert await manager.connect() is True
            assert manager.connected is True
            names = {t.name for t in await manager.list_tools()}
            assert {"echo", "boom"} <= names
        finally:
            await manager.aclose()
        assert manager.connected is False

    async def test_call_real_tool_round_trip(self) -> None:
        manager = MCPManager(_config())
        try:
            assert await manager.connect() is True
            result = await manager.call_tool("echo", {"text": "hi"})
            text, is_error = extract_text_content(result)
            assert is_error is False
            assert "echo: hi" in text
        finally:
            await manager.aclose()

    async def test_real_tool_error_sets_is_error(self) -> None:
        manager = MCPManager(_config())
        try:
            assert await manager.connect() is True
            result = await manager.call_tool("boom", {})
            _text, is_error = extract_text_content(result)
            assert is_error is True
        finally:
            await manager.aclose()

    async def test_missing_binary_connect_returns_false(self) -> None:
        """A bad command must NOT raise at boot — the optional-key invariant."""

        manager = MCPManager(_config(command="definitely-not-a-real-binary-xyz"))
        try:
            assert await manager.connect() is False
            assert manager.connected is False
            assert await manager.list_tools() == []
        finally:
            await manager.aclose()


class TestRealTransportRuntime:
    """MCPRuntime startup → register → dispatch → shutdown, real subprocess."""

    async def test_startup_registers_exposed_curated_tool(self) -> None:
        registry = SubAgentToolRegistry()
        runtime = MCPRuntime([_config(expose=("echo",))])
        try:
            summary = await runtime.startup(registry)
            assert summary == {"echo": ["echo"]}
            # expose=("echo",) drops "boom" — only the allowlisted tool registers.
            assert registry.get("echo") is not None
            assert registry.get("boom") is None
        finally:
            await runtime.aclose()

    async def test_registered_tool_dispatches_end_to_end(self) -> None:
        registry = SubAgentToolRegistry()
        runtime = MCPRuntime([_config(expose=("echo",))])
        try:
            await runtime.startup(registry)
            tool = registry.get("echo")
            assert tool is not None
            dispatcher = SubAgentToolDispatcher(registry)
            result = await dispatcher.dispatch(
                name=tool.name,
                arguments={"text": "via-dispatch"},
                context=_StubContext(),
            )
            assert result.ok is True
        finally:
            await runtime.aclose()

    async def test_empty_manifest_boots_green(self) -> None:
        registry = SubAgentToolRegistry()
        runtime = MCPRuntime([])
        summary = await runtime.startup(registry)
        assert summary == {}
        assert len(registry) == 0
        await runtime.aclose()  # idempotent, no managers
