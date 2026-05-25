## Parent

`prd/0006-jarvis-v2-context-overhaul.md`

## What to build

Unify the event channels and ship the per-task in-progress overlay UI.

Collapse the existing `ws_events.emit` and `emit_debug` producers into a single `EventBus` with one event shape: `DebugEvent` extended with a `task_id` field (re-using the existing ContextVar plumbing). All emitters route through this single producer — no parallel paths.

Expose `/ws/task/{task_id}` as a FILTERED subscription over the same ring-buffer producer; not a new topic, not a new persistent store. Reflections stay ring-buffer-only — sub-agents that die on process restart take their thoughts with them by design, since the runs themselves don't survive.

When the frontend overlay opens for a task, the WS session uses a snapshot-then-tail protocol in ONE session: first frame is a snapshot of currently-buffered events for that `task_id`; subsequent frames are the live tail. No HTTP+WS race.

Bound retention by bytes and age in `EventRetentionPolicy` (not by count + kind awareness — that was rejected during PRD review).

Frontend behaviors:
- Clicking a running task opens an overlay subscribing to `/ws/task/{id}`; renders the reflection timeline live (`thought`, `tool_invoke`, `tool_result`, `addendum_received`, `status_change`).
- Clicking a finished task opens an overlay rendering its `ui_payload` (Markdown component).
- Clicking a finished task with no `ui_payload` opens an empty-state overlay (clear, not blank).

## Acceptance criteria

- [ ] `EventBus` is the single producer; `ws_events.emit` and `emit_debug` are collapsed into it; no duplicate emission paths remain (grep-cleaned).
- [ ] `DebugEvent.task_id` field added; populated from the existing ContextVar.
- [ ] `/ws/task/{task_id}` is a filter over the existing ring buffer, not a new topic or table.
- [ ] Snapshot-then-tail protocol implemented in a single WS session; no HTTP-then-WS upgrade race.
- [ ] `EventRetentionPolicy` bounds by bytes + age, configurable.
- [ ] Reflection events emitted by sub-agents: `thought`, `tool_invoke`, `tool_result`, `addendum_received`, `status_change`.
- [ ] Frontend: clicking a running task opens the in-progress overlay and shows live reflections; matches PRD UX.
- [ ] Frontend: clicking a finished task with `ui_payload` renders it as Markdown.
- [ ] Frontend: clicking a finished task without `ui_payload` shows a clear empty-state overlay.
- [ ] Tests: filtered subscription receives only events for the requested `task_id`; snapshot+tail ordering deterministic; multi-client multi-overlay scenario does not cross-leak events.
- [ ] Test: process restart causes running sub-agents to die and their reflection events to be lost (documented, asserted) while `task_completed` history survives via existing `tasks` / `task_messages` tables.

## Blocked by

`issues/0045-sub-agent-contract-rewrite.md`
