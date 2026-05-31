import { create } from "zustand";
import type { AgentActivityMsg, ReasoningDeltaMsg, TaskState } from "../types/ws";

/**
 * PRD 0011 — agent-activity feed store.
 *
 * Issue 0069 (tracer bullet) accumulated only the live `reasoning_delta` stream
 * per `agent_ref`. Issue 0071 adds the curated activity CHIPS (`agent_activity`
 * events) and — crucially — interleaves them with the reasoning IN ORDER, so
 * the `AgentBlock` can render a single chronological flow (reasoning text
 * segments + chips) exactly as they arrived on the wire.
 *
 * Issue 0073 adds two things on top WITHOUT changing the per-agent timeline
 * shape 0071 settled on:
 *   1. LANES — a first-seen-ordered `agentOrder` list so a container can render
 *      one `AgentBlock` per active agent. The store already keys timelines by
 *      `agent_ref`, so lanes are conceptually distinct; `agentOrder` just makes
 *      the set enumerable & stable for a `.map()`. One agent's deltas never
 *      bleed into another's timeline (each `agent_ref` owns its own array).
 *   2. THROTTLING — `reasoning_delta` events arrive token-by-token, and with N
 *      concurrent agents that floods React with a store update per token. We
 *      buffer incoming reasoning deltas per `agent_ref` and FLUSH them on a
 *      single `requestAnimationFrame` tick, so the store mutates at most ~once
 *      per frame regardless of how many tokens (or agents) arrived in between.
 *      Chips (`agent_activity`) are low-frequency and discrete, so they apply
 *      immediately — but the flush drains any pending reasoning first to keep
 *      interleave order exact.
 *
 * State design (forward-looking for 0075 sliding window): each agent owns an
 * ORDERED `AgentTimelineItem[]`. A reasoning delta either extends the trailing
 * reasoning segment (so a burst of deltas coalesces into one text block) or
 * starts a new one if the last item is a chip. A chip is appended as its own
 * item. Order is preserved, so a later issue can window/collapse the timelines
 * without re-deriving order.
 *
 * Issue 0074 — COLLAPSE LIFECYCLE. When a task terminates the block must stop
 * being the live ACTIVE view and become a COLLAPSED summary, while keeping the
 * timeline content so the user can expand it again to re-read the reasoning.
 * The timeline arrays already persist (nothing clears them on terminal state —
 * `clearAgent` is only for an explicit lane drop), so all this slice adds is a
 * tiny per-agent FINISHED map: `finishedByAgent[agentRef]` records the terminal
 * `TaskState` once a task ends. The block reads it to flip ACTIVE → COLLAPSED.
 * The map is purely additive — `agentOrder` / `timelineByAgent` and every 0073/
 * 0075 behaviour are untouched. `reset` clears it; `clearAgent` drops the one
 * entry (a dropped lane has no finished state to remember).
 *
 * NOT in this slice: the Jarvis block (0072), sliding window (0075) and the
 * side-panel rail (0076). The summary's title + result handle are NOT held here
 * (they live on the chatStore task); only the lifecycle bit + final state are.
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
  /** First-seen-ordered list of `agent_ref`s with a lane. A lanes container
   * maps over this to render one `AgentBlock` per agent in a stable order. */
  agentOrder: string[];
  /** Issue 0074 — per-agent terminal state. Absent while the agent is live;
   * set to the final `TaskState` (`done` / `failed`) once its task ends. The
   * `AgentBlock` reads this to switch from the live ACTIVE timeline to the
   * COLLAPSED summary while keeping the timeline content for expand. */
  finishedByAgent: Record<string, TaskState>;
  /** Append a `reasoning_delta` suffix. COALESCED: the delta is buffered and
   * applied on the next animation-frame flush, not synchronously per token. */
  appendReasoningDelta: (msg: ReasoningDeltaMsg) => void;
  /** Append an activity chip as its own ordered timeline item. Applies
   * immediately (after draining any pending reasoning to preserve order). */
  appendActivity: (msg: AgentActivityMsg) => void;
  /** Issue 0074 — mark an agent finished with its terminal `TaskState`. The
   * timeline is RETAINED (so the collapsed block can expand to re-read it);
   * this only records the lifecycle bit. Idempotent; a no-op if the state is
   * unchanged so a replayed terminal event doesn't churn the store. */
  markAgentFinished: (agentRef: string, finalState: TaskState) => void;
  /** Drop a single agent's timeline + lane (e.g. when its task terminates). */
  clearAgent: (agentRef: string) => void;
  /** Wipe all timelines / lanes / pending buffers. */
  reset: () => void;
  /** Drain all buffered reasoning deltas into `timelineByAgent` NOW. Called by
   * the rAF tick; also exposed for tests / teardown to flush synchronously. */
  flushReasoning: () => void;
};

/**
 * Pure reducer: fold a reasoning suffix into an agent's existing timeline,
 * coalescing into the trailing reasoning segment (or starting a new one after a
 * chip). Returns a NEW array; never mutates the input. Exported so it (and the
 * coalescing behaviour) is unit-testable without React or the rAF scheduler.
 */
export function appendReasoningToTimeline(
  existing: AgentTimelineItem[],
  suffix: string,
): AgentTimelineItem[] {
  const last = existing[existing.length - 1];
  if (last && last.kind === "reasoning") {
    return [...existing.slice(0, -1), { kind: "reasoning", text: last.text + suffix }];
  }
  return [...existing, { kind: "reasoning", text: suffix }];
}

/**
 * Module-level buffer of un-flushed reasoning suffixes, keyed by `agent_ref`.
 * Lives outside zustand state so accumulating a delta does NOT trigger a store
 * update / re-render — only the flush does. Insertion order of keys mirrors
 * first-seen agent order, which the flush relies on to register lanes in order.
 */
const pendingReasoning = new Map<string, string>();

/** Handle for the scheduled flush, so we coalesce many deltas into one tick. */
let flushHandle: number | null = null;

/** rAF if available (browser), else a ~16ms timer (jsdom / Node test env). */
const scheduleFlush =
  typeof requestAnimationFrame === "function"
    ? (cb: () => void) => requestAnimationFrame(cb)
    : (cb: () => void) => setTimeout(cb, 16) as unknown as number;
const cancelFlush =
  typeof cancelAnimationFrame === "function"
    ? (h: number) => cancelAnimationFrame(h)
    : (h: number) => clearTimeout(h);

export const useActivityFeedStore = create<ActivityFeedState>((set) => ({
  timelineByAgent: {},
  agentOrder: [],
  finishedByAgent: {},
  appendReasoningDelta: (msg) => {
    // Buffer the suffix; do NOT touch the store yet. Multiple tokens arriving
    // within one frame collapse into a single concatenated suffix here.
    const prev = pendingReasoning.get(msg.agent_ref) ?? "";
    pendingReasoning.set(msg.agent_ref, prev + msg.delta);
    if (flushHandle === null) {
      flushHandle = scheduleFlush(() => {
        flushHandle = null;
        // `set` is closed over below via the store; re-enter through getState
        // is avoided by calling the action directly.
        useActivityFeedStore.getState().flushReasoning();
      });
    }
  },
  flushReasoning: () => {
    if (pendingReasoning.size === 0) return;
    // Snapshot + clear the buffer up front so deltas arriving during the set()
    // are not lost (they re-arm a fresh flush).
    const drained = Array.from(pendingReasoning.entries());
    pendingReasoning.clear();
    set((state) => {
      const timelineByAgent = { ...state.timelineByAgent };
      const agentOrder = [...state.agentOrder];
      for (const [agentRef, suffix] of drained) {
        if (suffix.length === 0) continue;
        const existing = timelineByAgent[agentRef] ?? [];
        timelineByAgent[agentRef] = appendReasoningToTimeline(existing, suffix);
        if (!agentOrder.includes(agentRef)) agentOrder.push(agentRef);
      }
      return { timelineByAgent, agentOrder };
    });
  },
  appendActivity: (msg) => {
    // Drain pending reasoning FIRST so the chip lands after the reasoning that
    // preceded it on the wire — interleave order must stay exact.
    useActivityFeedStore.getState().flushReasoning();
    set((state) => {
      const existing = state.timelineByAgent[msg.agent_ref] ?? [];
      const chip: ChipItem = {
        kind: "chip",
        activityKind: msg.kind,
        label: msg.label,
        status: msg.status,
      };
      const agentOrder = state.agentOrder.includes(msg.agent_ref)
        ? state.agentOrder
        : [...state.agentOrder, msg.agent_ref];
      return {
        timelineByAgent: {
          ...state.timelineByAgent,
          [msg.agent_ref]: [...existing, chip],
        },
        agentOrder,
      };
    });
  },
  markAgentFinished: (agentRef, finalState) =>
    set((state) => {
      // Idempotent: a replayed / duplicate terminal event with the same state
      // is a no-op so we don't churn the store (and trigger re-renders).
      if (state.finishedByAgent[agentRef] === finalState) return state;
      return {
        finishedByAgent: { ...state.finishedByAgent, [agentRef]: finalState },
      };
    }),
  clearAgent: (agentRef) =>
    set((state) => {
      pendingReasoning.delete(agentRef);
      // A dropped lane has no finished state to remember.
      const { [agentRef]: _finished, ...restFinished } = state.finishedByAgent;
      if (!(agentRef in state.timelineByAgent)) {
        if (!state.agentOrder.includes(agentRef) && !(agentRef in state.finishedByAgent)) {
          return state;
        }
        return {
          agentOrder: state.agentOrder.filter((r) => r !== agentRef),
          finishedByAgent: restFinished,
        };
      }
      const { [agentRef]: _removed, ...rest } = state.timelineByAgent;
      return {
        timelineByAgent: rest,
        agentOrder: state.agentOrder.filter((r) => r !== agentRef),
        finishedByAgent: restFinished,
      };
    }),
  reset: () => {
    pendingReasoning.clear();
    if (flushHandle !== null) {
      cancelFlush(flushHandle);
      flushHandle = null;
    }
    set({ timelineByAgent: {}, agentOrder: [], finishedByAgent: {} });
  },
}));
