# Jarvis Orchestrator (Multi-Task Assistant)

Shipped on 2026-05-22 from PRD `prd/0003-jarvis-orchestrator.md`.

## What it does

Bob becomes a single-interlocutor personal assistant: the user only ever
talks to **Jarvis**, a configurable-personality LLM whose conversation now
survives restarts (SQLite-backed thread). Jarvis decides whether to answer
directly or delegate long-running work to **sub-agents** that run in the
background. The user sees those sub-tasks in a right-hand sidebar with live
state, can open a drawer for the full transcript, and can dismiss or cancel
them. When a sub-task ends or needs input, Jarvis pushes a proactive
message in the main chat with its own tone — the user never speaks to a
sub-agent directly.

## Technical surface

- **Persistence**: SQLite at `${BOB_DATA_DIR}/bob.db`. Migrations under
  `backend/src/bob/db/migrations/` (idempotent runner). Tables:
  `jarvis_messages`, `tasks`, `task_messages` (+ `dismissed` column).
  Personality file at `${BOB_DATA_DIR}/jarvis.md`, bootstrapped on first
  boot.
- **LLM tool calling** (`bob.llm_client.LLMClient.complete`): unified API
  for Claude CLI + LM Studio. Shared types in `bob.llm.types`
  (`ToolDefinition`, `ToolCall`, `LLMResponse`). Per-role backends via
  `JARVIS_BACKEND` / `SUBAGENT_BACKEND` env vars (fall back to
  `LLM_PROVIDER`).
- **Orchestrator** (`bob.orchestrator.Orchestrator`, singleton): every
  user turn first asks Jarvis with tools `[spawn_subtask,
  forward_to_subtask, cancel_subtask]`. If a tool is called, the task is
  routed to the scheduler; otherwise the existing structured-output chat
  path produces the reply. The active waiting-for-input tasks are
  injected into the system prompt so Jarvis knows which `task_id` to
  forward / cancel.
- **TaskScheduler** (`bob.task_scheduler.TaskScheduler`): caps running
  sub-tasks at `MAX_RUNNING_TASKS` (default 3), queues the surplus as
  `pending`, promotes the oldest pending when a slot frees. On boot
  coerces any leftover `running` task back to `pending` and re-promotes
  under the cap. Owns the runner asyncio.Task handles and implements real
  cancellation (`asyncio.Task.cancel()` + state finalization).
- **SubAgentRunner** (`bob.sub_agent_runner.SubAgentRunner`): one-shot
  per resumable iteration. Rebuilds the message log from `task_messages`
  every run so resumes see prior turns. Supports actions `done(result)`,
  `ask_user(question)`, `progress(status)` (hard cap of 10 consecutive
  progress messages without `done`/`ask_user`).
- **EventBus** (`bob.event_bus.EventBus`): asyncio pub/sub with topics
  `task_state_changed`, `task_message_added`.
- **ProactivityHandler**: subscribes to `task_state_changed`. On
  `waiting_input` triggers `generate_proactive_message` (paraphrases the
  raw sub-agent question via Jarvis). On `done` triggers
  `generate_done_synthesis` (2-3 line summary). Both pushes are gated by
  Jarvis state + user-typing signal.
- **Proactive queue**: orchestrator-internal asyncio queue + flusher
  background task. Buffers proactive events while Jarvis is `thinking`
  or the user is `typing`; flushes FIFO once both gates are clear. The
  frontend emits debounced `client_typing` WS events.
- **WS contract** additions:
  - Server → client: `task_created`, `task_updated`, `task_result`,
    `task_message`, `task_messages_snapshot`. `assistant_msg` gains
    `proactive: bool` (default false).
  - Client → server: `dismiss_task`, `cancel_task`,
    `request_task_messages`, `client_typing`.
- **Frontend**: `ChatView` splits into chat + right-hand sidebar.
  `TaskSidebar` lists `TaskCard`s with state-color dot, title,
  timestamp, progress status (italic grey under the title), "En attente"
  label for pending. Hover-revealed × buttons: cancel (× icon) on
  non-terminal cards, dismiss (eye-off icon) on `done`/`failed` cards.
  `TaskDrawer` (native `<dialog>`) shows goal + full transcript with
  action badges + result/reason; fetches snapshot on open and stays in
  sync via live `task_message` events. Proactive `assistant_msg`s render
  with an amber left-border + "Bob · auto" label and do not interrupt
  in-flight audio.

## Notable decisions

- The orchestrator is a true module-level singleton so the WS handler,
  the proactivity handler and the typing signal share queue + state.
  `set_default_orchestrator(None)` is called on lifespan shutdown so test
  lifespans don't leak instances.
- The plain-text (no-spawn) path makes two LLM calls per turn: a
  `complete()` for the spawn decision, then a `chat()` with the
  structured-output schema for the actual reply. Accepted MVP cost.
- Cancellation **really** interrupts the runner via
  `asyncio.Task.cancel()` rather than polling a flag. A `_cancelling`
  set keeps the runner's done-callback from double-transitioning state
  after an explicit cancel.
- Sub-task slot accounting: `waiting_input` does NOT occupy a slot. When
  a sub-agent emits `ask_user`, its asyncio task ends, the scheduler's
  done-callback frees the slot, and the user's forwarded answer
  re-acquires a slot via `TaskScheduler.resume`.
- Failure reasons are persisted in `tasks.result` (same column as
  success results); the frontend disambiguates by `task.state`.
- `dismissed` tasks remain in SQLite for traceability — the WS replay
  filter just hides them via `list_tasks(include_dismissed=False)`.
- The proactive flusher only gates on `thinking` + `typing`, not on
  `speaking`. TTS-coincident proactive messages may interleave audibly
  but cannot be lost.

## Issues

- `issues/0015-jarvis-foundation-sqlite-prompt.md` — Jarvis foundation
  (SQLite thread + editable personality + migrations runner) — commit
  `faae73c`.
- `issues/0017-llm-tool-calling-abstraction.md` — unified
  `LLMClient.complete()` tool-calling API — commit `dc74888`.
- `issues/0016-task-store.md` — `TaskStore` + tasks/task_messages tables
  + state machine — commit `de50df8`.
- `issues/0018-first-task-spawn-end-to-end.md` — Orchestrator +
  `SubAgentRunner` 1-shot + `spawn_subtask` — commit `7a1b7b1`.
- `issues/0019-sidebar-ui-shell-ws-events.md` — sidebar UI + `task_*` WS
  events — commit `9ad16ef`.
- `issues/0020-task-scheduler-cap-queue.md` — `TaskScheduler` cap +
  queue + boot recovery — commit `19debc8`.
- `issues/0021-multi-turn-ask-user-forward.md` — multi-turn `ask_user` +
  `forward_to_subtask` tool + EventBus — commit `31c66b9`.
- `issues/0024-task-drawer-dismiss.md` — `TaskDrawer` transcript +
  dismiss button — commit `2188bd6`.
- `issues/0022-progress-events-live.md` — sub-agent `progress(status)`
  action with live sidebar status — commit `e6e21bb`.
- `issues/0023-cancellation-two-paths.md` — two-path cancellation
  (sidebar × + `cancel_subtask` tool) — commit `1e682da`.
- `issues/0025-proactive-done-synthesis.md` — proactive done synthesis +
  thinking/typing queue — commit `02a9663`.
