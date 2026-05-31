import type { AgentTimelineItem } from "../store/activityFeedStore";
import type { TaskState } from "../types/ws";

/**
 * Reasoning-streaming PRD — derive a lane's CURRENT phase from the live signals
 * the store actually holds (timeline + terminal state).
 *
 * The Design-Mockup phases (loading → reading → thinking → tool → writing →
 * done) assume the native LM-Studio transport, which Bob does NOT use (it would
 * break Jarvis tool-calling + the sub-agent guided-JSON invariant — see the
 * reasoning-streaming handoff). So `loading` / `reading` / `writing` have no
 * data source. We derive the HONEST subset observable on `/v1`:
 *
 *   - `error`    — the task terminated failed.
 *   - `done`     — the task terminated done.
 *   - `tool`     — the trailing timeline item is a chip still `running`
 *                  (a tool call in flight).
 *   - `thinking` — reasoning has streamed and nothing terminal/in-flight.
 *   - `waiting`  — spawned but nothing streamed yet (the impatience zone —
 *                  rendered as an indeterminate spinner, never a % bar).
 *
 * Pure + exported so the (small, finite) state machine is unit-testable without
 * React. The phase only ever advances while running; the terminal states are
 * owned by `finishedByAgent`, never inferred away.
 */
export type AgentPhaseKey = "waiting" | "thinking" | "tool" | "done" | "error";
export type AgentPhaseStatus = "running" | "done" | "error";

export type AgentPhase = {
  key: AgentPhaseKey;
  status: AgentPhaseStatus;
};

export function deriveAgentPhase(
  timeline: readonly AgentTimelineItem[] | undefined,
  finished: TaskState | undefined,
  opts?: {
    /** Jarvis has no `task`, so no terminal `finishedByAgent` entry — it would
     * otherwise stay `thinking` forever. The orchestrator brackets each Jarvis
     * turn with the `thinking` start/end WS event (the chat store's
     * `isWaitingResponse`). Pass `turnActive: false` once the turn ended so the
     * persistent lane settles to `done` between turns and re-activates on the
     * next turn's reasoning. Omit (or `true`) for agents whose lifecycle is the
     * `finished` map. */
    turnActive?: boolean;
  },
): AgentPhase {
  if (finished === "failed") return { key: "error", status: "error" };
  if (finished === "done") return { key: "done", status: "done" };

  const last = timeline && timeline.length > 0 ? timeline[timeline.length - 1] : undefined;
  if (last && last.kind === "chip" && last.status === "running") {
    return { key: "tool", status: "running" };
  }
  // Turn-bracketed agent (Jarvis): the turn ended → settle to done even though
  // there's no terminal `finished` state. A fresh reasoning delta next turn
  // flips `turnActive` back true → thinking.
  if (opts?.turnActive === false) return { key: "done", status: "done" };

  const sawReasoning = !!timeline?.some((it) => it.kind === "reasoning");
  if (sawReasoning) return { key: "thinking", status: "running" };
  return { key: "waiting", status: "running" };
}

/** Display metadata per phase — label + the tint CSS var the pip/progress use.
 * Tints reuse the HUD state palette so the lane reads in the same visual
 * language as the sphere. */
export const PHASE_META: Record<AgentPhaseKey, { label: string; tint: string }> = {
  // `waiting` is the running-but-no-reasoning gap (a guided-JSON sub-agent
  // generating its action — schema suppresses reasoning, so no token stream).
  // It IS working, not idle, so the label says so.
  waiting: { label: "Traitement…", tint: "var(--accent)" },
  thinking: { label: "Réflexion", tint: "#a89bc0" },
  tool: { label: "Outils", tint: "#c2a06a" },
  done: { label: "Terminé", tint: "#8fba9f" },
  error: { label: "Échec", tint: "var(--err)" },
};
