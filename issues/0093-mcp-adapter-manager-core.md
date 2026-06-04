## Parent

prd/0015-mcp-tool-scaling.md

## What to build

The minimal end-to-end MCP client tracer: Bob connects to one MCP server,
discovers a tool, wraps it as a native sub-agent tool, and runs it through the
existing dispatch + projection + card pipeline. The LLM never speaks MCP — this
is strictly a backend↔tool-server protocol, so it works identically on full
LM Studio.

Add a new `bob.connectors.mcp` package (mirroring the tavily/gmail connector
layout):

- An `MCPManager` owning session lifecycle for a configured server: connect
  (stdio subprocess and streamable-HTTP transports), `list_tools`, `call_tool`,
  per-call timeout. A missing or unreachable server logs an actionable message
  and registers no tools — it never breaks the boot (mirrors the optional
  `TAVILY_API_KEY` invariant). The transport is the test mock seam.
- An `mcp_*` error taxonomy (base error + unreachable / tool-error /
  missing-server), mirroring `web_search_*`.
- An `adapter.wrap(mcp_tool, curation) -> SubAgentToolDefinition`: builds the
  `args_model` dynamically (Pydantic `create_model`) from the MCP tool's input
  JSON Schema; the handler delegates to `MCPManager.call_tool`, folding returned
  content, the MCP `isError` flag, and transport exceptions into the structured
  handler outcome with `mcp_*` codes.
- A `project_mcp_default(result) -> ProjectedResult` turning an MCP tool's text
  content into a capped transcript digest plus a `Markdown` deliverable card,
  non-terminal by default. Reused by every uncurated MCP tool, so branching a
  tool needs no projector code. The frontend already degrades an unknown
  component to a generic doc card — no new render path.

Register one server's discovered tools into the sub-agent registry so the slice
is dispatchable end-to-end. Curation/manifest hardening is the next slice; here
a single server wired with minimal config is enough to demo.

## Acceptance criteria

- [ ] `mcp` SDK added as a backend dependency.
- [ ] `adapter.wrap` (with a fake MCP tool descriptor + mocked session): produced
      tool definition validates good args, rejects bad args, dispatches OK, and
      maps a transport failure / `isError` to the correct `mcp_*` code.
- [ ] `MCPManager` (mocked transport): connect / list / call happy path; a
      missing server registers nothing and does not raise at boot; an unreachable
      call surfaces a structured error.
- [ ] `project_mcp_default` (pure): asserts digest capping, the Markdown
      deliverable, summary, and non-terminal default.
- [ ] One MCP server's tool is callable end-to-end through the existing
      dispatch + projection pipeline and renders a generic Markdown card.
- [ ] Backend boots green with no MCP server configured.

## Blocked by

None - can start immediately.
