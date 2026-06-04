"""Manifest + lifecycle tests for the MCP fleet (issue 0094).

The transport is mocked at the connector boundary via the ``session_factory``
seam (the per-server mock seam from 0093): a fake async-context-manager yields a
fake :class:`MCPSession` with canned ``list_tools`` / ``call_tool``. No real
subprocess, no httpx, no ``mcp`` SDK transport stack is exercised.

Locks the manifest + lifecycle contract:

- a two-server manifest (one reachable, one absent) boots green: the reachable
  server's exposed tools register; the absent one registers nothing and never
  raises (per-server gating);
- the ``expose`` allowlist is honoured — only listed tools are wrapped;
- per-tool ``description_fr`` / ``args`` subset / ``tags`` / ``terminal``
  overrides are applied to the produced definitions;
- :meth:`MCPRuntime.aclose` closes every session (no zombie subprocesses);
- an empty manifest builds an empty runtime that registers nothing (boot green);
- a curated MCP tool's tags surface it via ``select_tools`` for a matching goal
  (the cross-module behaviour with issue 0092).
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import timedelta
from typing import Any

from bob.connectors.mcp import (
    MCPRuntime,
    MCPServerConfig,
    MCPToolOverride,
)
from bob.sub_agent.result_store import ToolResultStore
from bob.sub_agent.tool_registry import SubAgentToolRegistry
from bob.sub_agent.tool_retrieval import select_tools

# --- fakes ------------------------------------------------------------------


class _FakeTool:
    def __init__(self, name: str, *, input_schema: dict[str, Any] | None = None) -> None:
        self.name = name
        self.description = f"upstream desc {name}"
        self.inputSchema = input_schema or {
            "type": "object",
            "properties": {
                "city": {"type": "string", "description": "City name."},
                "units": {"type": "string", "description": "metric/imperial."},
            },
            "required": ["city"],
        }


class _ListResult:
    def __init__(self, tools: list[_FakeTool]) -> None:
        self.tools = tools


class _CallResult:
    def __init__(self, text: str) -> None:
        self.content = [type("_B", (), {"type": "text", "text": text})()]
        self.isError = False


class _FakeSession:
    def __init__(self, tools: list[_FakeTool]) -> None:
        self._tools = tools

    async def list_tools(self) -> _ListResult:
        return _ListResult(self._tools)

    async def call_tool(
        self,
        name: str,
        arguments: dict[str, Any] | None = None,
        read_timeout_seconds: timedelta | None = None,
    ) -> Any:
        return _CallResult(f"called {name}")


def _fleet_factory(
    sessions: dict[str, _FakeSession],
    *,
    absent: set[str] | None = None,
    closed: list[str] | None = None,
) -> Any:
    """Build a fleet ``session_factory`` keyed by server name.

    A server name in ``absent`` raises on connect (down/unreachable server). A
    closed server name is appended to ``closed`` when its CM exits — the seam the
    no-zombie-subprocess assertion checks.
    """

    absent = absent or set()

    @asynccontextmanager
    async def _cm(config: MCPServerConfig) -> Any:
        if config.name in absent:
            raise ConnectionError(f"{config.name} unreachable")
            yield  # pragma: no cover — makes this an async generator
        try:
            yield sessions[config.name]
        finally:
            if closed is not None:
                closed.append(config.name)

    return lambda config: _cm(config)


# --- two-server boot (reachable + absent) -----------------------------------


async def test_two_server_boot_reachable_registers_absent_skipped() -> None:
    """One reachable + one absent server: reachable registers, absent skipped."""

    sessions = {"weather": _FakeSession([_FakeTool("get_forecast"), _FakeTool("get_alerts")])}
    servers = [
        MCPServerConfig(
            name="weather",
            transport="stdio",
            command="weather-server",
            expose=("get_forecast",),  # only this one is wrapped
        ),
        MCPServerConfig(name="ghost", transport="stdio", command="missing-server"),
    ]
    runtime = MCPRuntime(servers, session_factory=_fleet_factory(sessions, absent={"ghost"}))
    registry = SubAgentToolRegistry()

    summary = await runtime.startup(registry)

    # The reachable server's EXPOSED tool registered; ``get_alerts`` was filtered
    # by the allowlist; the absent server registered nothing and did not raise.
    assert summary == {"weather": ["get_forecast"], "ghost": []}
    assert registry.get("get_forecast") is not None
    assert registry.get("get_alerts") is None
    assert len(registry) == 1


async def test_empty_manifest_registers_nothing_boots_green() -> None:
    runtime = MCPRuntime([])
    registry = SubAgentToolRegistry()
    summary = await runtime.startup(registry)
    assert summary == {}
    assert len(registry) == 0
    # aclose on an empty runtime is a no-op.
    await runtime.aclose()


# --- per-tool overrides applied ---------------------------------------------


async def test_per_tool_overrides_applied_to_definition() -> None:
    sessions = {"weather": _FakeSession([_FakeTool("get_forecast")])}
    servers = [
        MCPServerConfig(
            name="weather",
            transport="stdio",
            command="weather-server",
            expose=("get_forecast",),
            tools={
                "get_forecast": MCPToolOverride(
                    description_fr="Donne la météo d'une ville.",
                    args=("city",),  # drop "units"
                    tags=("météo", "weather", "temps"),
                    terminal=True,
                )
            },
        )
    ]
    runtime = MCPRuntime(servers, session_factory=_fleet_factory(sessions))
    registry = SubAgentToolRegistry()
    await runtime.startup(registry)

    defn = registry.get("get_forecast")
    assert defn is not None
    # description_fr override.
    assert defn.description == "Donne la météo d'une ville."
    # args subset — "units" dropped by the allowlist.
    assert list(defn.args_model.model_fields.keys()) == ["city"]
    # tags carried onto the definition.
    assert defn.tags == ("météo", "weather", "temps")
    # terminal override → the projector converges.
    store = ToolResultStore()
    stored = store.put(
        tool_name=defn.name,
        tool_version=defn.version,
        result={"tool": defn.name, "text": "Sunny.", "is_error": False},
        projector=defn.result_projector,
    )
    assert stored.projection.terminal is True


# --- shutdown closes every session ------------------------------------------


async def test_shutdown_closes_all_sessions_no_zombies() -> None:
    sessions = {
        "weather": _FakeSession([_FakeTool("get_forecast")]),
        "stocks": _FakeSession([_FakeTool("get_quote")]),
    }
    servers = [
        MCPServerConfig(name="weather", transport="stdio", command="w"),
        MCPServerConfig(name="stocks", transport="stdio", command="s"),
    ]
    closed: list[str] = []
    runtime = MCPRuntime(servers, session_factory=_fleet_factory(sessions, closed=closed))
    registry = SubAgentToolRegistry()
    await runtime.startup(registry)

    await runtime.aclose()

    # Every connected session's transport CM exited — no subprocess left running.
    assert sorted(closed) == ["stocks", "weather"]


async def test_shutdown_skips_servers_that_never_connected() -> None:
    sessions = {"weather": _FakeSession([_FakeTool("get_forecast")])}
    servers = [
        MCPServerConfig(name="weather", transport="stdio", command="w"),
        MCPServerConfig(name="ghost", transport="stdio", command="g"),
    ]
    closed: list[str] = []
    runtime = MCPRuntime(
        servers,
        session_factory=_fleet_factory(sessions, absent={"ghost"}, closed=closed),
    )
    await runtime.startup(SubAgentToolRegistry())
    await runtime.aclose()
    # Only the server that actually connected gets closed.
    assert closed == ["weather"]


# --- curated tags feed retrieval (cross-module with issue 0092) -------------


async def test_curated_tags_surface_tool_via_select_tools() -> None:
    """A tagged MCP tool is surfaced by ``select_tools`` for a matching goal."""

    sessions = {"weather": _FakeSession([_FakeTool("get_forecast")])}
    servers = [
        MCPServerConfig(
            name="weather",
            transport="stdio",
            command="weather-server",
            expose=("get_forecast",),
            tools={
                "get_forecast": MCPToolOverride(
                    description_fr="Donne la météo d'une ville.",
                    tags=("météo", "weather", "prévision"),
                    terminal=True,
                )
            },
        )
    ]
    runtime = MCPRuntime(servers, session_factory=_fleet_factory(sessions))
    registry = SubAgentToolRegistry()
    await runtime.startup(registry)

    # A weather goal whose words live ONLY in the curated tags (not the tool's
    # name "get_forecast") still surfaces the tool — the tags drove retrieval.
    selected = select_tools(registry, "quelle est la météo à Paris", k=8, min_score=1)
    assert any(d.name == "get_forecast" for d in selected)

    # An unrelated mail goal does NOT surface the weather tool.
    selected_mail = select_tools(registry, "mon dernier email reçu", k=8, min_score=1)
    assert all(d.name != "get_forecast" for d in selected_mail)
