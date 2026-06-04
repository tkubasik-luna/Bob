# Scaling tool surface: MCP connectors + goal-driven tool retrieval

Shipped on 2026-06-04 from PRD `prd/0015-mcp-tool-scaling.md`.

## What it does

Bob can be taught a new external capability by declaring a Model Context
Protocol (MCP) server in config — no bespoke connector code per integration.
A goal-driven retrieval layer keeps the local model's advertised tool surface
thin as the registry grows: before each sub-task Bob ranks its tools lexically
against the task goal and advertises only the most relevant few plus an
always-on core, while the full registry stays dispatchable. Weather is the
shipped acceptance case — asking "quel temps demain à Paris ?" routes to a
sub-task that calls a forecast MCP tool and answers in French with a card in
the HUD dock. Everything works fully on LM Studio; the LLM never speaks MCP.

## Technical surface

- **New module `bob.sub_agent.tool_retrieval`** — pure `select_tools(registry,
  goal, *, k, min_score)` (field-weighted lexical score over name+tags+desc,
  accent-stripped, French stop-words removed, `always_on ∪ {score ≥ min_score}`
  capped at `k`, no zero-score pad, empty → `always_on` fallback).
- **`SubAgentToolDefinition`** gains optional `always_on: bool` and
  `tags: tuple[str, ...]`. Gmail/web tools carry retrieval tags.
- **Runner** — `_advertise_tools` runs `select_tools` to feed the prompt
  catalogue on **every** provider (dispatch unchanged, advertised ⊂
  dispatchable). One gate, no provider branch — see the "Issue 0096 reverted"
  note below.
- **New package `bob.connectors.mcp`** — `errors` (`mcp_*` taxonomy),
  `models` (`MCPServerConfig`, `MCPToolOverride`), `manager` (`MCPManager`:
  stdio + streamable-HTTP transports, per-call timeout, restart-on-crash;
  `session_factory` mock seam), `adapter` (`wrap` builds args_model via Pydantic
  `create_model`, applies curation), `projector` (`project_mcp_default` /
  `make_mcp_projector(terminal=...)` → capped digest + Markdown card),
  `registration` (multi-server, `expose` allowlist, curation application),
  `lifecycle` (`MCPRuntime` connect-at-startup / close-at-shutdown).
- **Config** — `TOOL_RETRIEVAL_K`, `TOOL_RETRIEVAL_MIN_SCORE`, `MCP_SERVERS`
  manifest (per-server transport/expose/per-tool overrides), `MCP_CALL_TIMEOUT_SECONDS`.
  `.env.example` documents a `weather`/`get_forecast` manifest example.
- **FastAPI lifespan** (`main.py`) wires `MCPRuntime` startup/shutdown.
- **Weather skill pack** in `context/prompt_fragments.py` (`SUB_AGENT_SKILL_PACKS`,
  triggers météo/temps/weather/prévision) + one Jarvis capability line in
  `prompts/system_chat.md` (no tool name leaked).

## Notable decisions

- Retrieval is **lexical and pure** (zero dependency); `tags` are the manual
  recall escape hatch. BM25 is a same-interface upgrade; embeddings are out of
  scope. Gating touches only the prompt catalogue — **never** the dispatcher, so
  a registered-but-not-advertised tool still resolves by name.
- `always_on` core is the retrieval fallback — the model is never left tool-less.
- **MCP is strictly backend↔tool-server**; the LLM never speaks MCP, so the
  capability surface is identical on full LM Studio.
- A missing/unreachable MCP server logs an actionable message and registers
  nothing — boot stays green (mirrors the optional `TAVILY_API_KEY` invariant).
- A discovered tool renders via the **generic Markdown card** (`project_mcp_default`),
  so branching a tool needs no UI code; the frontend degrades an unknown
  component to a doc card. `terminal: true` tools converge; the runner rebuilds
  the card from the stored tool result on every exit path (PRD 0010 anti-stall).
- Existing **Gmail and Tavily connectors are not migrated** — kept as-is.
- **Issue 0096 was reverted** (2026-06-04, commit `813268a`): the native
  Anthropic tool-deferral path is gone. Its premise — a live
  `defer_loading`/`mcp_toolset` wire — never existed through Bob's CLI
  invocation (it runs `--tools ""` and reads tools only from the prompt), so a
  "deferred" tool was simply dropped from the catalogue and made uncallable.
  `select_tools` now runs on **every** provider with the same knobs, so Claude
  CLI gates identically to LM Studio and stays a reproducible debug reference;
  the gated prompt catalogue is the only scaling lever on a prompt-only backend.
  A real native-deferral wire would mean the CLI dispatching tools itself,
  bypassing Bob's dispatch/blackboard — a separate, larger change, not a flag.
  Removed: `supports_native_tool_deferral`, `ToolDeferralPlan` +
  `build_tool_deferral_plan`, `runner.last_deferral_plan`, the
  `native_anthropic_deferral` chip path.

## Issues

- `issues/0092-tool-retrieval-gating.md` — goal-driven tool retrieval gating — commit f1e8b9e
- `issues/0093-mcp-adapter-manager-core.md` — MCP adapter + manager + generic projector core — commit cb85285
- `issues/0094-mcp-manifest-lifecycle.md` — config-driven manifest + startup/shutdown lifecycle — commit 6b5ed8b
- `issues/0095-weather-end-to-end.md` — weather end-to-end acceptance case + skill pack — commit 1336ac2
- `issues/0096-native-tool-deferral.md` — provider-gated native Anthropic tool deferral — shipped c3076d5, **reverted** 813268a (no live deferral wire; unified on `select_tools`)
