"""A minimal real stdio MCP server used by the transport integration test.

Run as a subprocess (``python _echo_server.py``) so the test exercises the
production ``_default_session_factory`` path end-to-end: a real stdio transport,
a real ``ClientSession``, the real ``initialize`` handshake, real ``list_tools``
and ``call_tool`` round-trips. No mock seam is injected.

Kept deliberately tiny (one happy tool + one error tool) so the test asserts
against a known surface without pulling an external package off the network.
"""

from __future__ import annotations

from mcp.server.fastmcp import FastMCP

mcp = FastMCP("echo-test-server")


@mcp.tool()
def echo(text: str) -> str:
    """Echo the given text back, prefixed — the happy-path tool."""

    return f"echo: {text}"


@mcp.tool()
def boom() -> str:
    """Always raise — exercises the isError result-folding path."""

    raise RuntimeError("intentional boom")


if __name__ == "__main__":
    mcp.run()
