import { act, renderHook } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, test, vi } from "vitest";

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

import { useChatStore } from "../store/chatStore";
import { useChatWsBridge } from "./useChatWsBridge";

const initialState = useChatStore.getState();
let originalWebSocket: typeof WebSocket;

describe("useChatWsBridge", () => {
  beforeEach(() => {
    originalWebSocket = globalThis.WebSocket;
    sockets.list.length = 0;
    useChatStore.setState(initialState, true);
    // biome-ignore lint/suspicious/noExplicitAny: minimal WS stub for the test
    globalThis.WebSocket = MockSocket as any;
  });

  afterEach(() => {
    globalThis.WebSocket = originalWebSocket;
  });

  test("a `thinking { state: 'start' }` frame flips `isWaitingResponse` to true", () => {
    expect(useChatStore.getState().isWaitingResponse).toBe(false);

    renderHook(() => useChatWsBridge());

    const socket = sockets.list.at(-1);
    expect(socket).toBeDefined();
    if (!socket) return;

    // Walk the socket through the open handshake the hook installs in
    // `connect()`, then dispatch the JSON frame we want to assert on.
    act(() => {
      socket.readyState = MockSocket.OPEN;
      socket.onopen?.();
      socket.onmessage?.({ data: JSON.stringify({ type: "thinking", state: "start" }) });
    });

    expect(useChatStore.getState().isWaitingResponse).toBe(true);
  });
});
