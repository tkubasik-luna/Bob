"""Pure tests for the LM Studio SDK tool converter (PRD 0017 / M4, issue 0113).

The converter pair (:mod:`bob.llm.lmstudio_sdk.tools`) is pure — no SDK server,
no network — so these assert directly on the converted SDK structures:

- :func:`tool_definitions_to_sdk` — Bob ``ToolDefinition`` list → the SDK
  ``(LlmToolUseSettingToolArray, client_tool_map)`` pair, JSON-Schema params
  preserved, deterministic order, never-executed sentinel impls.
- :func:`tool_call_request_to_tool_call` — one captured SDK ``ToolCallRequest``
  → Bob ``ToolCall``; malformed arguments → ``LLMClientError`` (golden parity
  with the OpenAI native path, see :mod:`tests.fixtures.tool_calling`).
"""

from __future__ import annotations

from typing import Any

import pytest
from lmstudio._sdk_models import ToolCallRequest

from bob.llm.lmstudio_sdk.tools import (
    _never_called,
    tool_call_request_to_tool_call,
    tool_definitions_to_sdk,
)
from bob.llm.types import ToolCall, ToolDefinition
from bob.llm_client import LLMClientError
from tests.fixtures.tool_calling import NATIVE_WELL_FORMED


def _tool(name: str, *, props: dict[str, Any] | None = None) -> ToolDefinition:
    return ToolDefinition(
        name=name,
        description=f"desc {name}",
        parameters={
            "type": "object",
            "properties": props or {"q": {"type": "string"}},
            "required": list((props or {"q": {}}).keys()),
        },
    )


# --- ToolDefinition -> SDK ----------------------------------------------------


def test_tool_definitions_to_sdk_preserves_json_schema() -> None:
    params = {
        "type": "object",
        "properties": {"query": {"type": "string"}, "limit": {"type": "integer"}},
        "required": ["query"],
    }
    tool = ToolDefinition(name="web_search", description="search the web", parameters=params)

    llm_tools, client_tool_map = tool_definitions_to_sdk([tool])

    serialised = llm_tools.to_dict()
    assert serialised["type"] == "toolArray"
    tools_out = serialised["tools"]
    assert tools_out is not None
    (entry,) = tools_out
    assert entry["type"] == "function"
    assert entry["function"]["name"] == "web_search"
    assert entry["function"]["description"] == "search the web"
    # The JSON Schema is carried verbatim (not derived from Python type hints).
    assert entry["function"]["parameters"] == params
    # One sentinel client-tool entry per advertised tool (length consistency the
    # endpoint asserts), keyed by name.
    assert set(client_tool_map) == {"web_search"}


def test_tool_definitions_to_sdk_order_is_deterministic() -> None:
    """Tools are advertised in deterministic (sorted-by-name) order."""

    tools = [_tool("zebra"), _tool("alpha"), _tool("mike")]
    llm_tools, _ = tool_definitions_to_sdk(tools)
    tools_out = llm_tools.to_dict()["tools"]
    assert tools_out is not None
    names = [t["function"]["name"] for t in tools_out]
    assert names == ["alpha", "mike", "zebra"]


def test_tool_definitions_to_sdk_duplicate_name_raises() -> None:
    with pytest.raises(LLMClientError, match="Duplicate tool name"):
        tool_definitions_to_sdk([_tool("dup"), _tool("dup")])


def test_sentinel_implementation_never_executes() -> None:
    """The sentinel impls are wired but unreachable; calling one fails loudly."""

    _, client_tool_map = tool_definitions_to_sdk([_tool("web_search")])
    (_params, impl, is_async) = client_tool_map["web_search"]
    assert is_async is False
    assert impl is _never_called
    with pytest.raises(LLMClientError, match="must never execute"):
        impl()


# --- ToolCallRequest -> ToolCall ---------------------------------------------


def test_tool_call_request_maps_id_name_arguments() -> None:
    req = ToolCallRequest(
        type="function", name="spawn_subtask", id="call_7", arguments={"title": "x"}
    )
    call = tool_call_request_to_tool_call(req)
    assert call == ToolCall(id="call_7", name="spawn_subtask", arguments={"title": "x"})


def test_tool_call_request_none_arguments_decode_to_empty_dict() -> None:
    """A no-arg call (``arguments=None``) decodes to ``{}`` — parity with the
    OpenAI path's empty-args branch (see ``NATIVE_WELL_FORMED`` empty-args case)."""

    empty_fixture = next(f for f in NATIVE_WELL_FORMED if f.expected_arguments == {})
    req = ToolCallRequest(type="function", name=empty_fixture.name, id="c1", arguments=None)
    call = tool_call_request_to_tool_call(req)
    assert call.arguments == empty_fixture.expected_arguments == {}


def test_tool_call_request_missing_id_falls_back_to_name() -> None:
    req = ToolCallRequest(type="function", name="say", id=None, arguments={"speech": "hi"})
    call = tool_call_request_to_tool_call(req)
    assert call.id == "say"


def test_tool_call_request_well_formed_fixture_parity() -> None:
    """Each well-formed native fixture maps to the same decoded arguments the
    OpenAI native path produces (transport equivalence)."""

    for fixture in NATIVE_WELL_FORMED:
        # The SDK pre-decodes arguments server-side, so the captured request
        # already carries the parsed mapping (= the OpenAI path's json.loads).
        req = ToolCallRequest(
            type="function",
            name=fixture.name,
            id="c1",
            arguments=fixture.expected_arguments,
        )
        call = tool_call_request_to_tool_call(req)
        assert call.name == fixture.expected_name
        assert call.arguments == fixture.expected_arguments


def test_tool_call_request_non_mapping_arguments_raises() -> None:
    """A non-object arguments payload is malformed → ``LLMClientError`` (golden
    parity with the OpenAI native path's malformed-args hard fail)."""

    # ``ToolCallRequest`` is an immutable msgspec Struct that type-checks its
    # fields at construction, so a non-mapping ``arguments`` can only reach the
    # converter via a duck-typed request whose ``.arguments`` is malformed. The
    # converter reads only ``.name`` / ``.id`` / ``.arguments``.
    class _MalformedRequest:
        def __init__(self) -> None:
            self.name = "say"
            self.id = "c1"
            self.arguments = ["not", "an", "object"]

    with pytest.raises(LLMClientError, match="not a JSON object"):
        tool_call_request_to_tool_call(_MalformedRequest())  # type: ignore[arg-type]
