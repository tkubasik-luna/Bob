# Agent Activity Feed (live reasoning in the HUD)

Shipped on 2026-05-31 from PRD `prd/0011-agent-activity-feed.md`.

## What it does

During long-running work, the Sphere HUD now shows a live activity feed instead of leaving the user staring at a silent sphere. Each agent — Jarvis and every concurrent sub-task — gets its own block that streams its reasoning token-by-token (like a chat LLM "thinking"), with tool calls and incidents (stalls, caps, retries, validation failures) interleaved inline as chips. The feed lives in a right-edge collapsable panel that auto-expands on activity and collapses to a narrow rail (active-agent badges + count). When a task finishes, its block collapses to a summary with a button to open the result overlay (Mail/Sections) and an expand to re-read the reasoning. The feed keeps the whole session in scrollback and rehydrates finished blocks from the persisted TaskStore on reload.

## Technical surface

- **New WS events (user-facing, on `/ws/chat`)**: `reasoning_delta` (`{agent_ref, delta}`) and `agent_activity` (`{agent_ref, kind, label, status}`). `agent_ref` is a `task_id` or `"jarvis"`. Curated and redacted — separate from the debug ring buffer.
- **Backend modules**:
  - `StreamChunk` gained a `reasoning` kind (`reasoning_delta` field); `LMStudioClient` surfaces `delta.reasoning_content` in streaming.
  - `ReasoningStreamReader` (`backend/src/bob/sub_agent/reasoning_stream.py`) — streams a sub-agent LLM call, separates the reasoning channel from the `content`, exposes a `degraded` flag.
  - `ActivityProjector` (`backend/src/bob/sub_agent/activity_projector.py`) — pure projection of internal events → curated chip descriptors, with Mail redaction.
  - `SubAgentRunner` switched its per-iteration call to the streamed path; `Orchestrator` emits the Jarvis lane (reasoning + orchestration chips + duplicated final answer).
- **Frontend (Sphere HUD window `?ui=new`)**:
  - `activityFeedStore` — per-agent timelines (`timelineByAgent`), lane order (`agentOrder`), lifecycle (`finishedByAgent`), rAF-coalesced reasoning deltas, `rehydrateFromTasks`.
  - `AgentBlock` (active sliding-window + collapsed summary), `AgentLanes` (stacking), `AgentActivityPanel` (right-edge collapsable rail). `HudTasks` removed from the HUD.
  - Result button reuses the existing `SectionsOverlay` dispatcher via `result_payload`.

## Notable decisions

- **Reasoning is cosmetic.** The streamed `reasoning_content` never feeds action parsing — the SubAgentAction is always parsed/validated from the final aggregated `content` (guided-JSON intact). Zero correctness impact (locked invariant, regression-tested).
- **Robust fallback.** Models that emit no `reasoning_content` degrade to narrated steps (the sub-agent's progress thought rides the same `reasoning_delta` channel); the feed is never empty. Per-agent / per-iteration, not global.
- **Curated chip taxonomy.** Tool calls + ask_user + salient incidents become chips; passing validations are suppressed (no per-validation noise).
- **HUD-only scope.** The feed lives in the Sphere HUD window. The legacy `ChatView` + `TaskSidebar` + `TaskDrawer` are intentionally left intact (cohabiting). Removing the legacy surface is separate, future work.
- **Rehydrate gotcha.** `markAgentFinished` alone does not make a lane render — `agentOrder` must also be populated. On reconnect the backend replays `task_*` frames (`replayed: true`); `rehydrateFromTasks` rebuilds finished lanes (state/summary/result, no live reasoning replay).
- **Dependency**: live token streaming depends on the endpoint exposing `reasoning_content` (Qwen3, DeepSeek-R1, etc. in LM Studio). Non-reasoning models still get chips + narrated steps.

## Issues

- `issues/0069-reasoning-stream-tracer.md` — live reasoning stream tracer — commit d104354
- `issues/0071-activity-chips-projector.md` — activity chips taxonomy (ActivityProjector) — commit e614527
- `issues/0070-fallback-narrated-steps.md` — narrated-steps fallback — commit 83db4bd
- `issues/0073-multi-agent-lanes-throttling.md` — multi-agent lanes + delta coalescing — commit be9a1de
- `issues/0075-sliding-window-reasoning.md` — sliding-window + voir-tout — commit f96191c
- `issues/0072-jarvis-block.md` — Jarvis activity lane — commit 35548a1
- `issues/0074-block-lifecycle-collapse.md` — block lifecycle collapse + result access — commit d64d49e
- `issues/0076-collapsable-side-panel.md` — collapsable agent activity panel (replaces HudTasks) — commit f05ca7a
- `issues/0077-retention-rehydrate-remove-drawer.md` — session retention + rehydrate from TaskStore — commit f4396f6
