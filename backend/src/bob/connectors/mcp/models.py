"""Domain models + pure helpers for the MCP connector.

Keeps the protocol/transport types of the ``mcp`` SDK from leaking into the
adapter / projector layers. Three concerns live here:

- :class:`MCPServerConfig` — the declaration of one MCP server (name +
  transport + command/url) plus the curation manifest issue 0094 adds: an
  ``expose`` allowlist and per-tool :class:`MCPToolOverride` records.
- :class:`MCPToolOverride` — the per-tool curation a manifest carries
  (``description_fr`` French rewrite, ``args`` narrowed subset, retrieval
  ``tags``, ``terminal`` single-shot flag).
- :func:`extract_text_content` — folds an MCP ``CallToolResult``'s content
  blocks into a single plain-text string. Pure and SDK-shape-tolerant (accepts
  the real ``CallToolResult`` or any object exposing ``.content`` / ``.isError``)
  so the adapter handler stays a thin translator and the projector never touches
  the SDK.
- :func:`parse_mcp_servers` — folds the first-order ``mcp_servers`` config value
  (a list of dicts, e.g. from ``.env`` JSON) into typed :class:`MCPServerConfig`
  records. Lenient: a malformed entry is dropped rather than crashing the boot.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

MCPTransport = Literal["stdio", "http"]
"""Supported MCP client transports.

``stdio`` launches the server as a local subprocess and speaks over its
stdin/stdout; ``http`` connects to a remote streamable-HTTP endpoint.
"""


@dataclass(frozen=True)
class MCPToolOverride:
    """Per-tool curation declared in the ``mcp_servers`` manifest (issue 0094).

    Every field is optional — an absent override leaves the upstream tool's own
    schema / description intact. The overrides exist to *narrow* a verbose
    upstream server so a weak local model never sees its raw schema:

    - ``description_fr`` — French rewrite advertised to the model in place of
      the (often terse, English) upstream description.
    - ``args`` — the narrowed argument subset; when set the built ``args_model``
      keeps only these properties (the escape hatch for an over-broad schema).
    - ``tags`` — retrieval keywords folded into the lexical score so
      :func:`bob.sub_agent.tool_retrieval.select_tools` surfaces the tool for a
      matching goal even when the goal words are not in its name/description.
    - ``terminal`` — when ``True`` the generic projector marks the result as a
      converged answer (a single-shot lookup, e.g. a weather forecast); the
      default generic projector is non-terminal.
    """

    description_fr: str | None = None
    args: tuple[str, ...] | None = None
    tags: tuple[str, ...] = ()
    terminal: bool = False


@dataclass(frozen=True)
class MCPServerConfig:
    """Declaration of one MCP server + its curation manifest.

    - ``name`` — stable identifier; tool refs and log lines key on it.
    - ``transport`` — ``"stdio"`` (local subprocess) or ``"http"`` (remote).
    - ``command`` / ``args`` — the subprocess invocation for ``stdio``.
    - ``url`` — the endpoint for ``http``.
    - ``expose`` — the tool allowlist: when set, ONLY tools whose name is listed
      are wrapped (the rest of a verbose server are dropped). ``None`` (the
      default) exposes every discovered tool — backward compatible with the
      single-server 0093 glue.
    - ``tools`` — per-tool :class:`MCPToolOverride` records keyed by tool name.

    Validation is intentionally light here; :meth:`MCPManager.connect` asserts
    the transport-appropriate fields are present.
    """

    name: str
    transport: MCPTransport = "stdio"
    command: str | None = None
    args: tuple[str, ...] = ()
    url: str | None = None
    env: dict[str, str] | None = None
    expose: tuple[str, ...] | None = None
    tools: dict[str, MCPToolOverride] = field(default_factory=dict)

    def override_for(self, tool_name: str) -> MCPToolOverride | None:
        """Return the curation override for ``tool_name`` if the manifest has one."""

        return self.tools.get(tool_name)

    def is_exposed(self, tool_name: str) -> bool:
        """True when ``tool_name`` passes the ``expose`` allowlist (or none is set)."""

        return self.expose is None or tool_name in self.expose


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


def _coerce_str_tuple(value: Any) -> tuple[str, ...]:
    """Fold a list/tuple of strings into a string tuple, dropping non-strings."""

    if not isinstance(value, (list, tuple)):
        return ()
    return tuple(item for item in value if isinstance(item, str) and item)


def _parse_override(raw: Any) -> MCPToolOverride:
    """Build one :class:`MCPToolOverride` from a manifest dict (lenient)."""

    data = raw if isinstance(raw, dict) else {}
    description_fr = data.get("description_fr")
    description_fr = description_fr if isinstance(description_fr, str) and description_fr else None

    args = data.get("args")
    args_tuple: tuple[str, ...] | None = _coerce_str_tuple(args) if args is not None else None

    return MCPToolOverride(
        description_fr=description_fr,
        args=args_tuple,
        tags=_coerce_str_tuple(data.get("tags")),
        terminal=bool(data.get("terminal", False)),
    )


def parse_mcp_servers(raw: Any) -> tuple[MCPServerConfig, ...]:
    """Fold a raw ``mcp_servers`` config value into typed :class:`MCPServerConfig`.

    ``raw`` is the manifest as it arrives from config — a list of dicts (e.g.
    JSON in ``.env``). Each entry maps to one server. The function is **lenient
    by design** so a malformed manifest never crashes the boot (the
    optional-``TAVILY_API_KEY`` invariant): an entry that is not a dict, or that
    lacks a usable ``name``, is dropped with the rest still parsed. An empty /
    absent / non-list value yields an empty tuple ⇒ no MCP tools, boot green.

    Recognised keys per entry: ``name`` (required), ``transport``
    (``"stdio"`` | ``"http"``, default ``"stdio"``), ``command``, ``args``,
    ``url``, ``env``, ``expose`` (tool allowlist), and ``tools`` (a dict mapping
    tool name → per-tool override dict). Unknown keys are ignored.
    """

    if not isinstance(raw, (list, tuple)):
        return ()

    servers: list[MCPServerConfig] = []
    for entry in raw:
        if not isinstance(entry, dict):
            continue
        name = entry.get("name")
        if not isinstance(name, str) or not name:
            continue

        transport = entry.get("transport", "stdio")
        if transport not in ("stdio", "http"):
            transport = "stdio"

        command = entry.get("command")
        command = command if isinstance(command, str) and command else None
        url = entry.get("url")
        url = url if isinstance(url, str) and url else None

        env_raw = entry.get("env")
        env: dict[str, str] | None = None
        if isinstance(env_raw, dict):
            env = {k: v for k, v in env_raw.items() if isinstance(k, str) and isinstance(v, str)}

        expose_raw = entry.get("expose")
        expose: tuple[str, ...] | None = (
            _coerce_str_tuple(expose_raw) if expose_raw is not None else None
        )

        tools_raw = entry.get("tools")
        tools: dict[str, MCPToolOverride] = {}
        if isinstance(tools_raw, dict):
            for tool_name, override_raw in tools_raw.items():
                if isinstance(tool_name, str) and tool_name:
                    tools[tool_name] = _parse_override(override_raw)

        servers.append(
            MCPServerConfig(
                name=name,
                transport=transport,
                command=command,
                args=_coerce_str_tuple(entry.get("args")),
                url=url,
                env=env,
                expose=expose,
                tools=tools,
            )
        )

    return tuple(servers)


__all__ = [
    "MCPServerConfig",
    "MCPToolOverride",
    "MCPTransport",
    "extract_text_content",
    "parse_mcp_servers",
]
