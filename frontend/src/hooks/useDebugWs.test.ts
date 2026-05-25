import { act, renderHook } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, test, vi } from "vitest";
import type { DebugEvent } from "../types/ws-debug";

// Capture every constructed mock so the test can drive `onmessage` from
// outside. We push the live instance into a hoisted ref so the spec can read
// the latest socket after the hook calls `new WebSocket(...)`.
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

import { useDebugWs } from "./useDebugWs";

let originalWebSocket: typeof WebSocket;

function makeEvent(summary: string): DebugEvent {
  return {
    ts: "2026-05-25T14:23:01.123Z",
    category: "system",
    severity: "info",
    source: "test.case",
    summary,
    payload: {},
    turn_id: null,
    correlation_id: null,
    replayed: false,
  };
}

function dispatch(socket: MockSocket, event: DebugEvent) {
  socket.onmessage?.({ data: JSON.stringify(event) });
}

describe("useDebugWs", () => {
  beforeEach(() => {
    originalWebSocket = globalThis.WebSocket;
    sockets.list.length = 0;
    // biome-ignore lint/suspicious/noExplicitAny: minimal WS stub for the test
    globalThis.WebSocket = MockSocket as any;
  });

  afterEach(() => {
    globalThis.WebSocket = originalWebSocket;
  });

  test("events landing while not paused stream straight into `events`", () => {
    const { result } = renderHook(() => useDebugWs());

    const socket = sockets.list.at(-1);
    expect(socket).toBeDefined();
    if (!socket) return;

    act(() => {
      socket.readyState = MockSocket.OPEN;
      socket.onopen?.();
      dispatch(socket, makeEvent("a"));
      dispatch(socket, makeEvent("b"));
    });

    expect(result.current.events.map((e) => e.summary)).toEqual(["a", "b"]);
    expect(result.current.paused).toBe(false);
    expect(result.current.pendingCount).toBe(0);
  });

  test("events arriving while paused are buffered and flushed in order on resume", () => {
    const { result } = renderHook(() => useDebugWs());

    const socket = sockets.list.at(-1);
    expect(socket).toBeDefined();
    if (!socket) return;

    // Pre-pause baseline.
    act(() => {
      socket.readyState = MockSocket.OPEN;
      socket.onopen?.();
      dispatch(socket, makeEvent("pre-1"));
    });
    expect(result.current.events.map((e) => e.summary)).toEqual(["pre-1"]);

    // Pause and feed three events; the visible array must stay frozen.
    act(() => {
      result.current.setPaused(true);
    });
    expect(result.current.paused).toBe(true);

    act(() => {
      dispatch(socket, makeEvent("buf-1"));
      dispatch(socket, makeEvent("buf-2"));
      dispatch(socket, makeEvent("buf-3"));
    });

    expect(result.current.events.map((e) => e.summary)).toEqual(["pre-1"]);
    expect(result.current.pendingCount).toBe(3);

    // Resume → buffered events appended chronologically, counter resets.
    act(() => {
      result.current.setPaused(false);
    });

    expect(result.current.events.map((e) => e.summary)).toEqual([
      "pre-1",
      "buf-1",
      "buf-2",
      "buf-3",
    ]);
    expect(result.current.paused).toBe(false);
    expect(result.current.pendingCount).toBe(0);
  });

  test("the functional setPaused form sees the previous value", () => {
    const { result } = renderHook(() => useDebugWs());

    expect(result.current.paused).toBe(false);
    act(() => {
      result.current.setPaused((p) => !p);
    });
    expect(result.current.paused).toBe(true);
    act(() => {
      result.current.setPaused((p) => !p);
    });
    expect(result.current.paused).toBe(false);
  });

  test("`clear()` empties the visible feed and any pending buffer without touching the socket", () => {
    const { result } = renderHook(() => useDebugWs());

    const socket = sockets.list.at(-1);
    expect(socket).toBeDefined();
    if (!socket) return;

    act(() => {
      socket.readyState = MockSocket.OPEN;
      socket.onopen?.();
      dispatch(socket, makeEvent("a"));
      dispatch(socket, makeEvent("b"));
    });
    expect(result.current.events).toHaveLength(2);

    // Pause + buffer some events so we can assert clear wipes them too.
    act(() => {
      result.current.setPaused(true);
      dispatch(socket, makeEvent("buf"));
    });
    expect(result.current.pendingCount).toBe(1);

    act(() => {
      result.current.clear();
    });
    expect(result.current.events).toEqual([]);
    expect(result.current.pendingCount).toBe(0);

    // Resuming after clear must not resurrect previously buffered events.
    act(() => {
      result.current.setPaused(false);
    });
    expect(result.current.events).toEqual([]);

    // Socket itself is untouched — close was never called.
    expect(socket.close).not.toHaveBeenCalled();

    // New live events stream in normally after a clear.
    act(() => {
      dispatch(socket, makeEvent("after"));
    });
    expect(result.current.events.map((e) => e.summary)).toEqual(["after"]);
  });

  test("malformed frames are silently ignored in both modes", () => {
    const { result } = renderHook(() => useDebugWs());

    const socket = sockets.list.at(-1);
    expect(socket).toBeDefined();
    if (!socket) return;

    act(() => {
      socket.readyState = MockSocket.OPEN;
      socket.onopen?.();
      socket.onmessage?.({ data: "not-json" });
    });
    expect(result.current.events).toEqual([]);

    act(() => {
      result.current.setPaused(true);
      socket.onmessage?.({ data: "still-not-json" });
    });
    expect(result.current.pendingCount).toBe(0);
  });
});
