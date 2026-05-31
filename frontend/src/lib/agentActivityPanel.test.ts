import { describe, expect, test } from "vitest";
import type { AgentTimelineItem } from "../store/activityFeedStore";
import type { TaskState } from "../types/ws";
import {
  computeActiveAgents,
  computeActiveCount,
  computeActivitySignal,
  shouldAutoExpand,
} from "./agentActivityPanel";

const r = (text: string): AgentTimelineItem => ({ kind: "reasoning", text });

describe("computeActiveAgents", () => {
  test("returns agents in `agentOrder` with no terminal state, in order", () => {
    const order = ["jarvis", "task-a", "task-b"];
    const finished: Record<string, TaskState> = { "task-a": "done" };
    expect(computeActiveAgents(order, finished)).toEqual(["jarvis", "task-b"]);
  });

  test("empty when every agent finished", () => {
    expect(computeActiveAgents(["a", "b"], { a: "done", b: "failed" })).toEqual([]);
  });

  test("empty when there are no agents", () => {
    expect(computeActiveAgents([], {})).toEqual([]);
  });
});

describe("computeActiveCount", () => {
  test("counts only the unfinished agents", () => {
    expect(computeActiveCount(["a", "b", "c"], { b: "done" })).toBe(2);
  });
});

describe("computeActivitySignal", () => {
  test("sums timeline item counts across all agents", () => {
    const t: Record<string, AgentTimelineItem[]> = {
      jarvis: [r("a"), r("b")],
      "task-a": [r("c")],
    };
    expect(computeActivitySignal(t)).toBe(3);
  });

  test("zero for an empty map", () => {
    expect(computeActivitySignal({})).toBe(0);
  });
});

describe("shouldAutoExpand", () => {
  test("expands when the signal increased and an agent is active", () => {
    expect(shouldAutoExpand({ prevSignal: 2, nextSignal: 3, activeCount: 1 })).toBe(true);
  });

  test("does NOT expand when the signal did not increase", () => {
    expect(shouldAutoExpand({ prevSignal: 3, nextSignal: 3, activeCount: 1 })).toBe(false);
  });

  test("does NOT expand on a signal increase when no agent is active", () => {
    // A late item on an already-finished agent must not re-pop the panel.
    expect(shouldAutoExpand({ prevSignal: 2, nextSignal: 3, activeCount: 0 })).toBe(false);
  });

  test("does NOT expand when the signal drops (e.g. a lane was cleared)", () => {
    expect(shouldAutoExpand({ prevSignal: 5, nextSignal: 1, activeCount: 1 })).toBe(false);
  });
});
