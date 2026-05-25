import { act, renderHook } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, test, vi } from "vitest";
import type { DebugEvent, TaskWsFrame } from "../types/ws-debug";

// Mock WebSocket — captured per-test so each test can drive `onmessage`.
const sockets = vi.hoisted(() => ({ list: [] as MockSocket[] }));

class MockSocket {
  static OPEN = 1;
  static CONNECTING = 0;
  static CLOSED = 3;
  url: string;
  readyState: number = MockSocket.CONNECTING;
  binaryType = "";
  onopen: (() => void) | null = null;
  onmessage: ((event: { data: unknown }) => void) | null = null;
  onerror: (() => void) | null = null;
  onclose: (() => void) | null = null;
  send = vi.fn();
  close = vi.fn();
  constructor(url: string) {
    this.url = url;
    sockets.list.push(this);
  }
}

import { useTaskEvents } from "./useTaskEvents";

let originalWebSocket: typeof WebSocket;

function makeEvent(summary: string, opts: Partial<DebugEvent> = {}): DebugEvent {
  return {
    ts: opts.ts ?? `2026-05-25T14:23:0${summary.length}.000Z`,
    category: opts.category ?? "task",
    severity: opts.severity ?? "info",
    source: opts.source ?? "test.case",
    summary,
    payload: opts.payload ?? {},
    turn_id: null,
    correlation_id: null,
    parent_task_id: opts.parent_task_id ?? null,
    task_id: opts.task_id ?? "task-1",
    replayed: opts.replayed ?? false,
  };
}

function dispatch(socket: MockSocket, frame: TaskWsFrame) {
  socket.onmessage?.({ data: JSON.stringify(frame) });
}

describe("useTaskEvents", () => {
  beforeEach(() => {
    originalWebSocket = globalThis.WebSocket;
    sockets.list.length = 0;
    // biome-ignore lint/suspicious/noExplicitAny: minimal WS stub for the test
    globalThis.WebSocket = MockSocket as any;
  });

  afterEach(() => {
    globalThis.WebSocket = originalWebSocket;
  });

  test("passing null taskId keeps the hook idle (no socket opens)", () => {
    const { result } = renderHook(({ id }: { id: string | null }) => useTaskEvents(id), {
      initialProps: { id: null },
    });
    expect(sockets.list).toHaveLength(0);
    expect(result.current.events).toEqual([]);
    expect(result.current.ready).toBe(false);
  });

  test("opens a socket and merges snapshot frame on receipt", () => {
    const { result } = renderHook(({ id }: { id: string | null }) => useTaskEvents(id), {
      initialProps: { id: "task-1" },
    });

    const socket = sockets.list.at(-1);
    expect(socket).toBeDefined();
    if (!socket) return;
    expect(socket.url).toBe("ws://127.0.0.1:8000/ws/task/task-1");

    act(() => {
      socket.readyState = MockSocket.OPEN;
      socket.onopen?.();
      dispatch(socket, {
        type: "snapshot",
        task_id: "task-1",
        events: [makeEvent("seed-1"), makeEvent("seed-2")],
      });
    });

    expect(result.current.ready).toBe(true);
    expect(result.current.events.map((e) => e.summary)).toEqual(["seed-1", "seed-2"]);
  });

  test("appends tail frames in arrival order after the snapshot", () => {
    const { result } = renderHook(() => useTaskEvents("task-1"));
    const socket = sockets.list.at(-1);
    if (!socket) return;

    act(() => {
      socket.readyState = MockSocket.OPEN;
      socket.onopen?.();
      dispatch(socket, { type: "snapshot", task_id: "task-1", events: [makeEvent("a")] });
      dispatch(socket, { type: "tail", event: makeEvent("b") });
      dispatch(socket, { type: "tail", event: makeEvent("c") });
    });

    expect(result.current.events.map((e) => e.summary)).toEqual(["a", "b", "c"]);
  });

  test("deduplicates events sharing the same (ts, source, summary) tuple", () => {
    const { result } = renderHook(() => useTaskEvents("task-1"));
    const socket = sockets.list.at(-1);
    if (!socket) return;

    const seed = makeEvent("dup", { ts: "2026-05-25T14:23:01.000Z", source: "s" });
    const tailDup = makeEvent("dup", { ts: "2026-05-25T14:23:01.000Z", source: "s" });

    act(() => {
      socket.readyState = MockSocket.OPEN;
      socket.onopen?.();
      dispatch(socket, { type: "snapshot", task_id: "task-1", events: [seed] });
      dispatch(socket, { type: "tail", event: tailDup });
    });

    expect(result.current.events).toHaveLength(1);
  });

  test("changing taskId clears state and re-opens the socket", () => {
    const { result, rerender } = renderHook(({ id }: { id: string | null }) => useTaskEvents(id), {
      initialProps: { id: "task-1" },
    });
    const first = sockets.list.at(-1);
    if (!first) return;

    act(() => {
      first.readyState = MockSocket.OPEN;
      first.onopen?.();
      dispatch(first, { type: "snapshot", task_id: "task-1", events: [makeEvent("for-1")] });
    });
    expect(result.current.events.map((e) => e.summary)).toEqual(["for-1"]);

    act(() => {
      rerender({ id: "task-2" });
    });

    // State cleared; a new socket was opened for the new id.
    expect(result.current.events).toEqual([]);
    expect(result.current.ready).toBe(false);
    const second = sockets.list.at(-1);
    expect(second).toBeDefined();
    if (!second) return;
    expect(second.url).toBe("ws://127.0.0.1:8000/ws/task/task-2");

    act(() => {
      second.readyState = MockSocket.OPEN;
      second.onopen?.();
      dispatch(second, {
        type: "snapshot",
        task_id: "task-2",
        events: [makeEvent("for-2")],
      });
    });
    expect(result.current.events.map((e) => e.summary)).toEqual(["for-2"]);
  });

  test("malformed frames are silently ignored", () => {
    const { result } = renderHook(() => useTaskEvents("task-1"));
    const socket = sockets.list.at(-1);
    if (!socket) return;

    act(() => {
      socket.readyState = MockSocket.OPEN;
      socket.onopen?.();
      socket.onmessage?.({ data: "not-json" });
    });

    expect(result.current.ready).toBe(false);
    expect(result.current.events).toEqual([]);
  });

  test("unmount closes the socket without scheduling a reconnect", () => {
    const { unmount } = renderHook(() => useTaskEvents("task-1"));
    const socket = sockets.list.at(-1);
    if (!socket) return;

    unmount();

    expect(socket.close).toHaveBeenCalledTimes(1);
  });
});
