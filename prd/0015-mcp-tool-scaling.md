# 0015 — Scaling tool surface: MCP connectors + goal-driven tool retrieval

## Problem Statement

Today every third-party capability Bob exposes to a sub-agent (Gmail, Tavily
web search) is a hand-written connector: a client, an error taxonomy, a tool
handler, and a result projector — roughly five files and a full test suite per
integration. The user wants to branch *many* more tools (weather, GitHub,
Slack, calendar, filesystem, …) without paying that cost each time.

Two problems block that:

1. **Cost per integration.** Writing a bespoke connector for every service does
   not scale to "many tools".
2. **Tool-surface bloat kills the local model.** The sub-agent runner advertises
   the *entire* tool registry to the model on every turn. Bob targets local
   LM Studio models that are already weak at tool-calling (the whole
   anti-hallucination effort). At 3 tools this is fine; at 30 the model sees 30
   schemas every call and mis-selects or stalls. The goal-triggered skill packs
   filter the *prose guidance*, but the *advertised tool catalogue* is the full
   registry, unfiltered.

The user must be able to keep Bob running **fully on LM Studio** (no Claude
provider) with the same capability surface — local operation is a first-order
constraint, not a nice-to-have.

## Solution

From the user's perspective: Bob can be taught a new external capability by
declaring it in a small config manifest pointing at a Model Context Protocol
(MCP) server — no new connector code. When the user asks Bob something
("quel temps demain à Paris ?"), Bob routes it to a sub-agent exactly as it does
for mail or web search; the sub-agent now has access to the new tool, calls it,
and Bob answers in French with a card in the HUD dock and a spoken summary. On
any failure (server down, missing config) Bob explains in French what to do —
never a broken state.

Under the hood, two capabilities are added:

- **Goal-driven tool retrieval** — before each sub-task, Bob ranks its known
  tools against the task goal (purely lexical, server-side, no model in the
  loop) and advertises only the most relevant few plus an always-on core. The
  local model sees a thin, relevant surface regardless of how many tools are
  registered. The full registry stays dispatchable.
- **MCP connector adapter** — Bob acts as an MCP *client*. A manager connects to
  declared MCP servers (local subprocess over stdio, or remote over HTTP),
  discovers their tools, and wraps each as a native sub-agent tool definition,
  with a curation manifest that narrows the argument surface and rewrites the
  description in French. A generic projector turns any MCP tool's text result
  into a Markdown card with no per-tool code; typed cards remain an opt-in
  upgrade.

The MCP integration is independent of the LLM provider: the model never speaks
MCP, so everything works identically on full LM Studio. Native Anthropic tool
deferral (`defer_loading` / Tool Search Tool) was specified as an optional,
provider-gated upgrade — **but it was shipped then reverted** (commit 813268a):
the `claude` CLI exposes no live deferral wire through Bob's invocation, so it
only dropped tools from the prompt catalogue and made them uncallable. Tool
advertisement now runs the single `select_tools` gate on every provider; see the
"provider-aware deferral" module note below.

## User Stories

1. As a Bob developer, I want to add a new external tool by declaring an MCP
   server in config, so that I do not write a bespoke connector per integration.
2. As a Bob developer, I want to expose only a chosen allowlist of an MCP
   server's tools, so that a verbose server does not dump its whole API surface
   on the model.
3. As a Bob developer, I want to rewrite a discovered tool's description and
   narrow its arguments in French, so that the local model gets a tight,
   idiomatic surface instead of the raw upstream schema.
4. As a Bob developer, I want a discovered MCP tool to render a usable card with
   zero per-tool UI code, so that branching a tool is cheap; I can upgrade to a
   typed card later only where the UX warrants it.
5. As a Bob user on a fully local LM Studio setup, I want every new tool to work
   without any Anthropic API dependency, so that I can run Bob fully offline
   (save for the tool's own network calls).
6. As a Bob user, I want Bob to pick the right tool for my request even when
   dozens are registered, so that asking for the weather does not get confused
   with mail or web search.
7. As a Bob user, I want Bob to stay responsive and accurate as more tools are
   added, so that scaling capability does not degrade the assistant.
8. As a Bob user, I want Bob to ask the weather and get a spoken French answer
   plus a dock card, so that I get the same polished experience as mail and web.
9. As a Bob user, when an MCP server is down or misconfigured, I want Bob to
   explain in French what is wrong, so that I am never left with a broken
   overlay or a silent failure.
10. As a Bob developer, I want a missing or unreachable MCP server to never break
    the backend boot, so that one bad integration does not take Bob down.
11. As a Bob developer, I want the tool-retrieval logic to be deterministic and
    unit-testable without a model, so that I can assert which tools a given goal
    surfaces.
12. As a Bob developer, I want an always-on core set of tools that is advertised
    regardless of the goal, so that a retrieval miss never leaves the model with
    no tools.
13. As a Bob developer, I want the model to still be able to call a registered
    tool that was not advertised this turn, so that gating the advertised
    surface never makes a registered tool undispatchable.
14. As a Bob developer, I want to tune the advertised tool count and relevance
    threshold via config, so that I can trade recall against surface size per
    deployment.
15. As a Bob developer, I want to add a tag to a tool to boost its retrieval for
    certain goals, so that I have a manual recall escape hatch before reaching
    for embeddings.
16. As a Bob developer, I want MCP sessions opened at startup and cleanly closed
    at shutdown, so that there are no zombie subprocesses.
17. As a Bob developer, I want a crashed or slow MCP call to surface a structured
    error rather than hang or crash, so that the sub-agent can speak a precise
    French sentence.
18. As a Bob developer, I want the existing Gmail and Tavily connectors left
    untouched, so that proven, controlled integrations are not destabilised by
    this work.
19. As a Bob developer running the Claude provider, I want the option to delegate
    tool discovery to Anthropic's native tool-deferral, so that strong models can
    use the platform feature — without that path being required for local use.
20. As a Bob developer, I want the weather capability to serve as an end-to-end
    acceptance case, so that the whole pipeline (retrieval → MCP call → projection
    → card → speech) is validated on a real tool.
21. As a Bob developer, I want a goal-triggered skill pack for weather, so that
    the sub-agent gets the recipe (extract place + date → call forecast →
    synthesise) without leaking tool names into Jarvis's prompt.
22. As a Bob user, I want a request spanning two intents ("mes mails et la
    météo") to surface both relevant tools, so that multi-intent goals are not
    starved.

## Implementation Decisions

### Module: tool retrieval (new, deep, pure)
- A `select_tools(registry, goal, *, k, min_score) -> list[ToolDefinition]`
  function. Pure, deterministic, no I/O, no model.
- Scoring is lexical over each tool's `name + description + tags`, with field
  weighting (name highest, tags next, description lowest). Accent-stripped,
  French stop-words removed, deterministic tie-break by name.
- Selection rule: `always_on tools ∪ {tools scoring ≥ min_score}`, capped at `k`
  by score. The advertised set is **not** padded to `k` with zero-score tools —
  fewer tools is the desired outcome. Empty retrieval falls back to the
  `always_on` core so the model is never left tool-less.
- V1 uses a hand-rolled weighted keyword score (zero dependency). BM25
  (`rank-bm25`) is a drop-in upgrade behind the same interface if recall proves
  insufficient; embeddings are explicitly out of scope.

### Module: sub-agent tool definition (modified)
- Gains two optional fields: `always_on: bool` (default false) and
  `tags: tuple[str, ...]` (default empty). Backward compatible; existing tools
  default to non-core, untagged.
- Existing Gmail and web tools gain tags to drive their retrieval.

### Module: sub-agent runner (modified)
- The point that renders the tool catalogue for the prompt is fed the
  **retrieved subset** of the registry (via `select_tools` against the task
  goal), not the full registry.
- Tool **dispatch** is unchanged: it continues to resolve against the full
  registry by name. Advertised set ⊂ dispatchable set — a non-advertised but
  registered tool still resolves if the model calls it.

### Module: MCP manager (new, I/O boundary, mockable)
- An `MCPManager` owning the lifecycle of all configured MCP server sessions:
  connect (stdio subprocess and streamable-HTTP transports), `list_tools`,
  `call_tool`, health, restart, per-call timeout.
- Connected at FastAPI startup, closed at shutdown. A missing or unreachable
  server logs an actionable message and registers no tools — it never breaks the
  boot (mirrors the optional-`TAVILY_API_KEY` invariant).
- The transport is the mock seam for tests (mirrors Gmail's `service_factory`
  and Tavily's `client_factory`).

### Module: MCP adapter (new, deep, pure save the delegated call)
- An `adapter.wrap(mcp_tool, curation) -> ToolDefinition` that builds a native
  sub-agent tool definition from a discovered MCP tool:
  - `args_model` built dynamically (via Pydantic `create_model`) from the MCP
    tool's input JSON Schema; the curation manifest may restrict the argument
    subset and is the escape hatch when the schema is lossy.
  - handler delegates to `MCPManager.call_tool`, folding the returned content,
    the MCP `isError` flag, and any transport exception into the structured
    handler outcome with an `mcp_*` error taxonomy (mirrors `web_search_*`).
  - description is the French override from the manifest when present.

### Module: generic MCP projector (new, pure)
- A `project_mcp_default(result) -> ProjectedResult` that turns an MCP tool's
  text content into a capped transcript digest plus a `Markdown` deliverable
  card, non-terminal by default. Reused by every uncurated MCP tool, so branching
  a tool requires no projector code. The frontend already degrades an unknown
  component to a generic doc card, so no new render path is needed.
- Per-tool curation may set `terminal` (e.g. a single-shot forecast) and, later,
  point at a typed projector for a dedicated card.

### Module: configuration (modified)
- A first-order `mcp_servers` manifest: per server `name`, `transport`,
  `command`/`url`, an `expose` allowlist, and per-tool overrides
  (`description_fr`, `args` subset, `tags`, `terminal`). Empty manifest ⇒ no MCP
  tools, boot green.
- Retrieval knobs: advertised-tool cap and minimum relevance score.

### Module: skill packs + Jarvis prompt (modified)
- A goal-triggered weather skill pack carrying the recipe, registered in the
  ordered skill-pack list (mirrors the Gmail and web packs).
- Jarvis's system prompt gains a single capability line for weather, leaking no
  tool name (mirrors the gmail/web routing pattern).

### Module: provider-aware deferral (REVERTED — see 813268a)
- Originally: when the provider is native Anthropic, the router skips
  server-side retrieval and passes `defer_loading` / `mcp_toolset` deferral to
  the API; LM Studio uses server-side retrieval.
- **Reverted.** Bob's `claude` CLI invocation has no live deferral wire (it runs
  `--tools ""` and reads tools only from the prompt), so "defer" dropped tools
  from the catalogue and made them uncallable. `select_tools` now runs on every
  provider with the same knobs — Claude CLI gates identically to LM Studio. A
  real deferral wire would mean the CLI dispatching tools itself, bypassing
  Bob's dispatch/blackboard — a separate, larger change.

### Architectural invariants
- The LLM never speaks MCP; MCP is strictly a backend↔tool-server protocol.
  Everything works identically on full LM Studio.
- The `SubAgentToolRegistry` stays the single canonical abstraction. MCP is one
  *source* of tool definitions behind it; retrieval gates the *advertised*
  subset in front of it.
- Existing Gmail and Tavily connectors are not migrated.

## Testing Decisions

A good test asserts external behaviour through a module's public interface, not
its internals: given inputs, assert the returned value or the observable
outcome. Prior art: the existing `test_web_search_tool` / `test_web_search_projector`
(handler + projection behaviour with the connector mocked at its boundary),
`tests/connectors/tavily/*` (transport-level mock), and the runner/orchestrator
suites.

Tests for **all** modules:

- **`select_tools`** — table-driven: for representative goals (mail, web,
  weather, multi-intent, junk), assert the exact advertised set and ordering;
  assert the `k` cap, the `min_score` threshold, the no-pad rule, the empty →
  `always_on` fallback, and determinism. No model, no I/O.
- **MCP adapter (`wrap`)** — with a fake MCP tool descriptor and a mocked
  session: assert the produced tool definition validates good args, rejects bad
  args, dispatches successfully, and maps a transport failure / `isError` to the
  right `mcp_*` code.
- **MCP manager** — with a mocked transport: assert connect/list/call happy path,
  a missing server registers nothing and does not raise at boot, an unreachable
  call surfaces a structured error, and shutdown closes sessions.
- **`project_mcp_default`** — pure: assert digest capping, the Markdown
  deliverable, summary, and non-terminal default; assert a curated `terminal`
  override is honoured.
- **Runner integration** — assert the rendered catalogue contains only the
  retrieved subset for a given goal while dispatch still resolves a
  non-advertised registered tool by name (advertised ⊂ dispatchable).
- **Weather end-to-end** — the spoken/HITL smoke checks below are the final
  acceptance; an integration test with a mocked weather session asserts the full
  chain (retrieval surfaces the tool → call → projection → deliverable).

## Out of Scope

- Embedding-based or model-driven tool retrieval. Retrieval is lexical;
  `tags` are the manual recall escape hatch. BM25 is a same-interface upgrade,
  not a deliverable here.
- Bob *exposing* its own tools as an MCP server (this PRD makes Bob an MCP
  *client* only).
- Migrating the existing Gmail or Tavily connectors to MCP — they are proven,
  thin, and intentionally kept.
- A typed weather UI card. The generic Markdown card is the shipped surface; a
  typed `Weather` card is a noted later upgrade.
- The native Anthropic tool-deferral path was specified, shipped, then reverted
  (813268a) — no live deferral wire exists through Bob's CLI invocation. Tool
  advertisement is the single `select_tools` gate on every provider.

## Further Notes

- Delivery is sliced as tracer bullets with a dependency DAG: retrieval/gating
  (foundation) and the MCP adapter core run in parallel; the manifest + lifecycle
  wiring follows the adapter; the weather end-to-end case depends on both
  retrieval and the manifest; the optional native-deferral layer followed the
  manifest (shipped then reverted — 813268a). The retrieval/gating slice has
  standalone value even with no MCP tools, because a thinner advertised surface
  already improves local-model reliability.
- The weather case is the chosen acceptance vehicle precisely because it
  exercises every layer end-to-end on a real, single-shot tool.
- First-order constraint throughout: full LM Studio operation. Any Anthropic-only
  capability is additive and provider-gated.
