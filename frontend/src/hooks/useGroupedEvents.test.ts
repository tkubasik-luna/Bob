import { renderHook } from "@testing-library/react";
import { describe, expect, test } from "vitest";
import type { DebugEvent } from "../types/ws-debug";
import { useGroupedEvents } from "./useGroupedEvents";

function makeEvent(overrides: Partial<DebugEvent> = {}): DebugEvent {
  return {
    ts: "2026-05-25T14:23:01.123Z",
    category: "system",
    severity: "info",
    source: "test",
    summary: "evt",
    payload: {},
    turn_id: null,
    correlation_id: null,
    parent_task_id: null,
    replayed: false,
    ...overrides,
  };
}

describe("useGroupedEvents memoization", () => {
  test("same events reference yields the same tree reference across re-renders", () => {
    const events: DebugEvent[] = [makeEvent({ summary: "a" }), makeEvent({ summary: "b" })];
    const { result, rerender } = renderHook(({ ev }) => useGroupedEvents(ev), {
      initialProps: { ev: events },
    });
    const firstTree = result.current;
    rerender({ ev: events });
    expect(result.current).toBe(firstTree);
  });

  test("different events reference recomputes the tree", () => {
    const events1: DebugEvent[] = [makeEvent({ summary: "a" })];
    const events2: DebugEvent[] = [makeEvent({ summary: "a" })];
    const { result, rerender } = renderHook(({ ev }) => useGroupedEvents(ev), {
      initialProps: { ev: events1 },
    });
    const firstTree = result.current;
    rerender({ ev: events2 });
    expect(result.current).not.toBe(firstTree);
  });
});
