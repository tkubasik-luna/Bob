import { describe, expect, test } from "vitest";
import {
  DEBUG_CATEGORIES,
  type DebugCategory,
  type DebugEvent,
  type DebugFilters,
  type DebugSeverity,
} from "../types/ws-debug";
import { filterEvents, passesFilters } from "./debugFilter";

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
