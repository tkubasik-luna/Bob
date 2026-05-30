# P4 — HermesToolCodec (Claude CLI robustness)

## Parent

`prd/0008-tool-calling-unification.md`

## What to build

Build `HermesToolCodec` and migrate the Claude CLI backend onto it. The CLI has no constrained decoding, so robustness comes from the trained Hermes prompt format plus a tolerant parse chain: inject tool definitions as a `<tools>` JSON-schema block (ChatML), and parse `<tool_call>` blocks by wrapping output in `<root>`, parsing XML, then decoding each call with `json.loads → ast.literal_eval → fenced-JSON` — no hand-rolled brace counting. The Claude CLI backend stays first-class; only its fragile wire format dies. Delete `_repair_json_braces` (`llm_client.py:166-229`) and the bespoke `{"tool_calls":[…]}` addendum (`_build_tools_system_addendum`). Reusable for a future Hermes/vLLM endpoint.

## Acceptance criteria

- [ ] `HermesToolCodec` injects `<tools>` and parses `<tool_call>` via the tolerant `json → ast → fence` chain
- [ ] Claude CLI backend routed through `HermesToolCodec`; identical behavior on well-formed calls
- [ ] `_repair_json_braces` and `_build_tools_system_addendum` deleted
- [ ] Backend-swap parity: a tool call and a malformed-call recovery behave identically after swapping `LLM_PROVIDER` between Claude CLI and LM Studio
- [ ] 0057 golden fixtures green; streaming preserved on the Claude path

## Blocked by

- `issues/0058-canonical-toolspec-codec-seam.md`
