import { create } from "zustand";
import type { ReasoningDeltaMsg } from "../types/ws";

/**
 * PRD 0011 / issue 0069 — agent-activity feed store (tracer bullet).
 *
 * Aggregates live `reasoning_delta` WS events by `agent_ref` (the running
 * sub-task's id) into a per-agent reasoning buffer. The minimal `AgentBlock`
 * component reads `reasoningByAgent[agentRef]` to render the streaming
 * chain-of-thought token-by-token while a sub-task runs.
 *
 * Deliberately thin: multi-agent lanes, chips, lifecycle, retention and the
 * full panel layout are later issues (0070+). This slice only accumulates the
 * reasoning text so it can be made visible somewhere in the HUD.
 */
type ActivityFeedState = {
  /** Accumulated reasoning text per `agent_ref`. Grows on every delta. */
  reasoningByAgent: Record<string, string>;
  /** Append a `reasoning_delta` suffix to the agent's buffer (creating it on
   * the first delta). */
  appendReasoningDelta: (msg: ReasoningDeltaMsg) => void;
  /** Drop a single agent's buffer (e.g. when its task terminates). */
  clearAgent: (agentRef: string) => void;
  /** Wipe all buffers. */
  reset: () => void;
};

export const useActivityFeedStore = create<ActivityFeedState>((set) => ({
  reasoningByAgent: {},
  appendReasoningDelta: (msg) =>
    set((state) => ({
      reasoningByAgent: {
        ...state.reasoningByAgent,
        [msg.agent_ref]: (state.reasoningByAgent[msg.agent_ref] ?? "") + msg.delta,
      },
    })),
  clearAgent: (agentRef) =>
    set((state) => {
      if (!(agentRef in state.reasoningByAgent)) return state;
      const { [agentRef]: _removed, ...rest } = state.reasoningByAgent;
      return { reasoningByAgent: rest };
    }),
  reset: () => set({ reasoningByAgent: {} }),
}));
