import { beforeEach, describe, expect, it } from "vitest";
import type { AgentActivityMsg, ReasoningDeltaMsg } from "../types/ws";
import {
  type AgentTimelineItem,
  appendReasoningToTimeline,
  useActivityFeedStore,
} from "./activityFeedStore";

const delta = (agent_ref: string, d: string): ReasoningDeltaMsg => ({
  type: "reasoning_delta",
  agent_ref,
  delta: d,
});

const activity = (
  agent_ref: string,
  label: string,
  overrides: Partial<AgentActivityMsg> = {},
): AgentActivityMsg => ({
  type: "agent_activity",
  agent_ref,
  kind: "tool_call",
  label,
  status: "running",
  ...overrides,
});

/** Concatenate the reasoning text of a lane (ignoring chips). */
const reasoningText = (timeline: AgentTimelineItem[] | undefined): string =>
  (timeline ?? [])
    .filter((i): i is Extract<AgentTimelineItem, { kind: "reasoning" }> => i.kind === "reasoning")
    .map((i) => i.text)
    .join("");

beforeEach(() => {
  // The coalescing buffer lives at module scope — reset() drains it + cancels
  // any pending flush so each test starts clean.
  useActivityFeedStore.getState().reset();
});

describe("activityFeedStore — pure coalescing reducer", () => {
  it("coalesces consecutive reasoning into a single trailing segment", () => {
    let tl: AgentTimelineItem[] = [];
    tl = appendReasoningToTimeline(tl, "Hello ");
    tl = appendReasoningToTimeline(tl, "world");
    expect(tl).toEqual([{ kind: "reasoning", text: "Hello world" }]);
  });

  it("starts a new reasoning segment after a chip", () => {
    const withChip: AgentTimelineItem[] = [
      { kind: "reasoning", text: "before" },
      { kind: "chip", activityKind: "tool_call", label: "search", status: "running" },
    ];
    const tl = appendReasoningToTimeline(withChip, "after");
    expect(tl).toEqual([
      { kind: "reasoning", text: "before" },
      { kind: "chip", activityKind: "tool_call", label: "search", status: "running" },
      { kind: "reasoning", text: "after" },
    ]);
  });
});

describe("activityFeedStore — lanes (per-agent isolation + order)", () => {
  it("keeps interleaved deltas from two agents in separate lanes, in order, no bleed", () => {
    const store = useActivityFeedStore.getState();
    // Interleave A and B token-by-token, the way two concurrent sub-agents
    // would stream them onto the wire.
    store.appendReasoningDelta(delta("A", "a1 "));
    store.appendReasoningDelta(delta("B", "b1 "));
    store.appendReasoningDelta(delta("A", "a2 "));
    store.appendReasoningDelta(delta("B", "b2 "));
    store.appendReasoningDelta(delta("A", "a3"));
    store.appendReasoningDelta(delta("B", "b3"));
    // Nothing applied yet — all buffered, awaiting the flush.
    store.flushReasoning();

    const { timelineByAgent, agentOrder } = useActivityFeedStore.getState();
    expect(reasoningText(timelineByAgent.A)).toBe("a1 a2 a3");
    expect(reasoningText(timelineByAgent.B)).toBe("b1 b2 b3");
    // First-seen order: A buffered before B.
    expect(agentOrder).toEqual(["A", "B"]);
  });

  it("interleaves chips with reasoning per lane without cross-bleed", () => {
    const store = useActivityFeedStore.getState();
    store.appendReasoningDelta(delta("A", "thinking-A"));
    store.appendReasoningDelta(delta("B", "thinking-B"));
    // A chip drains pending reasoning first, so it lands AFTER A's reasoning.
    store.appendActivity(activity("A", "gmail.search"));
    store.appendReasoningDelta(delta("A", "more-A"));
    store.flushReasoning();

    const { timelineByAgent } = useActivityFeedStore.getState();
    expect(timelineByAgent.A).toEqual([
      { kind: "reasoning", text: "thinking-A" },
      { kind: "chip", activityKind: "tool_call", label: "gmail.search", status: "running" },
      { kind: "reasoning", text: "more-A" },
    ]);
    // B is untouched by A's chip / second delta.
    expect(timelineByAgent.B).toEqual([{ kind: "reasoning", text: "thinking-B" }]);
  });
});

describe("activityFeedStore — coalescing / throttling at the store API", () => {
  it("buffers N deltas and applies them as ONE coalesced segment on flush", () => {
    const store = useActivityFeedStore.getState();
    for (let i = 0; i < 10; i++) {
      store.appendReasoningDelta(delta("A", `t${i} `));
    }
    // Before the flush the store has not been mutated at all.
    expect(useActivityFeedStore.getState().timelineByAgent.A).toBeUndefined();

    store.flushReasoning();

    const lane = useActivityFeedStore.getState().timelineByAgent.A;
    // 10 deltas collapse into a single reasoning segment — not 10 segments.
    expect(lane).toHaveLength(1);
    expect(lane?.[0]).toEqual({
      kind: "reasoning",
      text: "t0 t1 t2 t3 t4 t5 t6 t7 t8 t9 ",
    });
  });

  it("flush is idempotent when the buffer is empty", () => {
    const store = useActivityFeedStore.getState();
    store.appendReasoningDelta(delta("A", "x"));
    store.flushReasoning();
    const after = useActivityFeedStore.getState().timelineByAgent;
    store.flushReasoning(); // no pending → no-op, same reference semantics
    expect(useActivityFeedStore.getState().timelineByAgent.A).toEqual(after.A);
  });
});

describe("activityFeedStore — lane lifecycle", () => {
  it("clearAgent drops the lane and its order entry", () => {
    const store = useActivityFeedStore.getState();
    store.appendReasoningDelta(delta("A", "a"));
    store.appendReasoningDelta(delta("B", "b"));
    store.flushReasoning();
    store.clearAgent("A");

    const { timelineByAgent, agentOrder } = useActivityFeedStore.getState();
    expect(timelineByAgent.A).toBeUndefined();
    expect(agentOrder).toEqual(["B"]);
  });
});

describe("activityFeedStore — finish lifecycle (issue 0074)", () => {
  it("marking an agent finished RETAINS its timeline and exposes the final state", () => {
    const store = useActivityFeedStore.getState();
    store.appendReasoningDelta(delta("A", "thinking…"));
    store.appendActivity(activity("A", "gmail.search"));
    store.flushReasoning();

    store.markAgentFinished("A", "done");

    const { timelineByAgent, finishedByAgent, agentOrder } = useActivityFeedStore.getState();
    // Timeline content is kept so the collapsed block can expand to re-read it.
    expect(reasoningText(timelineByAgent.A)).toBe("thinking…");
    expect(timelineByAgent.A).toHaveLength(2);
    // The lane remains enumerable; only the lifecycle bit is added.
    expect(agentOrder).toEqual(["A"]);
    expect(finishedByAgent.A).toBe("done");
  });

  it("records a failure state for a force-terminated / failed agent", () => {
    const store = useActivityFeedStore.getState();
    store.appendReasoningDelta(delta("A", "stalled"));
    store.flushReasoning();
    store.markAgentFinished("A", "failed");

    expect(useActivityFeedStore.getState().finishedByAgent.A).toBe("failed");
    // Timeline (incl. the incident reasoning) survives so it's still visible.
    expect(reasoningText(useActivityFeedStore.getState().timelineByAgent.A)).toBe("stalled");
  });

  it("an unfinished agent stays active (no entry in finishedByAgent)", () => {
    const store = useActivityFeedStore.getState();
    store.appendReasoningDelta(delta("A", "a"));
    store.appendReasoningDelta(delta("B", "b"));
    store.flushReasoning();
    store.markAgentFinished("A", "done");

    const { finishedByAgent } = useActivityFeedStore.getState();
    expect(finishedByAgent.A).toBe("done");
    // B never terminated → still active.
    expect(finishedByAgent.B).toBeUndefined();
  });

  it("markAgentFinished is idempotent for the same state (no churn)", () => {
    const store = useActivityFeedStore.getState();
    store.markAgentFinished("A", "done");
    const first = useActivityFeedStore.getState().finishedByAgent;
    store.markAgentFinished("A", "done");
    // Same reference: no state object was produced for a redundant terminal event.
    expect(useActivityFeedStore.getState().finishedByAgent).toBe(first);
  });

  it("clearAgent drops the finished entry alongside the lane", () => {
    const store = useActivityFeedStore.getState();
    store.appendReasoningDelta(delta("A", "a"));
    store.flushReasoning();
    store.markAgentFinished("A", "done");
    store.clearAgent("A");

    const { timelineByAgent, finishedByAgent } = useActivityFeedStore.getState();
    expect(timelineByAgent.A).toBeUndefined();
    expect(finishedByAgent.A).toBeUndefined();
  });

  it("reset clears the finished map", () => {
    const store = useActivityFeedStore.getState();
    store.markAgentFinished("A", "done");
    store.reset();
    expect(useActivityFeedStore.getState().finishedByAgent).toEqual({});
  });
});
