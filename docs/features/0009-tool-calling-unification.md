# Tool-Calling Unification (Robust Codec Layer)

Shipped on 2026-05-29 from PRD `prd/0008-tool-calling-unification.md`.

> Feature-doc number `0009` continues the sequential ship-order series
> (`docs/features/0001…0008`). It is **decoupled from the PRD number**
> (`prd/0008-…`): the previous feature, Gmail/Mail-overlay, already took
> doc slot `0008` while coming from `prd/0007-…`. The series numbers by
> ship order, not by PRD id.

## What it does

Bob had grown three divergent tool-calling mechanisms (native LM Studio
function-calling, a fragile Claude-CLI `{"tool_calls":[…]}` prompt addendum,
and a `json.loads`-and-pray sub-agent envelope), each with its own parser and
its own way of failing. This feature collapses them onto one canonical path
and fixes two user-visible symptoms. First, when a sub-agent finishes a task
with a structured deliverable (a Mail card today), that card now actually
renders in the HUD instead of being silently flattened to text and dropped.
Second, when the model emits a malformed or schema-invalid tool call, Bob now
shows the model its specific error and lets it retry within a bounded budget,
instead of silently losing the call or killing the whole task. For developers,
registering a sub-agent tool is now just declaring a Pydantic args model — its
JSON Schema is shown to the model automatically, on every backend, with no
hand-written prose recipe.

## Technical surface

- **New backend package — `bob.llm.tooling`** — the codec layer that is the
  single source of truth for tool wire formats:
  - `spec.py` — `ToolSpec` (name, description, flat `parameters` JSON Schema
    derived from a Pydantic `args_model`), the one canonical tool shape shared
    by Jarvis and the sub-agent.
  - `codec.py` — the `ToolCodec` / `ToolCallStreamParser` protocols and
    `NativeToolCodec` (the LM Studio `tools=[]` → `message.tool_calls` path,
    extracted behaviour-identically out of `llm_client.py`), plus its
    streaming partial-argument parser so `say`'s `speech_delta` TTS streaming
    is preserved.
  - `hermes.py` — `HermesToolCodec`: the Nous-Hermes `<tools>` / `<tool_call>`
    ChatML format for the Claude CLI backend (which has no constrained
    decoding), with the tolerant `json.loads → ast.literal_eval → fenced-JSON`
    parse chain and a streaming parser. Replaces the deleted brace-repair hack.
  - `capability.py` — `BackendCapability` (declares `native_function_calling`
    / `guided_json` / `hermes_tags`), `select_codec(capability, mode)` with
    `ToolMode = auto|native|guided|hermes`, and `CodecNotAvailableError`. The
    factory picks the most robust supported codec per backend; no per-call
    branching, no long-lived feature flag.
  - `schema.py` — `flatten_schema()` (inlines `$ref`/`$defs`, collapses
    `Optional[X]` and string-`const` unions to flat enums, warns-not-drops on
    genuine heterogeneous unions, caps nesting depth) and `order_specs()`
    (deterministic name-sort for prompt-cache stability).
- **`bob.llm_client.py`** — the native path now lives in `NativeToolCodec`;
  the hand-rolled `_repair_json_braces` tolerant parser and the bespoke
  `_build_tools_system_addendum` / `{"tool_calls":[…]}` Claude-CLI block are
  **deleted** (PRD 0006 banned hand-rolled tolerant parsers).
- **`bob.sub_agent.runner.py`** — the sub-agent prompt now injects each tool's
  real argument JSON Schema (not name+description lines), and emitted
  `tool_call.args` are validated against the tool's `args_model` *before*
  dispatch. On LM Studio the whole `progress`/`tool_call`/`done` envelope is
  emitted under guided decoding (`response_format: json_schema` via the
  client's `supports_guided_json()`), so the envelope is valid by
  construction. A parse/validation error (unknown tool, invalid args, or — new
  in 0065 — an invalid `done` deliverable) is fed back under the
  `system_validator` role for a bounded retry, then forced to an explicit
  `done(failed, invalid_output)` on exhaustion. Never the `tool` role, never a
  silent drop.
- **`bob.sub_agent.actions.py`** — `done.ui_payload` is now the typed
  `Deliverable` union (`MarkdownDeliverable = str` **or**
  `ComponentDescriptor = {component, props}`); `SUB_AGENT_SCHEMA_VERSION`
  bumped `1 → 2`. The guided-JSON envelope schema drops the union field from
  its typed `properties` (an `anyOf`/`$ref` the decoder rejects) while keeping
  `additionalProperties: true`, so the field is still accepted on the wire and
  validated post-decode.
- **`bob.ui_registry.py`** — a single `_component_schema()` builder now backs
  both the `say` tool's `oneOf` and the new `validate_component_descriptor()`
  (module-level + method), so a sub-agent Mail deliverable is validated against
  the **same** schema the `say` tool uses — the two can never drift to two Mail
  schemas. `format: email` is enforced via `FormatChecker`.
- **`bob.task_store.py`** — a `result_payload` JSON column carries a structured
  `{component, props}` descriptor alongside the spoken `result` string, so a
  Mail card survives a persistence round-trip instead of being flattened.
- **`bob.tools.definitions.show_task_result.py`** — re-emits a stored
  descriptor verbatim with its original `component`, instead of force-wrapping
  every recalled result as `Markdown`.
- **`bob.context.prompt_fragments.py`** — the ~70-line Gmail prose recipe is
  extracted into a composable `GMAIL_SEARCH_SKILL_PACK`, loaded into the
  sub-agent prompt only when `select_skill_packs(goal)` matches a mail-shaped
  goal; `SUB_AGENT_V2_SYSTEM_PROMPT` stays tool-agnostic.
- **Golden fixtures** — `tests/fixtures/tool_calling.py` (`ENVELOPE_FIXTURES`)
  locks good + malformed tool-call behaviour (including the old broken-brace
  payloads) across paths before the refactor.

## Notable decisions

- **Backend swap (Claude CLI ↔ LM Studio) is first-class, permanently.** Each
  backend gets its most-robust codec: LM Studio leans on guided decoding
  (prevents malformed JSON), Claude CLI leans on the Hermes format + tolerant
  parse + self-correction (recovers from it, since the CLI cannot gate tokens).
  Selection is capability-driven via `select_codec`; `LLM_TOOL_MODE: auto`.
- **There is no `GuidedJsonCodec` class.** The PRD sketched one, but guided
  JSON is delivered as a *runner behaviour* — passing the `SubAgentAction`
  schema as `response_format` when `client.supports_guided_json()` — not as a
  third codec. Only `NativeToolCodec` and `HermesToolCodec` exist.
- **Validation feedback always rides the `system_validator` role, never
  `tool`** (PRD 0006 prompt-injection safety): echoing the model's own bad
  output back under a role it trusts would let it launder bad content. The
  offending payload is escaped behind an `[INVALID OUTPUT]:` marker.
- **The deliverable is validated like tool args.** `done.ui_payload`'s
  descriptor branch is validated against the single `ui_registry` schema and,
  on failure, routed through the *same* `system_validator` retry budget as a
  bad `tool_call.args` — the output half of the envelope is now as robust as
  the input half.
- **`ComponentDescriptor` is normalised back to a plain dict at the
  `_handle_done` boundary**, so 0064's dict-based persistence/transport and all
  the runner's `dict|str|None` helpers (`_deliverable_text`,
  `_redact_ui_payload_for_debug`, `_is_component_descriptor`) keep working
  unchanged — the type lives at the envelope edge, not through the whole path.
- **`flatten_schema` warns, never silently drops.** A genuine heterogeneous
  union is narrowed to its first branch with a logged warning (PRD 0008
  acceptance criterion) rather than emitting an `anyOf` a grammar compiler
  chokes on.
- **Privacy posture (0056) carried forward.** The structured Mail descriptor
  now persisted in SQLite + shipped on the completion event still passes
  through `_redact_ui_payload_for_debug` before any debug/JSONL sink — subject
  / `bodyPreview` / snippet stay out of the debug surface; the frontend render
  + LLM context still get the real content.

## Issues

- `issues/0057-tooling-golden-fixtures.md` — P0 golden fixtures locking
  tool-calling behaviour (no production change) — commit `bcbdea3`
- `issues/0058-canonical-toolspec-codec-seam.md` — P1 canonical `ToolSpec` +
  `ToolCodec` seam with `NativeToolCodec` — commit `2f2b868`
- `issues/0059-subagent-tool-args-schema.md` — P2 inject tool arg JSON Schema
  + validate args against `args_model` — commit `ef9bbcb`
- `issues/0060-guided-json-envelope.md` — P3 guided-JSON envelope on LM Studio
  via `response_format` — commit `e412a02`
- `issues/0061-hermes-tool-codec.md` — P4 `HermesToolCodec` for Claude CLI,
  drop hand-rolled brace repair — commit `5783dfc`
- `issues/0062-self-correction-loop.md` — P5 route tool-arg validation under
  `system_validator` with bounded retry — commit `0f703e4`
- `issues/0063-schema-hygiene-skills-split.md` — P6 `flatten_schema` /
  `order_specs` + Gmail recipe → skill pack — commit `cc5d25a`
- `issues/0064-deliverable-transport-dispatch.md` — P7a carry the deliverable
  descriptor structured end-to-end, fix the Mail overlay drop — commit
  `b1036f1`
- `issues/0065-deliverable-union-typed.md` — P7b type `done.ui_payload` as the
  validated `Deliverable` union, bump schema version `1 → 2` — commit `3bd8279`

## Manual live-smoke gates (deferred to user)

These need a running LM Studio (or Claude CLI) backend + the Tauri app and
were not exercised in CI:

- **0060** — run a Gmail-search sub-task on LM Studio and confirm the
  `progress`/`tool_call`/`done` envelope is produced under guided decoding
  (valid JSON every turn, no parse fallback).
- **0062** — feed an intentionally hard arg case and confirm the model is shown
  its error and recovers in-loop rather than failing the task.
- **0064 / 0065** — run a real Gmail search end-to-end and confirm the Mail
  overlay opens (structured descriptor survives persistence + the completion
  event), and that an invalid descriptor is self-corrected, not dropped.
