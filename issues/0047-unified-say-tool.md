## Parent

`prd/0006-jarvis-v2-context-overhaul.md`

## What to build

Unify all Jarvis emission as tool calls. Add a `say(speech: str, ui: object | null)` tool to the tool registry. Update the Jarvis system prompt to instruct: every turn MUST be exactly one tool call (no free-form text). Remove the existing `_reply_with_structured_response` path entirely.

After this slice, every Jarvis turn dispatches through `ToolDispatcher`. Direct replies become `say` tool calls; task operations remain `spawn` / `forward` / `cancel` (those become `spawn_task` / `addendum_task` / `replan_task` / `cancel_task` in 0050). The `jarvis.route` structured event introduced in 0044 now logs on every turn, including direct replies — previously a blind spot.

This unification is a prerequisite for streaming (0049): a known-shape tool-call argument string is the only thing the partial-JSON parser will be asked to parse.

User-visible behavior should remain equivalent to today's direct replies — the user does not see the change, but every reply is now a structured, validated, version-tagged emission.

## Acceptance criteria

- [ ] `SayTool` exists in the registry with versioned schema (`v1.say`) and Pydantic-validated args.
- [ ] Jarvis system prompt updated: closed instruction that every turn is a tool call.
- [ ] `_reply_with_structured_response` and the free-form JSON parsing code path removed.
- [ ] Every Jarvis turn ends in exactly one dispatched tool call (asserted in integration tests).
- [ ] `jarvis.route` structured event now logs on `say` calls too (not just task ops).
- [ ] Integration test: simple-question → Jarvis emits `say(speech, ui=null)` → user sees identical reply to pre-migration behavior.
- [ ] Integration test: an attempted free-form reply from the fake LLM is treated as validation failure (will route through 0048's handler once 0048 lands; for this slice, it errors as in 0044's unknown-shape path).
- [ ] Golden prompt snapshots updated.

## Blocked by

`issues/0044-tool-registry-versioned.md`, `issues/0046-bounded-context-providers.md`
