"""Bob ``ToolDefinition`` ⇄ ``lmstudio`` SDK tool conversion (PRD 0017 / M4).

This is the deep, PURE, separately-testable converter pair that replaces the
OpenAI native codec for the LM Studio SDK transport (PRD 0017 decision Q5: the
SDK-native tool surface is the *only* tool path for LM Studio; the codec layer's
``NativeToolCodec`` is no longer the boundary). Two directions:

- :func:`tool_definitions_to_sdk` — Bob :class:`bob.llm.types.ToolDefinition` list
  → the SDK's low-level ``(LlmToolUseSettingToolArray, client_tool_map)`` pair the
  private :class:`lmstudio.json_api.ChatResponseEndpoint` consumes as ``llm_tools``
  / ``client_tool_map``. Deterministic spec ordering
  (:func:`bob.llm.tooling.schema.order_specs`) is preserved upstream so the wire
  tool order is byte-stable across runs (parity with the OpenAI path).
- :func:`tool_call_request_to_tool_call` — one SDK
  :class:`lmstudio.json_api.ToolCallRequest` (captured WITHOUT execution) →
  Bob :class:`bob.llm.types.ToolCall`. Malformed arguments raise
  :class:`bob.llm_client.LLMClientError`, byte-identical error surface to the
  OpenAI path (golden parity, see :mod:`tests.fixtures.tool_calling`).

WHY we bypass ``ChatResponseEndpoint.parse_tools``
--------------------------------------------------
``parse_tools`` expects each ``ToolFunctionDef.parameters`` to be a mapping of
*Python type hints* (it builds a ``msgspec`` struct + derives the JSON Schema
itself) and requires a real Python ``implementation`` callable per tool, because
its purpose is to drive ``act()``'s agentic *executor*. Bob already carries a
hand-written JSON Schema on every :class:`ToolDefinition` and dispatches tool
calls through the ORCHESTRATOR — it must never execute a callable here. So we
build the ``LlmTool`` directly from Bob's JSON Schema (the same
``{"type":"function","function":{name,description,parameters}}`` shape
``_to_llm_tool_def`` ultimately emits) and pair it with a sentinel
``client_tool_map`` that is never invoked.

The no-execution guarantee
--------------------------
The SDK only ever calls a tool's implementation via
``endpoint.request_tool_call_async()`` inside the ``act()`` loop. The ``complete``
driver never calls either (it only *collects* :class:`PredictionToolCallEvent`
args), so the sentinel implementations in :data:`client_tool_map` are unreachable.
They additionally raise on call as a belt-and-braces guard
(:func:`_never_called`) — if a future refactor ever wired them up, it would fail
loudly rather than silently execute a tool.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, cast

from lmstudio._sdk_models import (
    LlmTool,
    LlmToolFunctionDict,
    LlmToolUseSettingToolArray,
    ToolCallRequest,
)

from bob.llm.tooling.schema import order_specs
from bob.llm.tooling.spec import ToolSpec
from bob.llm.types import ToolCall, ToolDefinition
from bob.llm_client import LLMClientError

if TYPE_CHECKING:
    from lmstudio.json_api import ClientToolMap


def _never_called(*_args: Any, **_kwargs: Any) -> Any:
    """Sentinel tool implementation — must never run (capture-only transport).

    Bob dispatches tool calls through the orchestrator, never through the SDK's
    ``act()`` executor. The ``complete`` driver only collects tool-call requests,
    so this is unreachable; raising makes a future mis-wiring fail loudly instead
    of silently executing a tool.
    """

    raise LLMClientError(
        "LM Studio SDK tool implementation was invoked — the SDK transport "
        "captures tool-calls for orchestrator dispatch and must never execute "
        "them itself. This indicates a regression in the capture driver."
    )


def tool_definitions_to_sdk(
    tools: list[ToolDefinition],
) -> tuple[LlmToolUseSettingToolArray, ClientToolMap]:
    """Convert Bob tool definitions to the SDK's low-level tool pair.

    Returns ``(llm_tools, client_tool_map)`` ready to hand to
    :class:`lmstudio.json_api.ChatResponseEndpoint` as ``llm_tools`` /
    ``client_tool_map``. ``llm_tools`` is the server-side advertisement built
    directly from each tool's JSON Schema (no ``parse_tools`` round-trip);
    ``client_tool_map`` is a sentinel map (one never-called entry per tool) that
    satisfies the endpoint's length/consistency assertions without ever being
    invoked (see module docstring).

    Tool order is made deterministic via
    :func:`bob.llm.tooling.schema.order_specs` (same ordering the OpenAI native
    path applies) so the advertised wire order is stable.
    """

    # Deterministic spec order, upstream of the SDK conversion (PRD 0017: keep
    # ``order_specs`` even though the native codec is bypassed for tools).
    specs = order_specs([ToolSpec.from_tool_definition(tool) for tool in tools])

    llm_tool_defs: list[LlmTool] = []
    client_tool_map: dict[str, Any] = {}
    for spec in specs:
        if spec.name in client_tool_map:
            # Mirror the SDK's own duplicate-name guard so the failure mode is
            # identical whichever conversion path is used.
            raise LLMClientError(
                f"Duplicate tool name {spec.name!r} in tool definitions — "
                "tool names must be unique."
            )
        # ``LlmTool._from_api_dict`` accepts the ``{"type","function":{name,
        # description,parameters}}`` shape with ``parameters`` as a raw JSON
        # Schema dict (the same shape the SDK's own ``_to_llm_tool_def`` emits).
        # The TypedDict declares a narrower ``parameters`` type, so cast.
        llm_tool_defs.append(
            LlmTool._from_api_dict(
                cast(
                    LlmToolFunctionDict,
                    {
                        "type": "function",
                        "function": {
                            "name": spec.name,
                            "description": spec.description,
                            "parameters": spec.parameters,
                        },
                    },
                )
            )
        )
        # The endpoint asserts ``len(client_tool_map) == len(llm_tools.tools)``.
        # The tuple shape is ``(params_struct, implementation, is_async)``; only
        # the length + presence matter for capture-only, so ``None`` stands in
        # for the params struct and ``_never_called`` for the (unreachable) impl.
        client_tool_map[spec.name] = (None, _never_called, False)

    llm_tools = LlmToolUseSettingToolArray(tools=llm_tool_defs)
    return llm_tools, client_tool_map


def tool_call_request_to_tool_call(request: ToolCallRequest) -> ToolCall:
    """Map one captured SDK :class:`ToolCallRequest` to a Bob :class:`ToolCall`.

    The SDK has already JSON-decoded the arguments server-side, so
    ``request.arguments`` is a ``Mapping`` (or ``None`` for a no-arg call).
    ``None`` decodes to ``{}`` (parity with the OpenAI path's empty-string →
    ``{}`` branch). Any other non-mapping shape is malformed and raises
    :class:`bob.llm_client.LLMClientError` — the same error surface the OpenAI
    transport produces for non-object arguments (golden parity, see
    :mod:`tests.fixtures.tool_calling`).

    A missing ``id`` is filled with the tool name (the SDK does not always assign
    a call id; the orchestrator only needs a stable, non-empty handle). The name
    is required by the wire format and always present.
    """

    arguments = request.arguments
    if arguments is None:
        decoded: dict[str, Any] = {}
    elif isinstance(arguments, dict):
        decoded = dict(arguments)
    else:
        raise LLMClientError(
            f"LM Studio SDK tool-call arguments for {request.name!r} are not a "
            f"JSON object (got {type(arguments).__name__}): {arguments!r}"
        )

    call_id = request.id or request.name
    return ToolCall(id=call_id, name=request.name, arguments=decoded)


__all__ = ["tool_call_request_to_tool_call", "tool_definitions_to_sdk"]
