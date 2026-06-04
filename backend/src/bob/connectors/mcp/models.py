"""Domain models + pure helpers for the MCP connector.

Keeps the protocol/transport types of the ``mcp`` SDK from leaking into the
adapter / projector layers. Two concerns live here:

- :class:`MCPServerConfig` — the minimal declaration of one MCP server (name +
  transport + command/url). This slice wires a single server with minimal
  config; the full ``mcp_servers`` manifest (allowlist, per-tool overrides) is
  issue 0094.
- :func:`extract_text_content` — folds an MCP ``CallToolResult``'s content
  blocks into a single plain-text string. Pure and SDK-shape-tolerant (accepts
  the real ``CallToolResult`` or any object exposing ``.content`` / ``.isError``)
  so the adapter handler stays a thin translator and the projector never touches
  the SDK.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

MCPTransport = Literal["stdio", "http"]
"""Supported MCP client transports.

``stdio`` launches the server as a local subprocess and speaks over its
stdin/stdout; ``http`` connects to a remote streamable-HTTP endpoint.
"""


@dataclass(frozen=True)
class MCPServerConfig:
    """Minimal declaration of one MCP server.

    - ``name`` — stable identifier; tool refs and log lines key on it.
    - ``transport`` — ``"stdio"`` (local subprocess) or ``"http"`` (remote).
    - ``command`` / ``args`` — the subprocess invocation for ``stdio``.
    - ``url`` — the endpoint for ``http``.

    Validation is intentionally light here (the full manifest with per-tool
    overrides is issue 0094); :meth:`MCPManager.connect` asserts the
    transport-appropriate fields are present.
    """

    name: str
    transport: MCPTransport = "stdio"
    command: str | None = None
    args: tuple[str, ...] = ()
    url: str | None = None
    env: dict[str, str] | None = None


def extract_text_content(result: Any) -> tuple[str, bool]:
    """Fold an MCP tool result's content blocks into ``(text, is_error)``.

    ``result`` is an ``mcp.types.CallToolResult`` (or any object exposing a
    ``content`` iterable and an ``isError`` flag). Each content block's ``text``
    attribute is concatenated with blank-line separators; non-text blocks
    (images, embedded resources) contribute their ``repr`` placeholder so the
    digest still reflects that the tool returned *something*. Returns the joined
    text plus the boolean ``isError`` flag (defaulting to ``False`` when absent).

    Pure: no SDK import, no I/O — safe to call from the projector and the
    handler alike.
    """

    is_error = bool(getattr(result, "isError", False))
    blocks = getattr(result, "content", None) or []

    parts: list[str] = []
    for block in blocks:
        text = getattr(block, "text", None)
        if isinstance(text, str):
            parts.append(text)
            continue
        # Non-text block (image / audio / embedded resource): keep a short
        # placeholder so the digest reflects a non-empty result instead of
        # silently dropping it.
        block_type = getattr(block, "type", None)
        parts.append(f"[{block_type or 'content'}]")

    return "\n\n".join(p for p in parts if p), is_error


__all__ = [
    "MCPServerConfig",
    "MCPTransport",
    "extract_text_content",
]
