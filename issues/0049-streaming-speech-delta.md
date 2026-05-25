## Parent

`prd/0006-jarvis-v2-context-overhaul.md`

## What to build

Stream Jarvis tool-call arguments end-to-end so the user hears Jarvis start speaking while the LLM is still generating.

Wrap a battle-tested partial-JSON library (e.g. `partial-json-parser` or equivalent) in a `PartialJsonParser` module. Do NOT hand-roll a tolerant parser. Add a `StreamEmitter` that, given the parser's incremental events, emits `speech_delta` WebSocket frames as the `say.speech` field accumulates, and emits a single `ui_payload` frame on argument-object close.

Switch `llm_client` to streaming mode for tool-call arguments (OpenAI-compatible `delta.tool_calls[0].function.arguments` consumption). Backend now flushes `speech_delta` token-by-token to the WS client. Frontend pipes `speech_delta` directly into the TTS engine for chunked synthesis and updates the sphere text progressively. The `ui_payload` overlay opens once the corresponding argument object has been fully validated.

There is NO feature flag — streaming is the only path post-merge. A short stabilisation window with rollback discipline replaces the flag (PRD architect requirement).

User-visible payoff: first audible token within a fraction of a second of message submit. UI overlay still appears at end (sequenced after spoken phrase).

## Acceptance criteria

- [ ] `PartialJsonParser` is a thin wrapper over a battle-tested library; no custom tolerant scanner.
- [ ] `StreamEmitter` emits one `speech_delta` per parser yield on the `speech` field; one `ui_payload` on argument-object close.
- [ ] `llm_client` consumes streamed tool-call argument deltas (OpenAI-compatible) and feeds them to the parser.
- [ ] WS protocol gains `speech_delta` and `ui_payload` frames; existing `assistant_msg` frame compatibility documented (kept for non-streaming code paths or removed if unreferenced — pick one and grep-clean).
- [ ] Frontend consumes `speech_delta` and pipes to TTS; sphere text renders progressively.
- [ ] Empty / zero-payload `ui` case handled (no overlay open).
- [ ] First `speech_delta` reaches frontend within an acceptable budget on the dev box (smoke target: < 500 ms after user submit for a simple say-only reply).
- [ ] Parser unit tests: UTF-8 split mid-codepoint, escaped quotes inside `speech`, nested objects in `ui`, trailing-comma tolerance.
- [ ] `StreamEmitter` contract tests: correct frame sequence given a fixed parser-event stream.
- [ ] End-to-end test with streamed-fake-LLM: assert frame sequence and timing relationships.
- [ ] Golden prompt + behavior snapshots updated.

## Blocked by

`issues/0047-unified-say-tool.md`, `issues/0048-validation-retry-policy.md`
