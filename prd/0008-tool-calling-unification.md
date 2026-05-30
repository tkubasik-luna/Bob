# 0008 — Tool-Calling Unification (Robust Codec Layer)

> Status: **proposed plan** (investigation + design, no code yet). Authored autonomously while the requester was AFK; the two open forks that would normally be asked up front are resolved in **Decisions Taken While AFK** below, with rationale, and flagged as reversible.

## Problem Statement

Bob has grown **three divergent tool-calling mechanisms**, each with its own wire format, its own parser, and its own failure mode. The weakest of the three runs in the sub-agent — exactly where the real tool work happens (Gmail search today; `web_search` / `web_fetch` and more tomorrow).

| Path | Where | Wire format | Parse | Failure mode |
|------|-------|-------------|-------|--------------|
| Jarvis + LM Studio | `llm_client.py:514` | native OpenAI `tools=[]` → `message.tool_calls` | SDK object | depends on model's native FC quality |
| Jarvis + Claude CLI | `llm_client.py:1406` | prompt addendum `{"tool_calls":[...]}` | `raw_decode` + `_repair_json_braces` (`llm_client.py:166`) | brace-balancer band-aid; silent drop on failure |
| Sub-agent (any backend) | `runner.py:587` | system-prompt action `{"action":"tool_call","name","args"}` | `json.loads` only (`runner.py:282`,`:321`) | force-fail to `done(invalid_output)`; no recovery |

Concrete defects, with evidence:

- **The sub-agent never tells the model the tool's argument schema.** `runner.py:784` injects only `` - `name` : description `` per tool. The argument shapes live nowhere structured — instead they are hand-written into a ~70-line French prose recipe for the single existing tool (`prompt_fragments.py:321-385`). This does not scale: every new tool needs another prose block, and the model is guessing argument names from prose.
- **The sub-agent throws away constrained decoding.** `LLMClient.chat(schema=…)` already wires LM Studio's `response_format: {"type":"json_schema", …}` (`llm_client.py:372-375`) — guided decoding that makes malformed JSON nearly impossible. The sub-agent calls `chat()` **without** `schema=` (`runner.py:587`) and instead asks for JSON in prose and hopes. This is the single biggest robustness miss, and it is a **regression against Bob's founding design** — `INVESTIGATION.md:49` names structured outputs / `json_schema` as *"le pattern clé"* for the LM Studio integration.
- **`_repair_json_braces` is a hand-rolled tolerant parser** (`llm_client.py:166-229`) — the exact thing PRD 0006 forbade ("a wrapper over … `partial-json-parser` … no hand-rolled tolerant parser", `prd/0006:87`). It exists only because the Claude CLI path hand-writes JSON with no schema constraint. It is a symptom, not a fix.
- **No self-correction.** On a malformed call the Jarvis path silently drops it (`llm_client.py:1452`, `:1463`) and the sub-agent force-fails the whole task to `done(failed, invalid_output)`. Neither echoes the specific error back to the model for a bounded retry, even though the validation/retry machinery (`system_validator` role, per-tool `RetryPolicy`) already exists from 0006. Seen live: a Gmail-search sub-task on a local model (`qwen3.5-9b`) emitted a markdown-fenced `progress` action, the `json.loads`-only parse failed, and with no recovery the task died `llm_failed` after a single iteration (`backend/logs/orchestration.jsonl`, 2026-05-28).
- **The deliverable half of the same envelope is untyped and lossy.** The `done` action the codec will validate on input also carries an *output* — `ui_payload` — which today is a free-form `dict | str` with no schema. When the sub-agent correctly emits a structured `{"component":"Mail","props":{…}}` deliverable (verified in the same log's sonnet runs), the descriptor is dropped at four points: `_deliverable_text` (`runner.py:196-213`) flattens it to text because it only reads `markdown`/`content`/`text` keys; `task.result` is a `str | None` column (`task_store.py:75`,`:339`,`:494`) so a descriptor cannot even be persisted; the `task_result` WS event ships only that text (`runner.py:1176-1182`); and the frontend's task-result effect always calls `setOverlayContent` (Markdown), never `setOverlayMail` (`SphereUI.tsx:239-259`). `show_task_result` then re-wraps any recalled result as `{"component":"Markdown",…}` (`show_task_result.py:166-169`), so recall is lossy too. **Net user-visible bug: the Mail overlay never appears even when the search succeeded** — the reported symptom.
- **Three formats = 3× the surface to maintain, test, and break.** A tool added to the sub-agent registry behaves nothing like a tool added to the Jarvis registry.

From the developer's perspective: adding a second sub-agent tool today means writing prose, not a schema; debugging a "tool call vanished" means knowing which of three parsers ran; and the most reliable mechanism we already paid for (guided decoding) is unused where it matters most.

## External Research (what robust systems do)

Two reference systems were studied. Both converge on the same shape: **one canonical tool representation + one robust parse/validate/retry loop, with the wire format swapped per backend.**

**Nous Hermes** (`NousResearch/Hermes-Function-Calling`) — a *trained* prompt-based format for models without native FC:
- Definitions as OpenAI-style JSON Schema inside `<tools></tools>` (ChatML system prompt); calls emitted as `<tool_call>{"name","arguments"}</tool_call>` (multiple allowed); results returned as a `tool`-role `<tool_response>`.
- Robust parse chain (`utils.py`): wrap output in `<root>`, parse XML, extract every `<tool_call>`, then decode each with `json.loads` → `ast.literal_eval` → markdown-fence extraction. Tolerant *without* hand-rolled brace counting.
- Schema validation (`validator.py`): pydantic `FunctionCall` + per-arg type check against the tool signature + required-args check.
- **Bounded self-correction loop** (`functioncall.py`, `max_depth=5`): on parse/validation/exec error, feed the exact error back and ask for a corrected call.
- Native engine support: vLLM `--tool-call-parser hermes`; LM Studio parses it for Hermes models.

**OpenClaw** (`openclaw/openclaw`) — a local-first personal agent (effectively a mature sibling of Bob):
- **"Providers own auth/catalog/runtime hooks; core owns the generic loop."** One canonical tool model; each provider has a codec that translates to its native format. This is precisely Bob's missing abstraction.
- Visible tools sent as **structured function definitions**, filtered (profile / allow-deny / sandbox / permissions) before reaching the model.
- **Flat schemas**: "prefer flat string enum helpers over `Type.Union`; some providers reject `anyOf`." Local / OpenAI-compatible endpoints choke on `anyOf` / `$ref` / deep nesting.
- **Deterministic ordering** of registry/tool lists before model payloads (prompt-cache stability).
- **Tools vs Skills**: tools = typed functions (act); skills = instruction packs (workflow). Bob's 70-line Gmail recipe is a *skill* crammed into the tool layer.

## Solution

Introduce a single **tool-calling codec layer**: one canonical `ToolSpec`, three interchangeable codecs selected by backend capability, and one shared parse → validate → (on error) retry-via-`system_validator` loop. Route **both** Jarvis and the sub-agent through it. Delete the bespoke per-path formats and the hand-rolled brace repair.

User- and developer-facing outcomes:

- Adding a tool means writing a Pydantic args model once; its JSON Schema is shown to the model automatically, on every backend, with no prose recipe.
- On LM Studio (the default and founding target), tool-call and control-envelope JSON is produced under **guided decoding**, so malformed JSON is effectively eliminated rather than repaired after the fact.
- A malformed or schema-invalid call is corrected in-loop (the model is shown its specific error and retries within the existing per-tool retry budget) instead of being silently dropped or failing the whole task.
- The three formats collapse to one canonical path; `_repair_json_braces` and the bespoke `{"tool_calls":[…]}` addendum are removed.
- The Gmail prose recipe moves out of the core action prompt into a composable skill pack, so the base contract stays small as the tool count grows.

## User Stories

1. As a Bob developer, I want to register a sub-agent tool by declaring a Pydantic args model, so that the model sees a real argument JSON Schema instead of me hand-writing a prose recipe.
2. As a Bob developer, I want one canonical `ToolSpec` shared by Jarvis and sub-agents, so that a tool behaves identically regardless of which actor calls it.
3. As a Bob developer, I want the wire format chosen by a per-backend codec, so that swapping LM Studio ↔ Hermes/vLLM ↔ Claude CLI does not require touching call sites.
4. As a Bob developer, I want sub-agent JSON produced under LM Studio guided decoding (`response_format: json_schema`), so that malformed-JSON failures are prevented at the source, not repaired afterward.
5. As a Bob developer, I want a malformed/invalid tool call echoed back to the model with its specific error for a bounded retry, so that one bad token does not silently lose a tool call or fail an entire task.
6. As a Bob developer, I want retry feedback injected under the `system_validator` role (never the `tool` role), so that a misbehaving model cannot launder its own bad output as trusted content (prompt-injection safety, inherited from 0006).
7. As a Bob developer, I want `_repair_json_braces` and the bespoke `{"tool_calls":[…]}` addendum deleted, so that we stop maintaining a hand-rolled tolerant parser that 0006 explicitly banned.
8. As a Bob developer, I want tool parameter schemas kept flat (no `anyOf`/`$ref`/deep nesting), so that local and OpenAI-compatible models and guided decoding do not choke on them.
9. As a Bob developer, I want the tool list ordered deterministically in the prompt, so that prompt-cache hits are stable across turns.
10. As a Bob developer, I want streaming preserved end-to-end (the `say` tool's `speech` field still flushes `speech_delta` via `PartialJsonParser`), so that progressive TTS is not regressed by the codec change.
11. As a Bob developer, I want the Gmail recipe extracted into a skill pack loaded only when relevant, so that the base sub-agent contract stays small as tools multiply.
12. As a Bob developer, I want golden fixtures for good and malformed tool calls (including the current broken-brace cases) before any refactor, so that behavior is locked and regressions are caught at PR time.
13. As a Bob developer, I want each migration phase independently shippable with its own tests, so that the running app never breaks mid-migration (the 0006 staging discipline).
14. As a Bob user, I want the assistant to recover from its own malformed tool calls instead of saying "Désolé, peux-tu reformuler ?" or silently doing nothing, so that it feels robust.
15. As a Bob developer, I want the codec selected by declared backend capability with no long-lived feature flag, so that we follow the 0006 "short stabilisation window, not a flag" discipline.
16. As a Bob operator, I want to swap the backend between Claude CLI and the LM Studio server (per actor, via `LLM_PROVIDER` / `JARVIS_BACKEND` / `SUBAGENT_BACKEND`) and get identical tool-calling behavior on either, so that the Claude ↔ local-model swap stays a first-class capability and is covered by tests on both backends.
17. As a Bob user, I want a sub-agent's structured deliverable (a Mail card today, other component cards later) to actually render when the task finishes, so that a successful Gmail search shows the mail instead of silently producing nothing.
18. As a Bob developer, I want `done.ui_payload` typed and validated as a `Deliverable` union (a Markdown string **or** a `{component, props}` descriptor) and carried structured end-to-end through persistence, the completion event, and `show_task_result`, so that the output half of the envelope is as robust as the validated input half.

## Decisions Taken While AFK

These two forks would normally be confirmed up front; resolved here with rationale. Both are reversible (config-level), not architectural lock-in.

- **Backend swap (Claude CLI ↔ LM Studio) is a first-class product principle — both stay, permanently.** The whole point of the codec layer is that swapping `LLM_PROVIDER` / `JARVIS_BACKEND` / `SUBAGENT_BACKEND` (`config.py:39,76-77`) is seamless: Jarvis and the sub-agent behave identically on either backend. Each backend gets its most-robust codec:
  - **LM Studio → `GuidedJsonCodec`** (constrained decoding via `response_format: json_schema`, `llm_client.py:372`), or `NativeToolCodec` when the configured model advertises reliable native function-calling. Matches `config.py:39` default + the founding pattern (`INVESTIGATION.md:49`).
  - **Claude CLI → `HermesToolCodec`.** The CLI has **no constrained decoding** — `chat(schema=…)` only appends the schema to the prompt as an instruction, it does not gate tokens (`llm_client.py:1055,1142`). So robustness on this backend comes from the trained Hermes prompt format (`<tools>`/`<tool_call>`) + the tolerant parse chain + the self-correction loop (P5), **not** guided JSON. This is what replaces today's fragile `{"tool_calls":[…]}` + `_repair_json_braces`.
  - A Hermes/vLLM endpoint, if ever added, reuses `HermesToolCodec` (or native Hermes parsing).
  Selection is capability-driven (see below); `LLM_TOOL_MODE: auto` picks the right one per backend.
- **Scope → full unification (P0→P6).** The request was explicitly "le plus robuste possible." The targeted subset **P2 + P3** fixes the LM Studio sub-agent path, but because Claude CLI swap is first-class, **P4 + P5 are not optional polish** — they are what make the Claude backend robust (no guided decoding to lean on there).

Hard constraints inherited from PRD 0006 (non-negotiable, carried verbatim):
- Validation/retry feedback uses the **`system_validator`** role, never `tool` (`prd/0006:88`). Hermes feeds errors back as a `tool` role — Bob must **not**; the codec adapts the Hermes loop to route through `system_validator`.
- **No long-lived feature flag** for the switch — capability detection + a short stabilisation window with rollback discipline (`prd/0006:84`).
- **No hand-rolled tolerant parser** — tolerant decoding is the documented `json.loads → ast.literal_eval → fenced-JSON` chain only (`prd/0006:87`).
- **Versioned everything** — codec/format identifiers and the action `schema_version` bump deliberately in PR.

## Implementation Decisions

### Architecture overview

```
ToolSpec  (name, description, parameters: flat JSON Schema, args_model)   ← single source of truth
   │   parameters derived from the Pydantic args_model via model_json_schema(), then flattened
   ▼
ToolCodec  ── selected by BackendCapability ──┐
   ├─ NativeToolCodec   inject via tools=[] param ; read message.tool_calls / streaming delta.tool_calls
   ├─ GuidedJsonCodec   inject schema via response_format json_schema ; envelope valid by construction   ← LM Studio default
   └─ HermesToolCodec   inject <tools> JSON-schema block (ChatML) ; parse <tool_call> via XML+json/ast/fence chain
   │
   ▼
ToolCallParser  (per codec)  →  ToolCall[]  →  arg-validate against args_model
   │                                              │ ok → dispatch
   └── parse error / schema-invalid ──────────────┘ → echo specific error under `system_validator`, retry (bounded by existing RetryPolicy)
```

- **One canonical `ToolSpec`.** Today Jarvis uses `ToolDefinition` (`parameters: dict` JSON Schema) and the sub-agent uses `SubAgentToolDefinition` (`args_model` Pydantic). Unify so `parameters` is *derived* from `args_model.model_json_schema()`; the registries stay separate (different visibility) but produce the same spec shape.
- **`ToolCodec` is the only thing that knows the wire format.** It exposes: `inject(messages, tools) -> messages`, and a `parse(raw | stream) -> ToolCall[]`. Call sites (`orchestrator`, `SubAgentRunner`) never see format details — openClaw's "core owns the loop, provider owns the format."
- **Capability-driven selection.** A `BackendCapability` (per provider/model) declares `native_function_calling`, `guided_json`, `hermes_tags`. The factory picks the most robust supported codec: native FC if declared reliable, else guided JSON, else Hermes tags. Configurable override (`LLM_TOOL_MODE: auto|native|guided|hermes`), defaulting to `auto`. No per-call branching.
- **Sub-agent envelope, backend-dependent robustness.** The sub-agent's control protocol (`progress`/`tool_call`/`done`, `actions.py:128`) carries the `SubAgentAction` union schema through the codec. On **LM Studio** the schema gates decoding (`response_format`), so the envelope is valid *by construction* — this removes the `json.loads`-and-pray path (`runner.py:321`) and the per-tool prose recipe. On **Claude CLI** the same schema is delivered Hermes-style (no token gating available), so validity is achieved *by recovery*: tolerant parse + P5 self-correction. Either way, `tool_call.args` are validated against the tool's `args_model` exactly as Jarvis args are.
- **Typed, lossless deliverable channel (output half of the envelope).** `done.ui_payload` becomes a validated `Deliverable` union — `MarkdownDeliverable(str)` or `ComponentDescriptor({component, props})` — declared in the `SubAgentAction` schema, so on LM Studio it is constrained-decoded valid-by-construction and on Claude CLI it is parse-recovered + self-corrected, exactly like `tool_call.args`. The descriptor is then carried *structured* the whole way: the runner stops flattening it (`_deliverable_text`, `runner.py:196-213`), persistence holds it structured (a JSON/`dict` result alongside the spoken `result_summary`, widening the `str`-only `task.result`), the completion event carries the descriptor (`runner.py:1176-1182`), and the frontend dispatches on `component` — `openOverlayFromDescriptor` already routes Markdown vs Mail (`SphereUI.tsx:156-179`); only the task-result effect (`:239-259`) needs to call it instead of assuming Markdown. `show_task_result` re-emits the stored descriptor with its original `component`, not a forced `Markdown` wrap (`show_task_result.py:166-169`). This is the same "validate the envelope" principle applied to its output, and it is what fixes the Mail-overlay drop.
  - **Single component-schema source of truth.** The `say` tool already embeds the full Mail JSON Schema (derived from `ui_registry.py:181`); the sub-agent `done.ui_payload` carries none today. When P7 validates the `ComponentDescriptor`, it must validate against the **same** `ui_registry` schema, not a second hand-written Mail schema — otherwise the two drift. Same flat-schema rule as tool args.
  - **Privacy posture (0056) extends to P7.** Today `task.result` holds only flattened text and the `_redact_ui_payload_for_debug` scrub (`runner.py:236-268`) covers just the debug envelope. P7 *persists the structured Mail descriptor* (subject + `bodyPreview` + snippet) in SQLite and ships it on the completion event — a wider leak surface. P7 must carry the 0056 redaction forward to the new persistence + event so subject/body stay out of the debug ring buffer, the `/ws/debug` feed, and the JSONL sink (frontend render + LLM context still get the real content, as today).
- **Streaming preserved.** The codec must support the streaming partial-argument extraction the `say` tool relies on (`PartialJsonParser` → `speech_delta`, `prd/0006:74`). `NativeToolCodec` reads `delta.tool_calls[].function.arguments` chunks; `GuidedJsonCodec` partial-parses the constrained stream; `HermesToolCodec` partial-parses inside `<tool_call>`. The parser stays the battle-tested `partial-json-parser` wrapper — no new hand-rolled parser.
- **Self-correction via `system_validator`.** The Hermes echo-error-and-retry loop is adopted but adapted to Bob's security model: the offending output is escaped and the specific parse/validation error is re-injected under `system_validator`, consuming the existing per-tool `RetryPolicy` budget. Replaces silent-drop (Jarvis) and force-fail (sub-agent).
- **Schema hygiene.** A `flatten_schema()` step inlines `$ref`, avoids `anyOf` where a flat enum works, and caps nesting depth — applied once when a tool is registered. Tool lists are ordered deterministically before injection (cache stability).
- **Skills vs tools.** Extract the Gmail recipe (`prompt_fragments.py:321-385`) into a `SkillPack` loaded into the sub-agent prompt only when the goal matches, leaving `SUB_AGENT_V2_SYSTEM_PROMPT` (`prompt_fragments.py:296`) focused on the action contract.

### Modules

- **`bob/llm/tooling/`** (new) — `ToolSpec`; `ToolCodec` protocol; `NativeToolCodec`, `GuidedJsonCodec`, `HermesToolCodec`; `BackendCapability` + `select_codec()`; `ToolCallParser` with the `json.loads → ast.literal_eval → fenced-JSON` chain; `flatten_schema()`.
- **`bob/llm_client.py`** — `complete()` / `stream_complete()` delegate injection + parsing to the codec; **delete** `_build_tools_system_addendum` (`:1377`), `_repair_json_braces` (`:166`), and the bespoke parse block (`:1432-1511`).
- **`bob/sub_agent/runner.py`** — `_build_messages` (`:784`) injects tool schemas via the codec instead of name+description lines; `_run` (`:587`) calls `chat(schema=…)` / the codec; tool-call args validated against `args_model`; parse/validation errors routed through `system_validator` rather than `_normalise_payload` force-fail.
- **`bob/sub_agent/tool_registry.py`** — `SubAgentToolDefinition` exposes `to_spec()` (derive flat `parameters` from `args_model`).
- **`bob/context/prompt_fragments.py`** — slim `SUB_AGENT_V2_SYSTEM_PROMPT`; new `SkillPack` fragments (Gmail recipe relocated).
- **`bob/config.py`** — `LLM_TOOL_MODE` (`auto` default) + per-backend capability defaults.

Deliverable-channel touch-points (P7):
- **`bob/sub_agent/actions.py`** — `DoneAction.ui_payload` typed as the `Deliverable` union instead of free-form; bump `SUB_AGENT_SCHEMA_VERSION`.
- **`bob/sub_agent/runner.py`** — `_deliverable_text` (`:196-213`) no longer flattens a descriptor; `_finalize_done` persists + emits the structured `Deliverable` alongside `result_summary` (`:1091-1106`,`:1176-1182`).
- **`bob/task_store.py`** — `result` (`:75`,`:339`,`:494`) widens from `str` to carry a structured deliverable (e.g. a `result_payload` JSON column) so a `{component,props}` descriptor survives a round-trip.
- **`bob/tools/definitions/show_task_result.py`** — `_show_task_result_handler` (`:166-169`) emits the stored descriptor's original `component` instead of a hardcoded `Markdown` wrap.
- **`frontend/src/components/SphereUI.tsx`** — the task-result effect (`:239-259`) dispatches via `openOverlayFromDescriptor` (`:156-179`) rather than always `setOverlayContent`.

### Migration phases (each shippable + tested → issues 0057+)

- **0057 — P0 Lock behavior.** Golden fixtures: good + malformed tool calls (incl. the broken-brace cases `_repair_json_braces` currently salvages) across all three paths. Extend `test_llm_client.py`, `test_sub_agent_v2_runner.py`. **No production code change.**
- **0058 — P1 Canonical spec + codec seam.** `ToolSpec`, `ToolCodec` protocol, extract current native path into `NativeToolCodec`. Behavior-preserving; golden tests stay green.
- **0059 — P2 Sub-agent tool args via schema.** Inject real arg JSON Schema (codec) instead of name+description; validate args against `args_model`. *Highest ROI.*
- **0060 — P3 Guided-JSON envelope for the sub-agent.** Emit `progress`/`tool_call`/`done` under `response_format: json_schema`. Removes the `json.loads`-and-pray path on LM Studio.
- **0061 — P4 Hermes codec (Claude CLI robustness).** Build `HermesToolCodec` (XML `<tools>`/`<tool_call>` + tolerant `json→ast→fence` chain) and migrate the Claude CLI backend onto it. The backend **stays first-class**; only its fragile wire format dies — **delete** `_repair_json_braces` + the `{"tool_calls":[…]}` addendum. Reused for a Hermes/vLLM endpoint if added.
- **0062 — P5 Self-correction loop.** Wire tool parse/validation errors into the `system_validator` retry budget (echo specific error → bounded retry), replacing silent-drop / force-fail. This is the **primary safety net on the Claude CLI backend**, which has no constrained decoding to prevent malformed output.
- **0063 — P6 Hygiene + skills split.** `flatten_schema()`, deterministic tool ordering, Gmail recipe → `SkillPack`.
- **0064 — P7 Typed deliverable channel (fixes the Mail-overlay bug).** Make `done.ui_payload` a validated `Deliverable` union in the `SubAgentAction` schema and carry the descriptor structured end-to-end: stop flattening in `_deliverable_text` (`runner.py:196`), persist it structured (`task_store.py`), ship it on the completion event (`runner.py:1176`), dispatch on `component` in the frontend task-result effect (`SphereUI.tsx:239`), and preserve the original component in `show_task_result` (`:166`). The transport + dispatch half is **independent of the codec** and can ship early — it fixes the already-correct sonnet path that produces a valid Mail payload today. The schema-validation half rides on P2/P3 (the envelope must be typed first). Two constraints: validate the descriptor against the **single `ui_registry` Mail schema** (no duplicate), and **carry the 0056 privacy redaction** onto the new structured persistence + completion event.

Early-exit: **0059 + 0060** resolve the LM Studio sub-agent pain. Since Claude ↔ LM Studio swap is first-class, **a complete robust state requires P4 + P5** — skipping them leaves the Claude backend on the fragile path. **P7's transport+dispatch fix is the highest *user-visible* ROI** (it restores the Mail overlay) and can land before the codec work, ahead of its schema-validation half.

### Testing strategy

- Backend: `uv run pytest` (golden fixtures + per-codec parse/inject unit tests + sub-agent runner integration), `uv run ruff check .`, `uv run ruff format --check .`, `uv run mypy .`.
- Per-codec parse tests must cover: multiple calls in one response, prose around the JSON, fenced JSON, single-quoted/py-dict args, and the exact broken-brace payloads from P0 (now handled by the tolerant chain or prevented by guided decoding — not by brace repair).
- Live smoke on LM Studio with a founding-stack model (Qwen 2.5 7B/14B or Llama 3.1 8B per `INVESTIGATION.md:60`): Gmail-search task end-to-end, plus an intentionally hard arg case to exercise the self-correction loop.
- Frontend unaffected, but `pnpm tsc --noEmit` if any generated tool/reason types change.

## Risks & Open Questions

- **Streaming regression on `say`.** The codec must not break `PartialJsonParser` → `speech_delta`. Mitigation: P1 keeps the native path byte-identical; streaming covered by golden + live tests before P4 touches formats.
- **Native FC quality varies by local model.** Some LM Studio models advertise `tools` but emit unreliable calls. Mitigation: `guided` is the safe default; `native` only when capability is declared reliable.
- **Guided decoding ≠ semantic validity.** `response_format` guarantees JSON shape, not correct tool/args. The `args_model` validation + self-correction loop (P5) covers semantics.
- **Schema flattening may lose expressiveness** (e.g. genuine unions). Mitigation: `flatten_schema()` warns rather than silently drops; tool authors keep args flat by convention (openClaw rule).
- **Backend-swap parity is a requirement, not a risk.** Claude CLI stays first-class alongside LM Studio (confirmed). Every golden + integration test runs against **both** backends; a tool call, a malformed-call recovery, and a sub-agent task must behave identically after swapping `LLM_PROVIDER`. The asymmetry to watch: LM Studio prevents malformed JSON via constrained decoding, while Claude CLI can only *recover* from it (tolerant parse + P5 self-correction) — so the Claude path leans harder on P4+P5 and must be tested for the recovery cases, not just the happy path.
- **P7 widens the privacy surface.** Persisting the structured Mail descriptor (subject/body) in SQLite + shipping it on the completion event re-opens the leak 0056 closed for the debug envelope. Mitigation: reuse `_redact_ui_payload_for_debug` (`runner.py:236-268`) on every new debug/JSONL path; a test asserts subject/`bodyPreview`/snippet never appear in a `DebugEvent` payload after P7.
- **Hermes `<scratch_pad>` GOAP reasoning** improves tool selection but costs tokens/latency — deferred (Bob is voice-latency sensitive); revisit only if tool-selection accuracy proves weak.

## Out of Scope

- New tools (beyond migrating Gmail / the `web_search` / `web_fetch` placeholders).
- Dynamic tool-visibility filtering (openClaw profiles/allow-deny/sandbox) — interesting later; the codec seam makes it additive.
- RAG / retrieval changes, streaming TTS internals.
- Broader overlay/HUD redesign. The *minimal* component-aware render dispatch needed to fix the Mail-overlay drop (the `SphereUI.tsx` task-result effect routing on `component` via the existing `openOverlayFromDescriptor`) is **in scope** via P7; a wider overlay rework is not.

## Appendix — Current-state evidence

- Native Jarvis path: `backend/src/bob/llm_client.py:514` (inject), parse via `message.tool_calls`.
- Claude CLI path: `llm_client.py:1377` (`_build_tools_system_addendum`), `:1406` (`complete`), `:1432-1511` (parse), `:143` (`_strip_code_fence`), `:166` (`_repair_json_braces`).
- Guided decoding already present but unused by sub-agent: `llm_client.py:372-375` (`response_format: json_schema`).
- Sub-agent: `runner.py:587` (`chat()` w/o `schema`), `:784-790` (name+description-only tool lines), `:308`/`:321` (`_normalise_payload` / `json.loads`), `actions.py:84` (`ToolCallAction`), `:128` (union), `:160` (`parse_action`).
- Gmail prose recipe: `prompt_fragments.py:321-385`; action contract: `:296`.
- Config: `config.py:39` (`LLM_PROVIDER`), `:76-77` (`JARVIS_BACKEND`/`SUBAGENT_BACKEND`).
- Founding intent (structured outputs as "le pattern clé"): `INVESTIGATION.md:49`.
- Deliverable-channel drop (Mail overlay never renders): `runner.py:196-213` (`_deliverable_text` flattens descriptor), `task_store.py:75`/`:339`/`:494` (`result: str | None`), `runner.py:1176-1182` (`task_result` ships text only), `frontend/src/components/SphereUI.tsx:239-259` (task-result effect → `setOverlayContent` only) vs `:156-179` (`openOverlayFromDescriptor` already supports Mail), `show_task_result.py:166-169` (forces `Markdown`).
- Live evidence: `backend/logs/orchestration.jsonl` (2026-05-28) — `qwen3.5-9b` run died `llm_failed` on a fenced `progress` action (parse fragility); sonnet runs produced a correct `{"component":"Mail",…}` `ui_payload` that still never reached the overlay (deliverable drop). Two faces, one symptom.

External sources:
- Hermes — `github.com/NousResearch/Hermes-Function-Calling` (README, `functioncall.py`, `utils.py`, `validator.py`); `huggingface.co/NousResearch/Hermes-2-Pro-Llama-3-8B`.
- OpenClaw — `docs.openclaw.ai/tools`; `github.com/openclaw/openclaw/blob/main/AGENTS.md`.
