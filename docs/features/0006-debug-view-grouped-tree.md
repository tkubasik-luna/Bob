# Debug View — Grouped Tree

Shipped on 2026-05-25 from PRD `prd/0006-debug-view-grouped-tree.md`.

## What it does

Restructures the `Cmd+Shift+D` debug feed from a flat chronological stream into a hierarchical tree. Each user input becomes a collapsible turn node grouping its sub-tasks, fused LLM calls (start+end merged with model / latency / tokens in the header), and bare events. Sub-tasks nest under their spawning task. The current live turn auto-expands, prior turns auto-collapse, and on reconnect the snapshot replay opens only the most recent turn. Turn coloring (djb2) shows as a left border + header tint instead of a per-row chip.

## Technical surface

- **Backend** — `debug_log.py` gains `current_task_id: ContextVar[str | None]` + `start_task()` helper (reset-token symmetric to `start_turn`). `DebugEvent` carries `parent_task_id`, captured automatically by `emit_debug` from the ContextVar. `sub_agent_runner.py` wraps each sub-task in `start_task` so nested `asyncio.create_task` spawns inherit the right parent. Wire envelope `/ws/debug` JSON now always carries `parent_task_id`.
- **Frontend** — new `lib/groupEvents.ts` (pure tree builder, sealed union `TurnNode | TaskNode | LlmCallNode | EventNode`), `hooks/useGroupedEvents.ts` (memoized wrapper), `components/debug/DebugTree.tsx` (recursive renderer), `components/debug/HighlightedJson.tsx` (extracted shared JSON view). `lib/debugFilter.ts` gains `pruneEmptyNodes`; counts recompute post-filter. `lib/turnColor.ts` gains `turnBorderColor` + `turnHeaderTint`. Expand state lives in `DebugView` as a `Map<nodeId, boolean>` with a sibling manual-override `Set`, keyed `turn:${id}` / `task:${id}` / `llm:${corrId}`. Tail-scroll targets the inner-most last node via ref, gated on `filteredCount` ticking (manual expand never scrolls).
- **No new migrations / no new events / no new endpoints.**

## Notable decisions

- `parent_task_id` is read exclusively from the ContextVar in `emit_debug`; no explicit kwarg. Forging a parent for tests/fixtures means setting the ContextVar.
- `turnHeaderTint` uses `hsla(h, 60%, 95%, 0.10)` — alpha wash, not the literal `hsl(...95%)` from the PRD — because the feed background is near-black (`#02060e`) and an opaque 95%-lightness fill would dominate the row.
- Snapshot-replay initial state: collapsed-except-max-ts is committed once via a `snapshotInitDoneRef` on the first all-`replayed` batch. Subsequent live events fall through to the live tracker.
- Manual expand override uses a separate `Set` (not just "key present in Map") so the auto-flow can keep writing into the Map without spuriously locking nodes.
- LLM fusion key is `correlation_id`. In-flight calls (start with no end) render with `⏳` instead of latency.
- Orphan events (no `turn_id`, no `parent_task_id`) remain inline at root, ordered chronologically among the turn nodes — preserving the "system boot / proactive flusher / TTS background" visibility from feature 0005.

## Issues

- `issues/0043-debug-view-parent-task-id.md` — backend `parent_task_id` ContextVar + wire — commit b8d5384
- `issues/0044-debug-view-grouped-tree.md` — grouped tree view + LLM fusion + pruneEmptyNodes — commit 52c395c
- `issues/0045-debug-view-tree-polish.md` — polish: border tint, auto-expand, replay collapsed, tail-scroll — commit 21b88ed
