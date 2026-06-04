"""Unit + integration tests for the MCP adapter (``wrap``) and registration.

Mirrors ``test_web_search_tool``: a fake MCP tool descriptor + a mocked MCP
session (via the manager's ``session_factory`` seam), asserting the produced
:class:`SubAgentToolDefinition`:

- validates good args and rejects bad args (dynamic ``args_model`` from the MCP
  tool's input JSON Schema);
- dispatches OK through the EXISTING :class:`SubAgentToolDispatcher`;
- maps a transport failure / ``isError`` to the correct ``mcp_*`` code;
- runs end-to-end through dispatch + projection + the single ``ui_registry``
  schema, rendering a generic ``Markdown`` card.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import timedelta
from typing import Any, ClassVar

import pytest
from pydantic import ValidationError

from bob.connectors.mcp import (
    MCPManager,
    MCPServerConfig,
    MCPToolCuration,
    register_mcp_tools,
    wrap,
)
from bob.sub_agent.result_store import ToolResultStore
from bob.sub_agent.tool_registry import (
    SubAgentToolDispatcher,
    SubAgentToolRegistry,
)
from bob.ui_registry import validate_component_descriptor

_CONFIG = MCPServerConfig(name="demo", transport="stdio", command="demo-server")


# --- fakes ------------------------------------------------------------------


_DEFAULT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "place": {"type": "string", "description": "City name."},
        "days": {"type": "integer", "description": "Forecast horizon."},
    },
    "required": ["place"],
}
_UNSET = object()


class _FakeTool:
    def __init__(
        self,
        name: str = "get_weather",
        description: str | None = "Get the weather forecast.",
        input_schema: Any = _UNSET,
    ) -> None:
        self.name = name
        self.description = description
        self.inputSchema = _DEFAULT_SCHEMA if input_schema is _UNSET else input_schema


class _TextBlock:
    def __init__(self, text: str) -> None:
        self.type = "text"
        self.text = text


class _CallResult:
    def __init__(self, text: str, *, is_error: bool = False) -> None:
        self.content = [_TextBlock(text)]
        self.isError = is_error


class _FakeSession:
    def __init__(self, *, call_result: Any = None, call_exc: Exception | None = None) -> None:
        self._call_result = call_result
        self._call_exc = call_exc

    async def list_tools(self) -> Any:
        class _R:
            tools: ClassVar[list[Any]] = [_FakeTool()]

        return _R()

    async def call_tool(
        self,
        name: str,
        arguments: dict[str, Any] | None = None,
        read_timeout_seconds: timedelta | None = None,
    ) -> Any:
        if self._call_exc is not None:
            raise self._call_exc
        return self._call_result


def _manager(session: _FakeSession) -> MCPManager:
    @asynccontextmanager
    async def _cm(_config: MCPServerConfig) -> Any:
        yield session

    return MCPManager(_CONFIG, session_factory=lambda config: _cm(config))


class _StubContext:
    task_id = "task-test"
    state: ClassVar[dict[str, Any]] = {}


# --- args model -------------------------------------------------------------


def test_wrap_builds_args_model_from_schema() -> None:
    defn = wrap(_FakeTool(), _manager(_FakeSession()))
    assert defn.name == "get_weather"
    assert defn.description == "Get the weather forecast."
    # Good args validate; optional arg defaults to None.
    parsed = defn.args_model.model_validate({"place": "Paris"})
    assert parsed.model_dump() == {"place": "Paris", "days": None}


@pytest.mark.parametrize(
    "bad",
    [
        {},  # missing required "place"
        {"days": 3},  # missing required "place"
        {"place": "Paris", "junk": 1},  # extra forbidden
    ],
)
def test_wrap_rejects_bad_args(bad: dict[str, Any]) -> None:
    defn = wrap(_FakeTool(), _manager(_FakeSession()))
    with pytest.raises(ValidationError):
        defn.args_model.model_validate(bad)


def test_wrap_no_schema_yields_no_arg_tool() -> None:
    defn = wrap(_FakeTool(input_schema={}), _manager(_FakeSession()))
    assert list(defn.args_model.model_fields.keys()) == []
    assert defn.args_model.model_validate({}).model_dump() == {}


def test_curation_overrides_description_and_restricts_args() -> None:
    curation = MCPToolCuration(description="Donne la météo.", expose_args=("place",))
    defn = wrap(_FakeTool(), _manager(_FakeSession()), curation=curation)
    assert defn.description == "Donne la météo."
    # "days" was dropped by the expose allowlist.
    assert list(defn.args_model.model_fields.keys()) == ["place"]


# --- handler / dispatch -----------------------------------------------------


async def _dispatch(manager: MCPManager, args: dict[str, Any]) -> Any:
    await manager.connect()
    defn = wrap(_FakeTool(), manager)
    registry = SubAgentToolRegistry([defn])
    dispatcher = SubAgentToolDispatcher(registry)
    return await dispatcher.dispatch(name="get_weather", arguments=args, context=_StubContext())


async def test_dispatch_happy_path() -> None:
    manager = _manager(_FakeSession(call_result=_CallResult("Sunny, 25C.")))
    result = await _dispatch(manager, {"place": "Paris"})
    assert result.ok is True
    assert result.result == {"tool": "get_weather", "text": "Sunny, 25C.", "is_error": False}


async def test_dispatch_rejects_bad_args_via_dispatcher() -> None:
    manager = _manager(_FakeSession(call_result=_CallResult("x")))
    result = await _dispatch(manager, {"days": 3})
    assert result.ok is False
    assert result.error_code == "invalid_args"


@pytest.mark.parametrize(
    ("exc", "code"),
    [
        (TimeoutError("timed out"), "mcp_unreachable"),
        (ConnectionError("reset"), "mcp_unreachable"),
    ],
)
async def test_dispatch_maps_transport_failure(exc: Exception, code: str) -> None:
    manager = _manager(_FakeSession(call_exc=exc))
    result = await _dispatch(manager, {"place": "Paris"})
    assert result.ok is False
    assert result.error_code == code


async def test_dispatch_maps_is_error_flag() -> None:
    manager = _manager(_FakeSession(call_result=_CallResult("upstream blew up", is_error=True)))
    result = await _dispatch(manager, {"place": "Paris"})
    assert result.ok is False
    assert result.error_code == "mcp_tool_error"
    assert result.error_message == "upstream blew up"


async def test_handler_missing_server_when_not_connected() -> None:
    # A manager that never connected → mcp_missing_server (not a crash).
    manager = _manager(_FakeSession(call_result=_CallResult("x")))
    defn = wrap(_FakeTool(), manager)
    registry = SubAgentToolRegistry([defn])
    dispatcher = SubAgentToolDispatcher(registry)
    result = await dispatcher.dispatch(
        name="get_weather", arguments={"place": "Paris"}, context=_StubContext()
    )
    assert result.ok is False
    assert result.error_code == "mcp_missing_server"


# --- end-to-end through dispatch + projection + card ------------------------


async def test_end_to_end_renders_generic_markdown_card() -> None:
    """The acceptance case: dispatch → projection → a valid Markdown card."""

    manager = _manager(_FakeSession(call_result=_CallResult("Forecast: sunny.")))
    await manager.connect()
    defn = wrap(_FakeTool(), manager)
    registry = SubAgentToolRegistry([defn])
    dispatcher = SubAgentToolDispatcher(registry)

    result = await dispatcher.dispatch(
        name="get_weather", arguments={"place": "Paris"}, context=_StubContext()
    )
    assert result.ok is True

    # Project through the SAME store the runner uses, with the tool's projector.
    store = ToolResultStore()
    stored = store.put(
        tool_name=defn.name,
        tool_version=defn.version,
        result=result.result,
        projector=defn.result_projector,
    )
    proj = stored.projection
    assert proj.terminal is False
    assert proj.deliverable is not None
    card = proj.deliverable[0]
    assert card["component"] == "Markdown"
    assert "Forecast: sunny." in card["props"]["content"]
    # Validates against the one ui_registry schema — no new render path.
    assert validate_component_descriptor(card) == []


# --- registration -----------------------------------------------------------


async def test_register_mcp_tools_registers_discovered_tools() -> None:
    manager = _manager(_FakeSession(call_result=_CallResult("x")))
    registry = SubAgentToolRegistry()
    names = await register_mcp_tools(manager, registry)
    assert names == ["get_weather"]
    assert registry.get("get_weather") is not None


async def test_register_mcp_tools_skips_dead_server() -> None:
    """A server that fails to connect registers nothing and does not raise."""

    @asynccontextmanager
    async def _cm(_config: MCPServerConfig) -> Any:
        raise ConnectionError("down")
        yield  # pragma: no cover

    manager = MCPManager(_CONFIG, session_factory=lambda config: _cm(config))
    registry = SubAgentToolRegistry()
    names = await register_mcp_tools(manager, registry)
    assert names == []
    assert len(registry) == 0


async def test_register_mcp_tools_skips_name_collision() -> None:
    manager = _manager(_FakeSession(call_result=_CallResult("x")))
    # Pre-register a tool named "get_weather" so the MCP one collides.
    existing = wrap(_FakeTool(), _manager(_FakeSession()))
    registry = SubAgentToolRegistry([existing])
    names = await register_mcp_tools(manager, registry)
    assert names == []  # collision skipped, no raise
    assert len(registry) == 1
