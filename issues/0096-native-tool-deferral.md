## Status: SHIPPED then REVERTED (2026-06-04)

Shipped in c3076d5, **reverted** in 813268a. The acceptance criteria below were
built against a false premise: the `claude` CLI Bob shells out to exposes **no
live `defer_loading` / `mcp_toolset` wire** — it runs `--tools ""` and reads its
tool list only from the prompt. So "delegate discovery to the platform" only
ever dropped deferred tools from the prompt catalogue and made them uncallable
(it broke MCP tools on Claude CLI). The path was removed and tool advertisement
now runs the single `select_tools` lexical gate (issue 0092) on **every**
provider, with the same knobs — Claude CLI gates identically to LM Studio.
A real deferral wire would require the CLI to dispatch tools itself, bypassing
Bob's dispatch/blackboard (PRD 0010) — a separate, much larger change, not a
capability flag. The acceptance criteria below are kept for the historical
record only.

## Parent

prd/0015-mcp-tool-scaling.md

## What to build

Optional, provider-gated upgrade: when the active provider is native Anthropic,
delegate tool discovery to the platform's tool deferral (`defer_loading` /
`mcp_toolset`) instead of server-side retrieval. For LM Studio
(OpenAI-compatible) this path does not exist and server-side `select_tools`
(issue 0092-tool-retrieval-gating) is used. This layer must have zero impact on
the local path and is never on the critical path for local use.

- A branch in the LLM router / swap layer: native-Anthropic provider → skip
  `select_tools`, pass `defer_loading` for deferred tools and `mcp_toolset`
  deferral for MCP servers (frequently-used tools kept always-loaded);
  LM Studio / any OpenAI-compatible provider → server-side retrieval, unchanged.
- The choice is keyed off the resolved provider — no behavioural change for the
  full-LM-Studio configuration.

This slice is genuinely optional and may be deferred to a follow-up; the local
experience is complete without it.

## Acceptance criteria

- [ ] With provider = native Anthropic, the request uses native tool deferral and
      does not run server-side `select_tools`.
- [ ] With provider = LM Studio (OpenAI-compatible), behaviour is byte-for-byte
      unchanged from issue 0092 (server-side retrieval, no deferral params).
- [ ] Provider switching at runtime (existing picker) flips the path correctly.
- [ ] Full-LM-Studio configuration shows zero behavioural change.

## Blocked by

- issues/0094-mcp-manifest-lifecycle.md
