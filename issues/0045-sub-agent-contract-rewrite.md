## Parent

`prd/0006-jarvis-v2-context-overhaul.md`

## What to build

Rewrite the sub-agent execution layer for structured concurrency, a versioned action contract, and cooperative cancellation with a hard-kill fallback.

Define a versioned `SubAgentAction` schema with three actions per iteration: `progress(thought)`, `tool_call(name, args)`, and `done(result_summary, ui_payload?, status, reason_code, cost)`. Statuses include at least `complete`, `degraded`, `failed`, `cancelled`, `timeout`. `reason_code` is drawn from a versioned registry (concrete registry ships in 0048; for this slice the codes used here live in the registry already).

Wrap all running sub-agents in a single `asyncio.TaskGroup` so a Jarvis crash cannot leak background coroutines. Implement cancellation as cooperative checkpoints at iteration boundary and tool-call boundary, with a 2 s grace timeout followed by a hard-kill path; document this contract explicitly in the runner module.

Add a `SubAgentPolicy` config object centralising max iterations, wall-clock budget, per-call token cap, and per-task-type overrides. Implement an `AddendumQueue` (`asyncio.Queue` per task) which is drained ONLY at iteration boundaries — the queue is wired but no user-facing tool fills it yet (that ships in 0050). Initial sub-agent tool registry contains `web_search` and `web_fetch`.

This slice does not change Jarvis-side user behavior. It establishes the runner contract that 0048 (validation) and 0052 (events) build on, and unblocks future memory-extraction sub-agents.

## Acceptance criteria

- [ ] `SubAgentAction` schema versioned (`schema_version: 1`) with `progress`, `tool_call`, `done`.
- [ ] `done` carries `result_summary`, optional `ui_payload`, `status`, `reason_code`, `cost`.
- [ ] All sub-agents run inside one `asyncio.TaskGroup`; killing the orchestrator cleans up running tasks deterministically.
- [ ] Cancellation checkpoints at iteration boundary AND tool-call boundary.
- [ ] 2 s grace timeout after cancel → hard-kill fallback. Behavior documented in the module docstring.
- [ ] `SubAgentPolicy` config object centralises max iterations, wall-clock, token cap, per-task-type overrides.
- [ ] `AddendumQueue` exists per task, drained only at iteration boundaries; no consumer yet (wired for 0050).
- [ ] `web_search` and `web_fetch` registered as initial sub-agent tools.
- [ ] Scripted-fake-LLM integration tests cover: iteration cap → forced `done(degraded)`, wall-clock cap → forced `done(timeout)`, token cap → forced `done(degraded)`, cancel within grace → `done(cancelled)`, cancel beyond grace → hard-kill recorded.
- [ ] Existing task lifecycle smoke test passes end-to-end with the new runner.

## Blocked by

`issues/0044-tool-registry-versioned.md`
