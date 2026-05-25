"""Contract tests for :class:`bob.tools.registry.ToolRegistry`.

These tests target the registry directly (no dispatcher, no orchestrator)
so a regression in lookup / projection semantics surfaces in isolation.
The dispatcher-level happy-path / error-path tests live in
``test_tool_dispatcher.py``; per-tool argument-shape assertions live in
``test_tool_spawn.py``, ``test_tool_forward.py``, ``test_tool_cancel.py``.
"""

from __future__ import annotations

import pytest
from pydantic import BaseModel

from bob.tools.dispatcher import ToolHandlerContext
from bob.tools.registry import (
    ToolDefinition,
    ToolRegistry,
    build_default_registry,
)
from bob.tools.types import ToolHandlerOutcome


class _NoopArgs(BaseModel):
    pass


async def _noop_handler(ctx: ToolHandlerContext, args: BaseModel) -> ToolHandlerOutcome:
    return ToolHandlerOutcome(status="ok")


def _make_definition(name: str = "x", version: str = "v1") -> ToolDefinition:
    return ToolDefinition(
        name=name,
        version=version,
        description="test tool",
        parameters={"type": "object", "properties": {}, "required": []},
        args_model=_NoopArgs,
        handler=_noop_handler,
    )


def test_register_and_get_round_trip() -> None:
    registry = ToolRegistry()
    definition = _make_definition()

    registry.register(definition)

    assert registry.get("x") is definition
    assert registry.get("missing") is None


def test_register_rejects_duplicate_name() -> None:
    registry = ToolRegistry([_make_definition(name="dup")])
    with pytest.raises(ValueError, match="already registered"):
        registry.register(_make_definition(name="dup"))


def test_names_preserves_registration_order() -> None:
    registry = ToolRegistry(
        [
            _make_definition(name="alpha"),
            _make_definition(name="beta"),
            _make_definition(name="gamma"),
        ]
    )
    assert registry.names() == ["alpha", "beta", "gamma"]


def test_qualified_name_combines_version_and_name() -> None:
    definition = _make_definition(name="spawn_subtask", version="v1")
    assert definition.qualified_name == "v1.spawn_subtask"


def test_as_llm_definitions_projects_to_llm_shape() -> None:
    definition = _make_definition(name="alpha", version="v1")
    registry = ToolRegistry([definition])

    llm_defs = registry.as_llm_definitions()
    assert len(llm_defs) == 1
    assert llm_defs[0].name == "alpha"
    assert llm_defs[0].description == "test tool"
    assert llm_defs[0].parameters == {
        "type": "object",
        "properties": {},
        "required": [],
    }


def test_iter_and_len_match_registration() -> None:
    registry = ToolRegistry(
        [
            _make_definition(name="a"),
            _make_definition(name="b"),
        ]
    )
    assert len(registry) == 2
    assert [d.name for d in registry] == ["a", "b"]


def test_default_registry_contains_three_v1_tools() -> None:
    """Behavior preservation: the registry ships exactly the legacy tool surface."""

    registry = build_default_registry()
    assert registry.names() == [
        "spawn_subtask",
        "forward_to_subtask",
        "cancel_subtask",
    ]
    for tool in registry:
        assert tool.version == "v1"


def test_default_registry_required_field_parity() -> None:
    """Each tool's JSON schema must list the Pydantic-required fields verbatim.

    Failing this catches the case where the LLM-facing schema and the
    dispatcher-side Pydantic model drift apart (e.g. someone updates the
    model but forgets the schema or vice versa).
    """

    registry = build_default_registry()
    for tool in registry:
        json_schema = tool.args_model.model_json_schema()
        json_required = set(json_schema.get("required", []))
        decl_required = set(tool.parameters.get("required", []))
        assert json_required == decl_required, (
            f"required-field drift on {tool.name}: "
            f"pydantic={json_required} vs JSON schema={decl_required}"
        )
