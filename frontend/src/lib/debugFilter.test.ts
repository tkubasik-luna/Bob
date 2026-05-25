import { describe, expect, test } from "vitest";
import {
  DEBUG_CATEGORIES,
  type DebugCategory,
  type DebugEvent,
  type DebugFilters,
  type DebugSeverity,
} from "../types/ws-debug";
import { filterEvents, passesFilters, pruneEmptyNodes } from "./debugFilter";
import { groupEvents, type TaskNode, type TurnNode } from "./groupEvents";

function makeEvent(category: DebugCategory, severity: DebugSeverity, summary = "evt"): DebugEvent {
  return {
    ts: "2026-05-25T14:23:01.123Z",
    category,
    severity,
    source: "test.case",
    summary,
    payload: {},
    turn_id: null,
    correlation_id: null,
    replayed: false,
  };
}

function allCategoriesOn(): Set<DebugCategory> {
  return new Set<DebugCategory>(DEBUG_CATEGORIES);
}

describe("passesFilters", () => {
  test("event in active category and >= threshold passes", () => {
    const filters: DebugFilters = { categoriesOn: allCategoriesOn(), severityThreshold: "info" };
    expect(passesFilters(makeEvent("llm", "info"), filters)).toBe(true);
    expect(passesFilters(makeEvent("llm", "warn"), filters)).toBe(true);
    expect(passesFilters(makeEvent("llm", "error"), filters)).toBe(true);
  });

  test("event below threshold is rejected", () => {
    const filters: DebugFilters = { categoriesOn: allCategoriesOn(), severityThreshold: "info" };
    expect(passesFilters(makeEvent("llm", "trace"), filters)).toBe(false);
    expect(passesFilters(makeEvent("llm", "debug"), filters)).toBe(false);
  });

  test("event in disabled category is rejected even when severity high", () => {
    const categoriesOn = allCategoriesOn();
    categoriesOn.delete("voice");
    const filters: DebugFilters = { categoriesOn, severityThreshold: "trace" };
    expect(passesFilters(makeEvent("voice", "error"), filters)).toBe(false);
    // Other categories still pass.
    expect(passesFilters(makeEvent("llm", "trace"), filters)).toBe(true);
  });

  test("threshold = trace lets every severity through", () => {
    const filters: DebugFilters = { categoriesOn: allCategoriesOn(), severityThreshold: "trace" };
    for (const sev of ["trace", "debug", "info", "warn", "error"] as const) {
      expect(passesFilters(makeEvent("system", sev), filters)).toBe(true);
    }
  });

  test("threshold = error only lets error through", () => {
    const filters: DebugFilters = { categoriesOn: allCategoriesOn(), severityThreshold: "error" };
    expect(passesFilters(makeEvent("system", "warn"), filters)).toBe(false);
    expect(passesFilters(makeEvent("system", "error"), filters)).toBe(true);
  });
});

describe("filterEvents", () => {
  const sample: DebugEvent[] = [
    makeEvent("input", "info", "user typed"),
    makeEvent("llm", "trace", "token chunk"),
    makeEvent("voice", "warn", "tts dropout"),
    makeEvent("task", "error", "subtask failed"),
    makeEvent("system", "debug", "boot step"),
    makeEvent("output", "info", "rendered card"),
  ];

  test("default filters (all on, info) hide trace+debug and keep the rest", () => {
    const filters: DebugFilters = { categoriesOn: allCategoriesOn(), severityThreshold: "info" };
    const out = filterEvents(sample, filters);
    expect(out.map((e) => e.summary)).toEqual([
      "user typed",
      "tts dropout",
      "subtask failed",
      "rendered card",
    ]);
  });

  test("threshold = warn keeps only warn + error", () => {
    const filters: DebugFilters = { categoriesOn: allCategoriesOn(), severityThreshold: "warn" };
    const out = filterEvents(sample, filters);
    expect(out.map((e) => e.severity)).toEqual(["warn", "error"]);
  });

  test("disabling a category removes its rows in place", () => {
    const categoriesOn = allCategoriesOn();
    categoriesOn.delete("voice");
    const filters: DebugFilters = { categoriesOn, severityThreshold: "info" };
    const out = filterEvents(sample, filters);
    expect(out.find((e) => e.category === "voice")).toBeUndefined();
    expect(out.find((e) => e.category === "task")).toBeDefined();
  });

  test("threshold = trace + all categories returns the full input", () => {
    const filters: DebugFilters = { categoriesOn: allCategoriesOn(), severityThreshold: "trace" };
    const out = filterEvents(sample, filters);
    expect(out).toHaveLength(sample.length);
    expect(out).toEqual(sample);
  });

  test("preserves chronological input order after filtering", () => {
    const filters: DebugFilters = { categoriesOn: allCategoriesOn(), severityThreshold: "info" };
    const out = filterEvents(sample, filters);
    // Indices should be strictly ascending in the original array.
    const indices = out.map((e) => sample.indexOf(e));
    const sorted = [...indices].sort((a, b) => a - b);
    expect(indices).toEqual(sorted);
  });

  test("empty input yields empty output", () => {
    const filters: DebugFilters = { categoriesOn: allCategoriesOn(), severityThreshold: "info" };
    expect(filterEvents([], filters)).toEqual([]);
  });

  test("all categories off yields empty output regardless of severity", () => {
    const filters: DebugFilters = {
      categoriesOn: new Set<DebugCategory>(),
      severityThreshold: "trace",
    };
    expect(filterEvents(sample, filters)).toEqual([]);
  });
});

describe("pruneEmptyNodes", () => {
  function withTurn(category: DebugCategory, severity: DebugSeverity, turn_id: string): DebugEvent {
    return {
      ts: "2026-05-25T14:23:01.123Z",
      category,
      severity,
      source: "test",
      summary: `${category}/${severity}`,
      payload: {},
      turn_id,
      correlation_id: null,
      parent_task_id: null,
      replayed: false,
    };
  }

  test("drops turn whose descendants don't match the filter", () => {
    const tree = groupEvents([
      withTurn("voice", "trace", "T1"),
      withTurn("voice", "debug", "T1"),
    ]);
    const filters: DebugFilters = { categoriesOn: allCategoriesOn(), severityThreshold: "info" };
    const pruned = pruneEmptyNodes(tree, filters);
    expect(pruned).toHaveLength(0);
  });

  test("keeps turn whose at least one descendant matches", () => {
    const tree = groupEvents([
      withTurn("voice", "trace", "T1"),
      withTurn("voice", "warn", "T1"),
    ]);
    const filters: DebugFilters = { categoriesOn: allCategoriesOn(), severityThreshold: "info" };
    const pruned = pruneEmptyNodes(tree, filters);
    expect(pruned).toHaveLength(1);
    const turn = pruned[0] as TurnNode;
    expect(turn.eventCount).toBe(1);
  });

  test("recomputes counts post-filter", () => {
    const tree = groupEvents([
      withTurn("system", "info", "T1"),
      withTurn("system", "trace", "T1"),
      withTurn("system", "warn", "T1"),
    ]);
    const filters: DebugFilters = { categoriesOn: allCategoriesOn(), severityThreshold: "info" };
    const pruned = pruneEmptyNodes(tree, filters);
    const turn = pruned[0] as TurnNode;
    expect(turn.eventCount).toBe(2); // info + warn (trace dropped)
    expect(turn.maxSeverity).toBe("warn");
  });

  test("drops empty task within otherwise-surviving turn", () => {
    const events: DebugEvent[] = [
      {
        ts: "2026-05-25T14:23:00.000Z",
        category: "task",
        severity: "info",
        source: "test",
        summary: "spawn A",
        payload: { task_id: "A", title: "A" },
        turn_id: "T1",
        correlation_id: null,
        parent_task_id: null,
        replayed: false,
      },
      // Only an event the filter will drop lives in A
      {
        ts: "2026-05-25T14:23:01.000Z",
        category: "voice",
        severity: "trace",
        source: "test",
        summary: "deep",
        payload: {},
        turn_id: "T1",
        correlation_id: null,
        parent_task_id: "A",
        replayed: false,
      },
      // A direct-to-turn event that DOES pass
      {
        ts: "2026-05-25T14:23:02.000Z",
        category: "system",
        severity: "warn",
        source: "test",
        summary: "ok",
        payload: {},
        turn_id: "T1",
        correlation_id: null,
        parent_task_id: null,
        replayed: false,
      },
    ];
    const tree = groupEvents(events);
    const filters: DebugFilters = { categoriesOn: allCategoriesOn(), severityThreshold: "info" };
    const pruned = pruneEmptyNodes(tree, filters);
    const turn = pruned[0] as TurnNode;
    expect(turn.children.find((c) => c.kind === "task")).toBeUndefined();
    expect(turn.taskCount).toBe(0);
  });

  test("does not mutate the input tree", () => {
    const tree = groupEvents([
      withTurn("system", "info", "T1"),
      withTurn("system", "trace", "T1"),
    ]);
    const originalChildrenRef = (tree[0] as TurnNode).children;
    const originalCount = (tree[0] as TurnNode).eventCount;
    pruneEmptyNodes(tree, {
      categoriesOn: allCategoriesOn(),
      severityThreshold: "info",
    });
    expect((tree[0] as TurnNode).children).toBe(originalChildrenRef);
    expect((tree[0] as TurnNode).eventCount).toBe(originalCount);
  });
});
