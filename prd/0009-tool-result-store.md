# 0009 — Tool Result Store & Deterministic Deliverable Projection

> Status: **proposed plan** (investigation + design). Authored autonomously while the
> requester was AFK. Builds directly on the just-shipped 0008 codec unification and on the
> two mail-tool-loop investigations
> ([2026-05-29](../docs/investigations/2026-05-29-mail-tool-loop.md),
> [2026-05-30](../docs/investigations/2026-05-30-mail-overlay-empty-on-stall.md)).

## Problem Statement

The end-to-end flow **request → sub-task → tool call → data displayed** is unreliable on
*weak local models* (the founding target: 4–35B LM Studio models). 0008 made the *wire
format* robust (guided JSON, Hermes parse, self-correction). It did **not** fix the
*control-loop* failure that the 2026-05-30 investigation isolates, and it left a
context-cost problem that *causes* that failure.

Two coupled defects, with evidence:

### D1 — The deliverable depends on the weak model emitting a perfect `done`

A `gmail_search` returns the right data on the first call, then the 9B model **spins on
filler `progress`** and never emits the prescribed `done({component:"Mail", props})`
(2026-05-30 RC1). The stall guard force-terminates, but the forced exit salvages **text
only** — `_force_stalled_done` → `_emit_terminal_done(ui_payload=None)`
(`runner.py:1987-1995`, `:2040`). So `task.result_payload` is `null`, the `task_result`
frame omits it, and the **Mail overlay renders nothing** even though the data exists. The
deliverable is *deterministically reconstructable* from the result we already salvage as
text (`{component:"Mail", props: messages[0]}`) — but nothing builds it.

### D2 — The full tool result is replayed into every prompt, which *is* the stall driver

`_handle_tool_call` persists the tool result as a `tool` message holding the **entire**
blob: `json.dumps({"tool": name, "status":"ok", "result": <FULL RESULT>})`
(`runner.py:1550`). `_build_messages` replays **every** prior message verbatim each
iteration (`runner.py:1307-1310`). So a 2 KB Gmail result (subject + `bodyPreview` +
attachments + ids, per message) is re-sent on every turn. A weak model with a small
effective context drowns in its own transcript — which is exactly when it loses track that
the result is already in hand and starts narrating *"j'attends la réponse de l'outil"*. The
context bloat and the stall are the same bug seen from two sides.

### D3 — The fixes so far are patch-on-patch

The 2026-05-29/30 work added, in the runner loop, a thicket of guards: stall-streak
counters, force/nudge thresholds, duplicate-call dedup (RC4), control-char rejection (RC5),
"Trou A/B" errored-dispatch handling, text salvage, `redact_result_in_debug`,
`persist_result_on_failure`. Each is individually justified; together they are ~370 lines
of interleaved special cases in one `while True` (`runner.py:701-1281`) — *"des patch un
peu dans tous les sens."* They bound pathological loops but do not address D1/D2 at the
architectural level, so the deliverable still dies and the context still bloats.

## External Research (what robust agentic systems do)

- **Blackboard / artifact-store pattern** (classic multi-agent architecture; used by modern
  MCP-based and event-driven agent stacks). Agents write results to a *shared store* and
  pass **references**, not payloads. This "naturally reduces context overhead since agents
  store tool results … in the shared blackboard rather than maintaining them in individual
  contexts," and decouples downstream consumers from the producer.
- **Nous Hermes agent loop.** Keeps `tool` results inline but runs *preflight compression*
  once context exceeds ~50% and bounds the loop with an iteration budget (default 90,
  forced summary at 100%). It has **no** result-id store — inline-and-compress. For a weak
  *local* model the compress-after-the-fact approach is too late: the bloat already
  happened. Storing-and-referencing keeps context small *from the first turn*.
- **OpenClaw principle** (already adopted in 0008): *"core owns the generic loop; providers
  own the runtime hooks."* The natural place for "how do I turn this tool's result into a UI
  card / a compact digest / a spoken summary" is a **per-tool hook**, not the runner.

Synthesis: adopt the blackboard's *store-and-reference* for tool results (fixes D2 at the
source, not after the fact), and make the deliverable a **deterministic projection** of the
stored result via a per-tool hook (fixes D1 — the weak model is removed from the
display-critical path), collapsing the D3 patch-thicket into one mechanism.

## Solution

A small **Tool Result Store** (a per-run blackboard) plus a per-tool **result projector**.

```
tool dispatch ─ok─▶ ToolResultStore.put(result, projector)         ← blackboard
                       │  assigns ref  "gmail_search#1"
                       │  runs projector → ProjectedResult:
                       │       • digest      (compact)  ─────────▶ transcript message
                       │       • deliverable ({component,props})   (context saver, D2)
                       │       • summary     (deterministic text)
                       │       • terminal    (is this a complete answer?)
                       ▼
            ┌──────────────────────────────────────────────┐
            │  TERMINAL DELIVERABLE always comes from the    │
            │  store's projection, NEVER the model's free    │
            │  ui_payload, on EVERY exit path:               │
            │   • converge (terminal result → done now)      │  ← removes the stall window
            │   • model done(result_ref=…) → resolve store   │  ← "pass the data id"
            │   • forced stall / cap → last stored result    │  ← fixes 2026-05-30 (D1)
            └──────────────────────────────────────────────┘
```

**One rule replaces the patch-thicket:** *the deliverable is a deterministic projection of
a stored tool result.* The model decides **when** to finish and **which** result (by `ref`)
— or the runner decides for it (convergence / stall) — but the model never **produces** the
payload. A `{component:"Mail", props}` card is built by code from data that already exists,
so it survives every termination path.

User- and developer-facing outcomes:

- A successful `gmail_search` on a weak model **renders the Mail card** whether the model
  emits a clean `done`, references the result by id, stalls, or hits a cap.
- The sub-agent **converges in ~1 extra turn** on single-shot tools instead of burning 3–4
  filler `progress` rounds, because a *terminal* result force-finishes deterministically.
- The per-iteration prompt carries a **compact digest + a `result_ref`**, not the full blob
  — smaller context, fewer stalls, lower token cost.
- Adding a tool's display behaviour means writing **one projector function**, not a 70-line
  prose recipe and not new runner branches.
- The 2026-05-29/30 loop guards shrink to a thin **safety net** (they still bound genuinely
  pathological loops) rather than the primary mechanism.

## Decisions Taken While AFK

Reversible; flagged.

- **Store scope = per sub-agent run, in-memory.** The cross-*agent* data reference already
  exists: `spawn_task` returns a `task_id`, the deliverable persists on
  `task.result_payload`, and `show_task_result` recalls it by id (`show_task_result.py`).
  So `task_id` *is* the inter-agent id; the new `result_ref` is the *intra-loop* id. We do
  **not** add a new persistence table — that would be sprawl for no current consumer. If a
  future tool needs cross-task raw-result recall, the `StoredResult` is serialisable and the
  store can be promoted to `task_store` then. (Honours "éviter les patch dans tous les sens"
  and "évolutif".)
- **Convergence is per-tool and policy-gated, default ON.** `gmail_search`'s projector marks
  a result `terminal` (a mail lookup is single-shot — the recipe already says *"une fois un
  résultat NON VIDE reçu … termine"*). The runner force-finishes on a terminal result only
  when `SubAgentPolicy.converge_on_terminal_result` is True (default). A future multi-step
  tool returns `terminal=False` and relies on the model + the stall safety net. No global
  behaviour change is locked in.
- **The model's `ui_payload` stays supported for document-class tasks** (exposé / report /
  chronology produced as a markdown string with no backing tool result). The rule is: *if a
  usable stored result exists, the deliverable comes from its projection; else fall back to
  the model's `ui_payload`.* Both paths covered; no regression for document tasks.

Hard constraints carried from 0006/0008 (non-negotiable):

- The single Mail schema source of truth is `ui_registry`; a projected `Mail` deliverable is
  validated by `validate_component_descriptor` — no second Mail schema.
- Privacy (0056): the compact **digest that enters the transcript** and every **debug/JSONL
  sink** must keep email `subject`/`bodyPreview`/`snippet` out where 0056 already requires
  it. The digest deliberately *drops* `bodyPreview`; debug redaction
  (`_redact_ui_payload_for_debug`) still runs on the projected deliverable.
- Self-correction stays under `system_validator`, never `tool`.

## Implementation Decisions

### New module — `bob/sub_agent/result_store.py`

```python
@dataclass(frozen=True)
class ProjectedResult:
    digest: dict[str, Any]            # compact preview → transcript (D2)
    deliverable: dict[str, Any] | None  # {component, props} card, validated, or None
    summary: str                      # deterministic spoken/markdown summary
    terminal: bool                    # True → a complete answer; runner may converge

ToolResultProjector = Callable[[dict[str, Any]], ProjectedResult]

@dataclass(frozen=True)
class StoredResult:
    ref: str                          # "gmail_search#1"
    tool_name: str
    tool_version: str | None
    result: dict[str, Any]            # the full raw result (kept server-side only)
    projection: ProjectedResult

class ToolResultStore:               # per-run blackboard
    def put(self, *, tool_name, tool_version, result, projector) -> StoredResult: ...
    def get(self, ref: str) -> StoredResult | None: ...
    def last(self) -> StoredResult | None: ...
```

- **Default projector** (tools without a custom one): `digest = result` verbatim,
  `deliverable = None`, `summary = compact json`, `terminal = False`. So a tool with no
  projector behaves **exactly as today** (full blob in transcript, no card, model-driven
  termination). Additive, zero regression.
- Refs are `f"{tool_name}#{n}"` — human-readable, greppable, deterministic.

### Per-tool projector — on the tool definition

`SubAgentToolDefinition` gains `result_projector: ToolResultProjector | None = None`
(OpenClaw "provider owns the runtime hook"). `build_gmail_search_tool()` wires
`project_gmail_search`:

```python
def project_gmail_search(result) -> ProjectedResult:
    count = result.get("count", 0); msgs = result.get("messages") or []
    digest = {"count": count, "messages": [
        {"subject": m.get("subject"), "from": (m.get("from") or {}).get("name"),
         "receivedAt": m.get("receivedAt")} for m in msgs[:5]]}   # NO bodyPreview (0056 + D2)
    if count and msgs:
        return ProjectedResult(digest, {"component":"Mail","props":msgs[0]},
                               f"{count} email(s) — dernier : « {msgs[0].get('subject','')} »", True)
    return ProjectedResult(digest, None, "Aucun email ne correspond.", True)
```

`messages[0]` already matches the `Mail` props schema (`to_mail_props`,
`gmail/models.py:259`), so the deliverable validates against `ui_registry` unchanged.

### Runner changes (`runner.py`)

- `_run` owns one `ToolResultStore` per task. `_handle_tool_call` takes it, and on a
  **successful** dispatch: `stored = store.put(...)`, then persists the `tool` message as
  `{"tool": name, "status":"ok", "result_ref": stored.ref, "result": stored.projection.digest}`
  (compact — D2) instead of the full blob.
- **Convergence:** after a successful dispatch, if `stored.projection.terminal` and
  `policy.converge_on_terminal_result`, the runner finalises `done(complete)` immediately
  with the projected deliverable + summary — no further LLM turn. The happy-path stall
  window disappears.
- **Terminal deliverable, every path:** a new `_resolve_terminal_deliverable(store, result_ref)`
  returns `(ui_payload, summary)` from the store (by `result_ref` if given, else
  `store.last()`). Used by `_handle_done` (when the model's `ui_payload` is absent/invalid),
  by `_force_stalled_done`, and by the iteration/token-cap paths. This is the single place
  the deliverable is built — replacing `_salvage_tool_result_text` (text-only) for
  projector-backed tools and **fixing 2026-05-30** on stall/cap.
- **`DoneAction.result_ref: str | None`** (schema bump `2 → 3`): the model may finish by
  *referencing* the stored result instead of copying its data. `_handle_done` resolves it
  via the store.
- The 2026-05-29/30 guards (stall force/nudge, dedup, control-char) **stay** as the safety
  net but now rarely fire (convergence short-circuits the common case), and
  `_force_stalled_done` routes through `_resolve_terminal_deliverable`.

### Prompt / skill pack (`prompt_fragments.py`)

- Base contract gains one line: *tool responses carry a `result_ref`; to finish, emit
  `done(status, reason_code)` — you may set `result_ref` to the value shown; you do NOT need
  to copy the data into `ui_payload`.*
- The Gmail skill pack shrinks: the model no longer hand-builds the Mail descriptor (the
  runner does). Less for a weak model to get wrong.

### Modules touched

- **new** `bob/sub_agent/result_store.py` — store + `ProjectedResult` + projector protocol +
  default projector.
- `bob/sub_agent/tool_registry.py` — `result_projector` field; `project_gmail_search`.
- `bob/sub_agent/actions.py` — `DoneAction.result_ref`; schema `2 → 3`; envelope schema.
- `bob/sub_agent/runner.py` — store wiring, compact transcript, convergence,
  `_resolve_terminal_deliverable`, route stall/cap through it.
- `bob/sub_agent/policy.py` — `converge_on_terminal_result: bool = True`.
- `bob/context/prompt_fragments.py` — base contract line + slimmer Gmail pack.

Frontend is **unaffected** — the deliverable shape (`{component:"Mail", props}`) and the
`task_result` frame are unchanged; we only guarantee it is *populated* on more paths.

### Phases (each shippable + tested)

- **P1** `result_store.py` + `ProjectedResult` + `ToolResultStore` + default projector + unit tests.
- **P2** `gmail_search` projector + `result_projector` field + unit tests (incl. ui_registry validation of the projected card).
- **P3** Runner: store wiring + compact transcript message + tests (assert digest has no `bodyPreview`, full result never replayed).
- **P4** Runner: `_resolve_terminal_deliverable` + `DoneAction.result_ref` + route **stall/cap** through it → **fixes 2026-05-30**; tests for stall-with-deliverable, cap-with-deliverable, done-by-ref.
- **P5** Runner: convergence on terminal result + policy flag + tests (mail task converges in ≤2 iters, no stall path hit).
- **P6** Prompt/skill-pack slim + result_ref guidance + golden-prompt test update.
- **P7** Opt-in **live smoke** against `http://192.168.86.21:1234/v1` with a stub gmail tool returning a canned result: assert the real model converges to a Mail deliverable. Document model behaviour.
- **P8** Feature doc + clean commits.

### Testing strategy

- `uv run pytest` (unit: store + projector; integration: runner convergence / stall-salvage-deliverable / result_ref / compact transcript), `ruff`, `mypy`.
- The decisive regression test for 2026-05-30: *force a stall after one successful
  `gmail_search`; assert the terminal `task_result` carries a `result_payload`
  `{component:"Mail", …}` (not `null`)* — the test the old salvage path could not pass.
- Live (P7, opt-in via env, not in CI): drive `SubAgentRunner` with the **real** LLM client
  + a stub gmail tool; assert ≤2 iterations and a Mail deliverable. Exercises the new prompt
  + convergence against an actual weak local model (no Google creds needed).

## Risks & Open Questions

- **Over-eager convergence.** Force-finishing on the first terminal result is correct for
  single-shot `gmail_search` (the only real tool) but wrong for a future multi-step tool.
  Mitigation: `terminal` is per-projector (default `False` via the default projector) and
  globally gated by `policy.converge_on_terminal_result`. Multi-step tools opt out.
- **Digest too lossy for the model's own summary.** If convergence is off and the model
  writes `result_summary` from the digest, it must contain enough (subject + from + count).
  The gmail digest does. Mitigation: keep `subject`+`from.name`+`receivedAt`+`count`.
- **Schema bump 2 → 3.** `DoneAction.result_ref` is additive + optional; old payloads parse
  unchanged. The guided envelope already admits unknown fields
  (`additionalProperties: true`).
- **Privacy.** The digest drops `bodyPreview`; the projected deliverable still flows through
  `_redact_ui_payload_for_debug` to debug/JSONL sinks. A test asserts no `bodyPreview` in
  the transcript message or any `DebugEvent`.

## Out of Scope

- Cross-task raw-result persistence (a result-store table). The `task_id` + `result_payload`
  already serve recall; promote only when a real consumer appears.
- Conversation-level compression (Hermes-style preflight). The store makes per-turn context
  small enough that whole-transcript compression is unnecessary at current task lengths.
- New tools (`web_search`/`web_fetch` stay placeholders; they get the default projector).
- Any frontend change.

## Appendix — current-state evidence

- Deliverable dies on stall: `runner.py:1987-1995` (`_force_stalled_done` → text only),
  `:2022-2044` (`_emit_terminal_done` hardcodes `ui_payload=None`).
- Full blob in transcript: `runner.py:1541-1555` (`json.dumps({"tool":…, "result": result})`),
  replayed at `:1307-1310`.
- No projector hook today: `tool_registry.py:90-129` (`SubAgentToolDefinition`).
- gmail result shape: `tool_registry.py:531-538`; Mail props: `gmail/models.py:259-269`;
  Mail schema + validator: `ui_registry.py:219-302`, `:393-400`.
- Patch-thicket: `runner.py:701-1281` (the `_run` loop), `:228-301` (helpers added 05-29/30).
