"""Adapter: discovered MCP tool → native :class:`SubAgentToolDefinition`.

This is the bridge that lets a sub-agent dispatch an MCP tool through the
*existing* registry / dispatcher / projector pipeline — the LLM never speaks
MCP. :func:`wrap` produces a :class:`SubAgentToolDefinition` whose:

- ``args_model`` is built **dynamically** via Pydantic ``create_model`` from the
  MCP tool's input JSON Schema, so the dispatcher validates good args and
  rejects bad args exactly as it does for a hand-written tool;
- ``handler`` delegates to :meth:`MCPManager.call_tool`, folding the returned
  text content, the MCP ``isError`` flag, and any transport exception into a
  structured :class:`SubAgentToolHandlerOutcome` with the ``mcp_*`` taxonomy
  (mirrors the ``web_search_*`` handler);
- ``result_projector`` defaults to :func:`project_mcp_default` (generic Markdown
  card) — no per-tool projector code.

A :class:`MCPToolCuration` lets a caller override the French description,
restrict the advertised argument subset, attach retrieval ``tags``, and mark the
tool ``terminal`` (single-shot). The manifest layer (issue 0094) folds each
server's per-tool :class:`bob.connectors.mcp.models.MCPToolOverride` into this
curation via :meth:`MCPToolCuration.from_override`.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import structlog
from pydantic import BaseModel, ConfigDict, Field, create_model

from bob.connectors.mcp.errors import (
    MCPError,
    MCPMissingServerError,
    MCPToolError,
    MCPUnreachableError,
)
from bob.connectors.mcp.manager import MCPManager
from bob.connectors.mcp.models import MCPToolOverride, extract_text_content
from bob.connectors.mcp.projector import make_mcp_projector
from bob.sub_agent.result_store import ToolResultProjector
from bob.sub_agent.tool_registry import (
    SubAgentToolDefinition,
    SubAgentToolHandlerContext,
    SubAgentToolHandlerOutcome,
)

_logger = structlog.get_logger(__name__)

#: JSON-Schema primitive ``type`` → Python type used when building the dynamic
#: ``args_model``. Anything unrecognised (``object``, ``array``, union, or a
#: missing type) falls back to ``Any`` so the model is permissive rather than
#: rejecting a tool whose schema we cannot fully model — validation that matters
#: (required-ness, primitive type) is still enforced; richness is the curation
#: escape hatch (issue 0094).
_JSON_TYPE_MAP: dict[str, type] = {
    "string": str,
    "integer": int,
    "number": float,
    "boolean": bool,
    "object": dict,
    "array": list,
}


@dataclass(frozen=True)
class MCPToolCuration:
    """Per-tool curation applied by :func:`wrap`.

    - ``description`` — French override advertised to the model (the raw
      upstream description is often terse English). ``None`` keeps the MCP
      tool's own description.
    - ``expose_args`` — when set, restricts the built ``args_model`` to this
      subset of the schema's properties (the escape hatch when an upstream
      schema is too broad). ``None`` keeps the full property set.
    - ``tags`` — retrieval keywords carried onto the produced
      :class:`SubAgentToolDefinition` so
      :func:`bob.sub_agent.tool_retrieval.select_tools` can surface this tool
      for a matching goal (issue 0092 cross-module behaviour).
    - ``terminal`` — when ``True`` the produced tool uses the terminal-aware
      projector (:func:`bob.connectors.mcp.projector.make_mcp_projector`) so a
      single-shot lookup converges instead of looping for more tool calls.
    """

    description: str | None = None
    expose_args: tuple[str, ...] | None = None
    tags: tuple[str, ...] = ()
    terminal: bool = False

    @classmethod
    def from_override(cls, override: MCPToolOverride | None) -> MCPToolCuration:
        """Fold a manifest :class:`MCPToolOverride` into a :class:`MCPToolCuration`.

        ``None`` yields the empty curation (the upstream tool kept verbatim). The
        field rename (``description_fr`` → ``description``, ``args`` →
        ``expose_args``) keeps the manifest vocabulary (issue 0094) decoupled
        from the adapter's internal curation shape.
        """

        if override is None:
            return cls()
        return cls(
            description=override.description_fr,
            expose_args=override.args,
            tags=override.tags,
            terminal=override.terminal,
        )


def _build_args_model(tool_name: str, input_schema: dict[str, Any] | None) -> type[BaseModel]:
    """Build a Pydantic args model from an MCP tool's input JSON Schema.

    Maps each declared property to a typed field (primitive types via
    :data:`_JSON_TYPE_MAP`, everything else ``Any``), marks the schema's
    ``required`` properties as required and the rest as optional (default
    ``None``), and carries each property's ``description`` onto the field so the
    advertised ``ToolSpec`` schema stays informative.

    A missing / non-object schema yields a model with no fields — the tool then
    takes no arguments, which validates an empty ``{}`` and rejects extras.
    """

    schema = input_schema if isinstance(input_schema, dict) else {}
    properties = schema.get("properties")
    properties = properties if isinstance(properties, dict) else {}
    required = schema.get("required")
    required_set = set(required) if isinstance(required, list) else set()

    field_defs: dict[str, Any] = {}
    for prop_name, prop_schema in properties.items():
        if not isinstance(prop_name, str):
            continue
        prop_schema = prop_schema if isinstance(prop_schema, dict) else {}
        py_type = _python_type_for(prop_schema)
        description = prop_schema.get("description")
        description = description if isinstance(description, str) else None

        if prop_name in required_set:
            field_defs[prop_name] = (py_type, Field(..., description=description))
        else:
            field_defs[prop_name] = (py_type | None, Field(default=None, description=description))

    # ``extra="forbid"`` so the dispatcher rejects arguments the schema does not
    # declare — symmetric to the hand-written tools' ``additionalProperties:
    # false`` posture and keeps a weak model from smuggling junk keys through.
    model_name = f"MCP_{tool_name}_Args"
    return create_model(
        model_name,
        __config__=ConfigDict(extra="forbid"),
        **field_defs,
    )


def _python_type_for(prop_schema: dict[str, Any]) -> Any:
    """Resolve one property schema to a Python type for ``create_model``."""

    json_type = prop_schema.get("type")
    if isinstance(json_type, str):
        return _JSON_TYPE_MAP.get(json_type, Any)
    # Union types (``["string", "null"]``) or absent type → permissive ``Any``.
    return Any


def _restrict_schema(
    input_schema: dict[str, Any] | None, expose: tuple[str, ...]
) -> dict[str, Any]:
    """Return a copy of ``input_schema`` keeping only ``expose`` properties.

    The ``required`` list is pruned to the exposed subset so a curation that
    hides a previously-required arg does not leave the model demanding a field
    it can no longer pass.
    """

    schema = dict(input_schema) if isinstance(input_schema, dict) else {}
    properties = schema.get("properties")
    properties = properties if isinstance(properties, dict) else {}
    kept = {k: v for k, v in properties.items() if k in expose}
    schema["properties"] = kept
    required = schema.get("required")
    if isinstance(required, list):
        schema["required"] = [r for r in required if r in expose]
    return schema


def wrap(
    mcp_tool: Any,
    manager: MCPManager,
    *,
    curation: MCPToolCuration | None = None,
    projector: ToolResultProjector | None = None,
    version: str = "v1",
) -> SubAgentToolDefinition:
    """Wrap a discovered MCP tool as a native :class:`SubAgentToolDefinition`.

    ``mcp_tool`` is an ``mcp.types.Tool`` (or any object with ``name`` /
    ``description`` / ``inputSchema``). ``manager`` is the connected
    :class:`MCPManager` the handler calls through. The handler never raises
    through the dispatcher: every failure folds into an ``mcp_*`` error code.
    """

    curation = curation or MCPToolCuration()
    name = getattr(mcp_tool, "name", None)
    if not isinstance(name, str) or not name:
        raise ValueError("MCP tool descriptor has no usable 'name'")

    raw_description = getattr(mcp_tool, "description", None)
    description = curation.description or (
        raw_description if isinstance(raw_description, str) and raw_description else name
    )

    input_schema = getattr(mcp_tool, "inputSchema", None)
    if curation.expose_args is not None:
        input_schema = _restrict_schema(input_schema, curation.expose_args)
    args_model = _build_args_model(name, input_schema)

    async def _handler(
        _ctx: SubAgentToolHandlerContext,
        args: BaseModel,
    ) -> SubAgentToolHandlerOutcome:
        """Delegate to the MCP server and fold the result into an outcome.

        Error mapping (mirrors ``web_search_*``):

        - :class:`MCPMissingServerError` → ``mcp_missing_server``
        - :class:`MCPUnreachableError`   → ``mcp_unreachable``
        - ``CallToolResult.isError``     → ``mcp_tool_error``
        - any other :class:`MCPError`    → ``mcp_tool_failed``
        """

        arguments = args.model_dump(exclude_none=True)
        try:
            result = await manager.call_tool(name, arguments)
        except MCPMissingServerError as exc:
            _logger.warning("mcp_tool.missing_server", tool=name, error=str(exc))
            return SubAgentToolHandlerOutcome(
                status="error", error_code="mcp_missing_server", error_message=str(exc)
            )
        except MCPUnreachableError as exc:
            _logger.warning("mcp_tool.unreachable", tool=name, error=str(exc))
            return SubAgentToolHandlerOutcome(
                status="error", error_code="mcp_unreachable", error_message=str(exc)
            )
        except MCPToolError as exc:
            _logger.warning("mcp_tool.tool_error", tool=name, error=str(exc))
            return SubAgentToolHandlerOutcome(
                status="error", error_code="mcp_tool_error", error_message=str(exc)
            )
        except MCPError as exc:
            _logger.warning("mcp_tool.failed", tool=name, error=str(exc))
            return SubAgentToolHandlerOutcome(
                status="error", error_code="mcp_tool_failed", error_message=str(exc)
            )

        text, is_error = extract_text_content(result)
        if is_error:
            # The server ran the tool and reported failure — the returned text
            # carries the reason, so surface it as the error message.
            _logger.warning("mcp_tool.is_error", tool=name)
            return SubAgentToolHandlerOutcome(
                status="error",
                error_code="mcp_tool_error",
                error_message=text or f"MCP tool '{name}' reported an error.",
            )

        return SubAgentToolHandlerOutcome(
            status="ok",
            result={"tool": name, "text": text, "is_error": False},
        )

    # A caller-supplied projector wins; otherwise the curation's ``terminal``
    # flag picks the terminal-aware default (a single-shot lookup converges)
    # versus the non-terminal generic projector.
    result_projector = projector or make_mcp_projector(terminal=curation.terminal)

    return SubAgentToolDefinition(
        name=name,
        version=version,
        description=description,
        args_model=args_model,
        handler=_handler,
        result_projector=result_projector,
        tags=curation.tags,
    )


__all__ = ["MCPToolCuration", "wrap"]
