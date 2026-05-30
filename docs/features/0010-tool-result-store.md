# Tool Result Store & Deterministic Deliverable Projection

Shipped on 2026-05-30 from PRD `prd/0009-tool-result-store.md`. Continues the
ship-order series (`docs/features/0001…0009`); decoupled from the PRD number.

## What it does

The flow **request → sub-task → tool call → data displayed** was unreliable on
weak local models. The 0009 codec work made the *wire format* robust; this
feature fixes the *control loop* and the *context cost* that were the real
cause of the 2026-05-30 "Mail overlay stays empty when the sub-agent stalls"
bug.

The change is one idea borrowed from the blackboard / artifact-store pattern:
**a tool result is written to a per-run store keyed by a short reference, and
the deliverable is a deterministic projection of that stored result — never a
payload the weak model has to reproduce.** Concretely:

1. **Smaller context.** A successful tool result no longer goes into the
   transcript as its full blob; the `tool` message carries a compact *digest* +
   a `result_ref` (`gmail_search#1`). A weak model stops drowning in a 2 KB
   Gmail result re-sent every turn (the bloat that drove the stall), and the
   email body never enters the transcript at all.
2. **The deliverable always survives.** On every terminal path — a clean
   `done`, a `done` that *references* the result by id, a forced stall, or a
   cap — the Mail card is rebuilt by code from the stored result. The
   2026-05-30 empty-overlay-on-stall bug is fixed at the root: the overlay is
   populated whenever the data exists, regardless of what the model emitted.
3. **The happy path converges.** A single-shot result (a mail lookup) is marked
   *terminal*; the runner then finalises `done(complete)` deterministically
   right after the tool call instead of waiting for the weak model to conclude
   (which it routinely failed to do — it spun filler `progress` until the stall
   guard fired). A Gmail search now completes in **one** LLM call.

For developers, a tool's display behaviour is now **one projector function**
(`result -> ProjectedResult`), not a 70-line prose recipe and not new runner
branches. The 2026-05-29/30 loop guards (stall force, dedup, control-char)
remain as a thin safety net but rarely fire.

## Technical surface

- **New module — `bob.sub_agent.result_store`:**
  - `ProjectedResult(digest, deliverable, summary, terminal)` — the three
    projections of a tool result plus the converge signal.
  - `ToolResultProjector = Callable[[dict], ProjectedResult]` — the per-tool
    hook (OpenClaw "provider owns the runtime hook").
  - `ToolResultStore` — the per-run blackboard: `put` (assigns
    `f"{tool}#{n}"`, runs the projector, tolerates a raising one by falling
    back to `default_projector`), `get(ref)`, `last()`.
  - `default_projector` — preserves pre-0009 behaviour for un-projected tools
    (digest == full result, no deliverable, never terminal).
- **`bob.sub_agent.tool_registry`:**
  - `SubAgentToolDefinition.result_projector` (optional) — the hook on the tool.
  - `project_gmail_search` — wired onto `gmail_search`. Builds
    `{component:"Mail", props: messages[0]}` (validated against the single
    `ui_registry` schema), a body-free 5-message-capped digest, a deterministic
    French summary, and `terminal=True` (a mail lookup is single-shot).
- **`bob.sub_agent.actions`:** `DoneAction.result_ref` (optional);
  `SUB_AGENT_SCHEMA_VERSION` bumped `2 → 3`. A `done` may reference a stored
  result instead of copying its data into `ui_payload`.
- **`bob.sub_agent.policy`:** `SubAgentPolicy.converge_on_terminal_result`
  (default `True`) — the per-deployment / per-task escape hatch for the
  convergence behaviour.
- **`bob.sub_agent.runner`:**
  - `_run` owns one `ToolResultStore`; `_handle_tool_call` writes successful
    results to it and persists the compact digest + `result_ref` (not the blob).
  - `_resolve_terminal_deliverable` / `_select_done_deliverable` — the single
    place the deliverable is chosen, preferring the store projection.
  - convergence on a terminal result; `_force_stalled_done` and the cap paths
    rebuild the deliverable from the store via `_emit_terminal_done(result_store=…)`.
- **`bob.context.prompt_fragments`:** base sub-agent prompt v6 teaches the
  `result_ref` finishing path with a tool-agnostic example; the Gmail skill
  pack v2 drops the now-automatic descriptor-building + empty-result branch,
  keeping only search construction + the tool-ERROR branches.

Frontend is **unaffected** — the deliverable shape (`{component:"Mail", props}`)
and the `task_result` frame are unchanged; this feature only guarantees the
payload is *populated* on more paths.

## Notable decisions

- **Store scope is per-run, in-memory.** The cross-*agent* data reference
  already exists (`task_id` + `task.result_payload`, recalled by
  `show_task_result`); the new `result_ref` is the *intra-loop* id. No new
  persistence table was added — that would be sprawl for no current consumer.
- **The deliverable comes from the store, the model decides *when/which*.** The
  model's `ui_payload` is still honoured for document-class markdown
  deliverables and when it hand-builds a valid descriptor; the store wins for
  tool-backed cards and as the fallback for a bare/forced `done`.
- **Convergence is per-tool + policy-gated.** Only a projection that marks
  itself `terminal` converges, and only when the policy flag is on. Multi-step
  tools mark their projection non-terminal and keep model-driven termination.
- **Privacy (0056) preserved + tightened.** The transcript digest drops
  `bodyPreview` entirely (less leak than before); the projected card still
  passes through `_redact_ui_payload_for_debug` before any debug/JSONL sink.
- **The loop-convergence guards stay as a safety net.** Stall force / dedup /
  control-char rejection still bound pathological loops, but convergence
  short-circuits the common case so they rarely fire.
- **Two robustness guards from the review pass.** (1) A *resolved* `result_ref`
  is authoritative — the runner never substitutes a different (later) tool's
  card for an explicitly-referenced result that happens to have none. (2)
  `_finalize_done` re-validates a structured deliverable against `ui_registry`
  and drops it (keeping the text) if invalid, so a buggy projector cannot ship
  malformed props to the frontend on the deterministic paths (convergence /
  stall / cap) that bypass the up-front 0065 validation.

## Known follow-ups

- **Wall-clock timeout does not persist the card.** A `timeout` maps to the
  `failed` task state, which by design does not persist `result_payload` (legacy
  `_fail` semantics). So a task cut off by the 30-min wall-clock budget loses its
  structured card on a reconnect (the iteration / token caps, which are
  `degraded` → `done`, keep it). Treating a timeout-with-data as `degraded` so
  the card persists is a deliberate semantic change, left as a follow-up.
- **Larger-model live smoke.** `scripts/live_smoke_result_store.py` is verified
  on `gemma-4-e4b`; re-run it with a 24B/35B model pre-loaded in LM Studio to
  extend the evidence table.

## Issues / phases

- P1 `feat(sub_agent): ToolResultStore blackboard + deterministic projections`
- P2 `feat(sub_agent): gmail_search result projector + tool-def hook`
- P3 `feat(sub_agent): store tool results + compact transcript digest`
- P4 `feat(sub_agent): deterministic terminal deliverable from store + result_ref` — fixes 2026-05-30
- P5 `feat(sub_agent): converge on a terminal tool result`
- P6 `feat(sub_agent): result_ref prompt guidance + slimmed Gmail skill pack`

## Verification

- **Unit:** `test_result_store.py`, `test_gmail_projector.py` (incl. the
  projected card validating against the single `ui_registry` schema).
- **Integration (`test_sub_agent_v2_runner.py`):** the decisive 2026-05-30
  regression — a stall after one `gmail_search` yields
  `task.result_payload == {component:"Mail", …}` instead of `None`; plus cap,
  done-by-`result_ref`, bare-done, model-authored-descriptor precedence,
  convergence (one LLM call), empty-result convergence, convergence-disabled,
  non-terminal-tool, and compact-transcript / no-body-leak tests.
- **e2e (`test_sub_agent_gmail_search.py`):** a converging path through the
  real connector boundary + the preserved model-driven path (`converge=False`).
- **Live smoke (`scripts/live_smoke_result_store.py`, opt-in, not in CI):**
  drives a real `SubAgentRunner` against the LM Studio server with a stub
  `gmail_search` tool, looping over several weak local models. See results
  below.

### Live smoke results (2026-05-30, server `192.168.86.21:1234`)

Driven through the real LM Studio server with the stub `gmail_search` tool.

- **`google/gemma-4-e4b` (a ~4B model — the weak-model case that stalled
  before): PASS, 4/4 runs.** On the exact previously-failing goal
  (*"Rechercher et afficher le dernier email reçu dans la boîte de
  réception"*) the model emitted a valid first-turn tool call
  `{"action":"tool_call","name":"gmail_search","args":{"label":"INBOX","max_results":1}}`
  (it picked the INBOX fallback from the slim skill pack), the runner converged
  in **1 LLM call (~1–5 s)**, the transcript carried only the compact digest
  (`bodyPreview` never present), and `task.result_payload` was the populated
  `{component:"Mail", …}` card. A sender-specific goal (*"le dernier mail de
  Holyana Callejon"*) likewise produced `{"args":{"from_name":"Holyana
  Callejon", …}}` and converged with the card.
- **Mid / large models (`devstral-small-2`, `magistral-small` ~24B,
  `qwen3.6-35b-a3b`): not exercised** — they exceeded this server's
  model-load window (the `/chat/completions` connection dropped during the
  cold load). This is a server-capacity limit, not an architecture issue: the
  `/v1/models` endpoint and the 4B model both respond fine, and the flow is
  model-agnostic. Re-run `scripts/live_smoke_result_store.py` once a larger
  model is pre-loaded in LM Studio to extend the table.

The decisive takeaway: the request → task → tool → **data displayed** flow now
completes deterministically in a single LLM call on the weakest local model,
with the Mail card populated — the failure mode the 2026-05-30 investigation
reported is gone.
