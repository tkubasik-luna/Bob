# Displayable Signals Catalog — Agent Activity Feed (LM Studio)

> Design brief for Claude-Design. What live information Bob can surface for
> **Jarvis + sub-agents** while the LLM runs, so the user understands what is
> happening (the LLM is slow on local hardware and the wait feels opaque today).
>
> Companion to: `docs/handoffs/reasoning-streaming-handoff.md` (engineering
> context, hypotheses, invariant) and `docs/features/0011-agent-activity-feed.md`
> (shipped feed).

## Why this exists

Priority = **understand what's happening, maximally, in real time**. The model
can be slow; today the user waits with no feedback during the longest phases.
This catalog enumerates every signal we can extract from LM Studio, tagged by
which transport yields it and at what cost, so a UI can be designed against the
full menu.

## The constraint that shapes everything: transport

Three ways to call LM Studio. Each exposes a different set of live signals.

| Signal | `/v1/chat/completions` (current) | `/api/v1/chat` native SSE | `/v1/responses` |
|---|---|---|---|
| Schema-constrained output (`response_format: json_schema`) | ✅ Bob relies on it | ❌ no param in docs | ⚠️ unclear |
| Reasoning token-by-token | ⚠️ `delta.reasoning_content` (unreliable — 0 seen in our logs) | ✅ `reasoning.delta` first-class | ✅ `reasoning` |
| Model-load progress | ❌ | ✅ `model_load.progress` 0→1 | ❌ |
| **Prompt-processing progress** | ❌ | ✅ `prompt_processing.progress` 0→1 | ❌ |
| Answer token-by-token | ✅ `delta.content` | ✅ `message.delta` | ✅ `output_text.delta` |
| Tool call live (name + args + result) | partial (our codec) | ✅ `tool_call.start/arguments/success/failure` | ✅ MCP only |
| Final stats (tok/s, TTFT, reasoning tokens) | only with `stream_options:{include_usage:true}` | ✅ `chat.end.stats` | ✅ |

**Invariant (do not break):** sub-agent action is validated from the aggregated
guided-JSON content. Native `/api/v1/chat` has **no schema param** → moving
sub-agents there loses constrained validation, falling back to the fragile
self-correction loop on small models. Reasoning is cosmetic; validation is not.
The two highest-value live signals (prompt-processing progress, reliable
reasoning) live ONLY on the native transport. That tension is the core
engineering decision (see "Open items").

## The signal menu, by lifecycle phase

Each signal tagged with the cheapest transport that yields it. Design should
treat every signal as **optional** — it must degrade gracefully when a transport
doesn't emit it.

### Phase 1 — Before first token (the impatience zone — highest value)

This is the silent dead-time today. Biggest perceived-latency win.

- 🔵 **Model loading** — `model_load.progress` (0→1) → "Chargement modèle… 65%". *(native only; only fires if model not already resident)*
- 🔵 **Reading your context** — `prompt_processing.progress` (0→1) → progress bar while the model ingests the prompt. *(native only)* The single biggest addition for "LLM slow, on s'impatiente": it's the wait before any token appears.
- ⏱ **Time-to-first-token** — counter ticking up, client-side, any transport.

### Phase 2 — Thinking (live)

- 🧠 **Reasoning stream word-by-word** — `reasoning.delta`. *(reliable on native; flaky/absent on current `/v1` endpoint — see spike)*. Already plumbed end-to-end via `ReasoningStreamReader` → feed.
- 🧠 **Reasoning collapsed summary** — fold long thinking into "Réfléchit… (N tokens)" with expand-to-read.
- 🪶 **Narrated-step fallback** — already built (issue 0070) for when reasoning is degraded (no token stream available).

### Phase 3 — Acting (tool calls)

- 🔧 **Tool about to run** — `tool_call.start` → chip "Recherche Gmail…".
- 🔧 **Live arguments** — `tool_call.arguments` → show query/params as they stream in.
- 🔧 **Result / failure** — `tool_call.success` (✓ + output snippet) / `tool_call.failure` (✗ + reason).

### Phase 4 — Answering

- 💬 **Answer word-by-word** — `message.delta` (native) / `delta.content` (`/v1`).

### Phase 5 — Done (telemetry footer)

From `chat.end.stats`:

- 📊 `tokens_per_second` — generation speed badge.
- 📊 `time_to_first_token_seconds` — responsiveness.
- 📊 `reasoning_output_tokens` — how much it "thought".
- 📊 `input_tokens` / `total_output_tokens` — context + output size.

### Cross-cutting (multi-agent — already modeled)

- Per-agent lane (Jarvis + each sub-agent), live status chip, collapsible right
  rail. Exists: `activityFeedStore`, `AgentLanes`, `AgentBlock`,
  `AgentActivityPanel`.
- Errors — `error` event → inline red state; final payload still arrives in
  `chat.end`.

## Recommended design target

Design the feed against the **native-SSE signal set** (richest), because the two
highest-value items for the stated goal exist only there. Per-agent block:

1. **Phase strip** — Load → Read context → Think → Act → Answer, with progress
   bars on Load and Read context (the wait phases).
2. **Live thinking pane** — word-by-word reasoning, collapsible.
3. **Tool chips** — name → streaming args → result/✓/✗.
4. **Perf footer** — tok/s, TTFT, reasoning tokens.

Graceful degradation: on the current `/v1` transport, Phase 1 collapses to a
single spinner and Phase 2 falls back to narrated steps. Same layout, fewer lit
signals.

## Open items (engineering — resolve before/with build)

1. **Run the spike** (endpoint was offline at writing — `192.168.86.21:1234`
   timed out). Settle: does guided-JSON suppress `reasoning_content` on `/v1`
   (hypothesis b), or does the endpoint never propagate it at all (hypothesis a)?
   Decides whether a native SSE client is mandatory. Script: see handoff step 1.
2. **Schema vs native conflict** — native `/api/v1/chat` exposes no
   `response_format`. Decide: keep `/v1` for sub-agent validation + native only
   for Jarvis (reasoning-heavy, freeform), OR accept the self-correction loop
   everywhere. Product call before coding the native client.
3. **Observability** — log reasoning chunk count / `reasoning_output_tokens` per
   call in `log_llm_call` to confirm signal presence in prod. Invisible today.

## Source files (for the implementer)

| Role | File |
|------|------|
| StreamChunk contract | `backend/src/bob/llm/types.py` |
| reasoning_content read (`/v1`) | `backend/src/bob/llm_client.py` |
| Reasoning/content split + `degraded` | `backend/src/bob/sub_agent/reasoning_stream.py` |
| Sub-agent streamed loop | `backend/src/bob/sub_agent/runner.py` |
| Jarvis emission | `backend/src/bob/orchestrator.py` |
| Capabilities / codec selection | `backend/src/bob/llm/tooling.py` |
| Native SSE event spec | `docs/reference/lmstudio-chat-streaming-events.md` |
| Frontend feed | `frontend/src/store/activityFeedStore.ts`, `components/AgentBlock.tsx`, `AgentActivityPanel.tsx` |
