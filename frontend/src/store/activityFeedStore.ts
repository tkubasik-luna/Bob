import { create } from "zustand";
import type { AgentActivityMsg, ReasoningDeltaMsg } from "../types/ws";

/**
 * PRD 0011 — agent-activity feed store.
 *
 * Issue 0069 (tracer bullet) accumulated only the live `reasoning_delta` stream
 * per `agent_ref`. Issue 0071 adds the curated activity CHIPS (`agent_activity`
 * events) and — crucially — interleaves them with the reasoning IN ORDER, so
 * the `AgentBlock` can render a single chronological flow (reasoning text
 * segments + chips) exactly as they arrived on the wire.
 *
 * State design (forward-looking for 0073 lanes / 0075 sliding window):
 * each agent owns an ORDERED `AgentTimelineItem[]`. A reasoning delta either
 * extends the trailing reasoning segment (so a burst of deltas coalesces into
 * one text block) or starts a new one if the last item is a chip. A chip is
 * appended as its own item. Order is preserved, so a later issue can lane the
 * timelines side-by-side or window/collapse them without re-deriving order.
 *
 * NOT in this slice: lanes UI (0073), sliding window / collapse (0075), the
 * Jarvis block (0072) and the panel layout (0076). The per-agent timeline shape
 * is deliberately the seam those build on.
 */

/** A contiguous run of reasoning text inside an agent's timeline. */
export type ReasoningItem = {
  kind: "reasoning";
  text: string;
};

/** A discrete activity chip inside an agent's timeline. Mirrors the wire
 * `agent_activity` event minus the routing `type` / `agent_ref`. */
export type ChipItem = {
  kind: "chip";
  /** The chip's taxonomy kind (`tool_call`, `stall`, …). */
  activityKind: AgentActivityMsg["kind"];
  label: string;
  status: AgentActivityMsg["status"];
};

/** One ordered entry in an agent's interleaved reasoning + chip timeline. */
export type AgentTimelineItem = ReasoningItem | ChipItem;

type ActivityFeedState = {
  /** Ordered, interleaved timeline (reasoning segments + chips) per `agent_ref`.
   * The single source the `AgentBlock` renders from. */
  timelineByAgent: Record<string, AgentTimelineItem[]>;
  /** Append a `reasoning_delta` suffix, coalescing into the trailing reasoning
   * segment (or starting a new one after a chip). */
  appendReasoningDelta: (msg: ReasoningDeltaMsg) => void;
  /** Append an activity chip as its own ordered timeline item. */
  appendActivity: (msg: AgentActivityMsg) => void;
  /** Drop a single agent's timeline (e.g. when its task terminates). */
  clearAgent: (agentRef: string) => void;
  /** Wipe all timelines. */
  reset: () => void;
};

export const useActivityFeedStore = create<ActivityFeedState>((set) => ({
  timelineByAgent: {},
  appendReasoningDelta: (msg) =>
    set((state) => {
      const existing = state.timelineByAgent[msg.agent_ref] ?? [];
      const last = existing[existing.length - 1];
      let next: AgentTimelineItem[];
      if (last && last.kind === "reasoning") {
        // Coalesce into the trailing reasoning segment (immutably).
        next = [...existing.slice(0, -1), { kind: "reasoning", text: last.text + msg.delta }];
      } else {
        // First delta, or the last item is a chip — start a new segment.
        next = [...existing, { kind: "reasoning", text: msg.delta }];
      }
      return {
        timelineByAgent: { ...state.timelineByAgent, [msg.agent_ref]: next },
      };
    }),
  appendActivity: (msg) =>
    set((state) => {
      const existing = state.timelineByAgent[msg.agent_ref] ?? [];
      const chip: ChipItem = {
        kind: "chip",
        activityKind: msg.kind,
        label: msg.label,
        status: msg.status,
      };
      return {
        timelineByAgent: {
          ...state.timelineByAgent,
          [msg.agent_ref]: [...existing, chip],
        },
      };
    }),
  clearAgent: (agentRef) =>
    set((state) => {
      if (!(agentRef in state.timelineByAgent)) return state;
      const { [agentRef]: _removed, ...rest } = state.timelineByAgent;
      return { timelineByAgent: rest };
    }),
  reset: () => set({ timelineByAgent: {} }),
}));
