# PRD 0017 — LM Studio inference via the `lmstudio` SDK

Shipped on 2026-06-08 from PRD `prd/0017-lmstudio-sdk-inference.md`.

> **Partial ship.** Issues 0111–0115 (the SDK transport, behind a flag) are landed.
> The final purge (issue 0116 — flip the default to `sdk`, delete the OpenAI
> transport + the `openai` dependency) is **intentionally held** pending manual
> end-to-end validation against a real LM Studio server. Until then the default is
> `openai` and behaviour is byte-for-byte unchanged.

## What it does

Bob can now run LM Studio inference over the official `lmstudio` Python SDK
(native websocket protocol) instead of the OpenAI-compatible `…/v1` HTTP API. A
transport flag `LLM_LMSTUDIO_TRANSPORT` (`sdk` | `openai`, default `openai`)
selects between the two per the same `LLMClient` interface, so the switch is
progressive and instantly reversible. With `sdk` selected, chat, streaming chat,
tool-calling and streaming tool-calling all go through the SDK — same responses,
same token-by-token streaming, same early-TTS start for the `say` tool, same
reasoning feed, same perf stats and error surface as the OpenAI transport. Model
management (load/list/probe) was already on the SDK and is unchanged.

## Technical surface

- **New setting** `LLM_LMSTUDIO_TRANSPORT: Literal["sdk", "openai"] = "openai"`
  (`backend/src/bob/config.py`), gated in `backend/src/bob/llm/factory.py` for
  both the global builders and the per-role builders (`_build_role_client`).
- **New package** `backend/src/bob/llm/lmstudio_sdk/`:
  - `client.py` — `LMStudioSDKClient` (the `LLMClient` façade): `chat`,
    `stream_chat`, `complete`, `stream_complete`, `supports_guided_json`. Holds a
    long-lived per-role `AsyncClient` (lazy-connect, persistent websocket) with
    reconnect+retry-once on a websocket drop.
  - `history.py` — pure `messages_to_chat` converter (Bob message dicts → SDK
    `Chat`, incl. assistant-with-tool_calls + tool-result round-trip; consecutive
    `system` rows coalesced; `system_validator` fold applied before conversion).
  - `tools.py` — `ToolDefinition` → SDK `LlmTool`; `ToolCallRequest` → `ToolCall`
    (malformed args → `LLMClientError`, golden-fixture parity).
  - `streaming.py` — M5 adapter: SDK `LlmPredictionFragment` → `StreamChunk`
    (text / reasoning / terminal `perf` from `LlmPredictionStats`).
  - `endpoint.py` — M6: `BobChatResponseEndpoint` subclasses the SDK chat
    response endpoint and overrides `iter_message_events` to resurface the
    `toolCallGenerationArgumentFragmentGenerated` events the SDK 1.5.0 drops,
    so tool-call arguments stream incrementally.
- **Swap integration** (`backend/src/bob/llm_swap.py`): the superseded SDK
  client is `aclose`-d after the replacement is registered, in both
  `RoleLLMSwitcher` (`swap_role` / `set_reasoning`) and `LLMSwitcher`
  (`swap_lm_model` / `swap_provider` / `swap_base_url`). `aclose_client` is a
  duck-typed no-op for non-SDK clients. `orchestrator.py` gained a
  `jarvis_client` read property for that path.
- **Dependency** `lmstudio` pinned `>=1.5.0,<1.6` (the M6 override touches
  private SDK API).
- The OpenAI-transport `LMStudioClient` and the native tool codec for LM Studio
  remain in place (used when the flag is `openai`); the Hermes codec for Claude
  CLI is untouched.

## Notable decisions

- **Side-by-side + flag, then purge.** The SDK client lives next to the OpenAI
  one behind the flag (default `openai`) so the migration is reversible at
  bring-up. Issue 0116 (purge) is gated on real-server validation — until done,
  `LLM_LMSTUDIO_TRANSPORT=openai` must reproduce current behaviour exactly.
- **Tool capture without execution.** The SDK has no one-shot "give me the
  tool-calls" path (`respond()` never exposes them; `act()` is an agentic
  executor — wrong fit for Bob's orchestrator dispatch). `complete` /
  `stream_complete` drop to the private `ChatResponseEndpoint` +
  `AsyncPredictionStream._iter_events()` and **collect** `PredictionToolCallEvent`
  without running any callable (sentinel impls, no `act()` loop).
- **Private-API risk is isolated + guarded.** The only private-API surfaces are
  the tool-capture path (0113) and the arg-fragment override (M6, 0114),
  confined to `client.py` / `endpoint.py`. A **contract guard test** feeds the
  real wire-dict shapes to `BobChatResponseEndpoint.iter_message_events` and
  fails loudly if a future SDK upgrade renames the event, moves the fragment off
  the `content` key, or reintegrates it — the anti-regression sentinel. The
  version pin backs this up.
- **Arg fragment carries no identity in 1.5.0.** The real
  `toolCallGenerationArgumentFragmentGenerated` wire dict only has `content`; the
  override reconstructs the tool-call id / name / index from the preceding
  `toolCallGenerationStart` / `toolCallGenerationNameReceived` messages.
- **Hard voice-latency invariant.** `stream_complete` emits at least one
  `tool_call_args_delta` before `tool_call_end` (asserted in tests) so the `say`
  tool's `PartialJsonParser` → `speech_delta` → TTS path keeps starting on the
  first words. A whole-call fallback would be a latency regression.
- **Reasoning via `config.raw`.** The per-role reasoning level rides on the SDK
  raw config (`{"fields": [{"key": "reasoning", "value": level}]}`), the SDK
  equivalent of the OpenAI `extra_body.reasoning`. The exact key LM Studio
  honours is the main open item for the manual POC; `None` omits it (documented
  fallback).
- **`LLM_TOOL_MODE`** stays meaningful for the OpenAI transport (side-by-side)
  and Claude CLI (hermes); for the LM Studio SDK transport `guided`/`hermes` are
  rejected cleanly and `native`/`auto` use SDK-native tools. `supports_guided_json`
  stays `True` (`chat(schema=…)` is really gated by native structured output).
- **Tests stay offline.** The SDK is faked at its boundary (model:
  `tests/test_lm_studio_manager.py`), so the whole suite runs without a live
  server. Real-server parity (notably the `reasoning` mapping) is a manual POC
  behind the flag, out of CI.

## Issues

- `issues/0111-lmstudio-sdk-chat-poc-flag.md` — transport flag + factory select + `LMStudioSDKClient` skeleton + `chat()` POC + M3 base — commit 33a0ebc
- `issues/0112-lmstudio-sdk-stream-chat.md` — `stream_chat()` + streaming adapter M5 — commit 7149527
- `issues/0113-lmstudio-sdk-complete-tool-capture.md` — `complete()` tool-capture + M4 converter + M3 tool-turn extension — commit 596fcda
- `issues/0114-lmstudio-sdk-stream-complete-args-override.md` — `stream_complete()` + M6 arg-fragment override + contract guard test + version pin — commit 4948652
- `issues/0115-lmstudio-sdk-client-lifecycle-swap.md` — long-lived per-role `AsyncClient` + reconnect+retry-once + close-on-supersede — commit 97205f7
- `issues/0116-purge-openai-transport.md` — **held** (purge the OpenAI transport + `openai` dependency) — pending real-LM-Studio validation
