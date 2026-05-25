## Parent

`prd/0006-jarvis-v2-context-overhaul.md`

## What to build

Make Jarvis aware of active tasks and deliver their results with recency-correct phrasing. This is the user-facing slice that turns the v1 task model into the v2 lifecycle.

Add a `StateBlockProvider` that builds a STATE entry list each turn: `{id, title (≤8 words), state, last_update_1liner (≤120 chars), delivered_at_turn, last_event_id, lineage}`. Active set = (not-done tasks) ∪ (done tasks within last K user turns AND `delivered_at_turn` set). `age_min` is always recomputed at assembly, never persisted. Hard caps via `StatePolicy`: per-field char limits + max entry count. Token budget is asserted in tests, not enforced at runtime.

Add an injectable `EvictionStrategy` (default order: oldest delivered-done → oldest failed → oldest awaiting → never drop running) so future tuning is not a code change.

Implement `RecencyPolicy` as a struct (`age_turns`, `age_seconds`, `topic_overlap`) computed at turn-assembly time. The Jarvis prompt is instructed to use this signal to pick phrasing — "Voilà X" (active) vs "Tu m'avais demandé X, voilà..." (stale). The recency rule is not hardcoded strings in dispatch code.

Add the new task-operation tools to the registry: `SpawnTaskTool`, `AddendumTaskTool(task_id, info)` (no restart — info pushed into the sub-agent's `AddendumQueue` wired in 0045), `ReplanTaskTool(task_id, new_goal)` (cancel + respawn with `lineage = [old_id, ...]`; old task marked `superseded`), `CancelTaskTool`. Concurrency cap = 3 running, queue cap = 5 pending; overflow → tool error → Jarvis emits clarifying speech.

When a sub-agent finishes, materialise a synthetic `task_completed` `ContextEntry` (`recency` computed at next assembly, NOT at emit). Debounce pending completions ~300 ms so multiple done tasks can be batched into one Jarvis utterance.

## Acceptance criteria

- [ ] `StateBlockProvider` builds STATE entries with all PRD fields; char limits enforced; max-entry count enforced.
- [ ] `age_min` is recomputed at assembly time; never read from persistence.
- [ ] `EvictionStrategy` is injectable; default order matches PRD; `running` is never evicted.
- [ ] `RecencyPolicy` struct computed at turn-assembly time; deterministic given identical inputs.
- [ ] `SpawnTaskTool`, `AddendumTaskTool`, `ReplanTaskTool`, `CancelTaskTool` registered with versioned schemas.
- [ ] `AddendumTaskTool` pushes info into the sub-agent's `AddendumQueue`; the sub-agent reads it at the next iteration boundary; no restart occurs.
- [ ] `ReplanTaskTool` cancels old task + spawns new one with `lineage` chain; old task marked `superseded`.
- [ ] Concurrency cap (3 running) + queue cap (5 pending) enforced; overflow returns tool error; Jarvis emits clarifying speech telling the user to cancel one.
- [ ] Task completion materialises a `task_completed` `ContextEntry`; recency computed at assembly, asserted in test.
- [ ] Pending-completions debounce ~300 ms; multiple completions within window are batched into one Jarvis utterance (asserted in integration test).
- [ ] `delivered_at_turn` is set after delivery; same result is never announced twice (asserted).
- [ ] Integration tests cover the full PRD test scenarios: spawn + active delivery; stale delivery with reminder; addendum mid-flight; replan mid-flight; cancel; queue overflow → degrade speech; batched-completion within debounce.
- [ ] Pure tests for `EvictionStrategy` ordering and `RecencyPolicy` decisions.
- [ ] Golden prompt snapshots updated for STATE-aware prompts.

## Blocked by

`issues/0046-bounded-context-providers.md`, `issues/0047-unified-say-tool.md`
