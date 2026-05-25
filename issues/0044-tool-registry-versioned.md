## Parent

`prd/0006-jarvis-v2-context-overhaul.md`

## What to build

Replace the current hardcoded tool-dispatch switch in the orchestrator with a `ToolRegistry` + `ToolDispatcher` pattern, with versioned tool definitions and Pydantic-validated arguments. Migrate the existing `spawn` / `forward` / `cancel` tools into registry entries without changing their behavior. Add a `lineage: list[task_id]` column to the `tasks` table (empty list for existing rows; future `replan_task` will populate it). Every dispatch emits a structured `jarvis.route` event so future "why did Jarvis chat instead of spawning?" debugging is grep-friendly without parsing prose.

Validation of tool name + argument shape happens in the dispatcher and returns a structured error result. Unknown tool name and unknown task_id both route through the same error path (no silent dispatch). The actual retry/degrade behavior on validation error is wired in 0048 — for this slice, an unknown name simply returns an error result that the orchestrator currently surfaces the same way the legacy code does.

This slice does not introduce new user-visible features. It locks the contract that 0045, 0047, 0048, and 0050 all depend on.

## Acceptance criteria

- [ ] `ToolRegistry` + `ToolDefinition` (versioned, e.g. `v1.spawn_task`) + `ToolDispatcher` exist.
- [ ] Each tool's argument schema is Pydantic-validated at dispatch.
- [ ] `spawn`, `forward`, `cancel` tools migrated into the registry; behavior unchanged (existing integration tests still pass).
- [ ] `tasks.lineage` column added via migration; defaulted to `[]` for existing rows.
- [ ] Unknown tool name + unknown task_id both produce a structured error result through a single code path.
- [ ] `jarvis.route` structured event emitted on every dispatch (including direct-reply path once that exists in 0047 — for now, on every tool call).
- [ ] Contract tests covering: known-tool dispatch happy path, unknown-tool error, schema-invalid args error.
- [ ] Golden prompt snapshots still pass unchanged.

## Blocked by

`issues/0043-context-entry-foundation.md`
