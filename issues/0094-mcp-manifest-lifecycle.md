## Parent

prd/0015-mcp-tool-scaling.md

## What to build

Harden the MCP core into a config-driven, multi-server, robust integration. A
developer branches a new tool by declaring it in a manifest — no code.

- A first-order `mcp_servers` config manifest: per server `name`, `transport`,
  `command`/`url`, an `expose` allowlist (only listed tools are wrapped), and
  per-tool overrides — `description_fr` (French rewrite for the local model),
  `args` (narrowed argument subset), `tags` (boost retrieval from issue
  0092-tool-retrieval-gating), and `terminal` (e.g. a single-shot lookup
  converges).
- Boot wiring: `MCPManager` connects all configured servers at FastAPI startup,
  discovers tools, applies curation, wraps each, and registers them into the
  registry built by the default sub-agent registry factory. Sessions are closed
  cleanly at shutdown — no zombie subprocesses.
- Robustness: per-server gating (a down/absent server registers nothing, logs
  actionable, boot stays green), restart on crash, and per-call timeout so a
  slow/crashed call surfaces a structured `mcp_*` error rather than hanging.
- Curation narrows the surface: `expose` allowlist + `args` subset + French
  description override, so a verbose upstream server never dumps its raw schema
  on the local model.

The existing Gmail and Tavily connectors are not migrated — they stay as-is.

## Acceptance criteria

- [ ] A manifest with two servers (one reachable, one absent) boots green: the
      reachable server's exposed tools are registered; the absent one is logged
      actionably and registers nothing.
- [ ] `expose` allowlist is honoured — only listed tools are wrapped; per-tool
      `description_fr`, `args` subset, `tags`, and `terminal` overrides are
      applied to the produced tool definitions.
- [ ] Killing a server subprocess mid-flight triggers a clean restart or a
      structured `mcp_unreachable` error — no backend crash.
- [ ] A slow call hits the per-call timeout and returns a structured error.
- [ ] FastAPI shutdown closes all MCP sessions (no zombie subprocesses).
- [ ] Curated tags feed retrieval: a tagged MCP tool is surfaced by
      `select_tools` for a matching goal.

## Blocked by

- issues/0093-mcp-adapter-manager-core.md
