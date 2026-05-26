/**
 * Streaming-delta consumer tests (PRD 0006 / issue 0049).
 *
 * The "speech delta" consumer lives on the chat store and is wired by the
 * `useChatWsBridge` hook. We drive a mock socket through a representative
 * frame sequence and assert the streaming buffer state matches the
 * acceptance criteria:
 *
 * - one or more `speech_delta` frames accumulate into a single buffer
 *   keyed by `msg_id`;
 * - a `ui_payload` frame lands the descriptor on the same buffer;
 * - the closing `assistant_msg` persists the bubble AND clears the buffer;
 * - an empty / missing `ui` payload (no `ui_payload` frame) leaves the
 *   buffer's `ui` slot null — the SphereUI consumer treats this as
 *   "no overlay to open" (issue 0049 AC).
 *
 * We use the same mock-WebSocket harness as `useChatWsBridge.test.ts` so
 * the two specs share the surrounding ceremony.
 */
import { act, renderHook } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, test, vi } from "vitest";

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

function openSocket(): MockSocket {
  const socket = sockets.list.at(-1);
  if (!socket) throw new Error("no socket was constructed");
  act(() => {
    socket.readyState = MockSocket.OPEN;
    socket.onopen?.();
  });
  return socket;
}

function send(socket: MockSocket, frame: unknown): void {
  act(() => {
    socket.onmessage?.({ data: JSON.stringify(frame) });
  });
}

describe("speech_delta / ui_payload streaming pipeline", () => {
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

  test("speech_delta frames accumulate into a single streaming buffer", () => {
    renderHook(() => useChatWsBridge());
    const socket = openSocket();

    send(socket, { type: "speech_delta", msg_id: "m1", delta: "Bonjour " });
    expect(useChatStore.getState().streamingAssistant).toEqual({
      msgId: "m1",
      speech: "Bonjour ",
      ui: null,
    });

    send(socket, { type: "speech_delta", msg_id: "m1", delta: "Tom" });
    expect(useChatStore.getState().streamingAssistant?.speech).toBe("Bonjour Tom");
  });

  test("ui_payload lands the descriptor on the in-flight buffer", () => {
    renderHook(() => useChatWsBridge());
    const socket = openSocket();

    send(socket, { type: "speech_delta", msg_id: "m1", delta: "hi" });
    send(socket, {
      type: "ui_payload",
      msg_id: "m1",
      ui: { component: "Markdown", props: { content: "# Salut" } },
    });

    expect(useChatStore.getState().streamingAssistant?.ui).toEqual({
      component: "Markdown",
      props: { content: "# Salut" },
    });
  });

  test("closing assistant_msg persists the bubble and clears the streaming buffer", () => {
    renderHook(() => useChatWsBridge());
    const socket = openSocket();

    send(socket, { type: "speech_delta", msg_id: "m1", delta: "Hi" });
    send(socket, {
      type: "assistant_msg",
      msg_id: "m1",
      speech: "Hi",
      ui: [],
      proactive: false,
    });

    const state = useChatStore.getState();
    expect(state.streamingAssistant).toBeNull();
    expect(state.messages.at(-1)).toMatchObject({
      id: "m1",
      role: "assistant",
      content: "Hi",
    });
  });

  test("ui_payload before any speech_delta still opens the buffer", () => {
    renderHook(() => useChatWsBridge());
    const socket = openSocket();

    send(socket, {
      type: "ui_payload",
      msg_id: "m1",
      ui: { component: "Markdown", props: { content: "x" } },
    });

    // Edge case: backend never streamed speech before the close. The
    // buffer carries an empty speech but a valid ui — the SphereUI
    // consumer opens the overlay just the same.
    expect(useChatStore.getState().streamingAssistant).toEqual({
      msgId: "m1",
      speech: "",
      ui: { component: "Markdown", props: { content: "x" } },
    });
  });

  test("a new msg_id starts a fresh buffer (no cross-turn leakage)", () => {
    renderHook(() => useChatWsBridge());
    const socket = openSocket();

    send(socket, { type: "speech_delta", msg_id: "m1", delta: "first" });
    // Backend retried — fresh msg_id binds the next batch of deltas.
    send(socket, { type: "speech_delta", msg_id: "m2", delta: "retry " });
    send(socket, { type: "speech_delta", msg_id: "m2", delta: "speech" });

    expect(useChatStore.getState().streamingAssistant).toEqual({
      msgId: "m2",
      speech: "retry speech",
      ui: null,
    });
  });

  test("ui_payload with a stale msg_id is dropped", () => {
    renderHook(() => useChatWsBridge());
    const socket = openSocket();

    send(socket, { type: "speech_delta", msg_id: "m2", delta: "hi" });
    // Late ui_payload for the previous turn — must not stamp on m2.
    send(socket, {
      type: "ui_payload",
      msg_id: "m1-stale",
      ui: { component: "Markdown", props: { content: "stale" } },
    });

    expect(useChatStore.getState().streamingAssistant?.ui).toBeNull();
  });

  test("absence of ui_payload leaves the buffer's ui slot null (no overlay)", () => {
    renderHook(() => useChatWsBridge());
    const socket = openSocket();

    // Streamed speech only — no ui frame ever lands. The closing
    // assistant_msg carries an empty ui array (legacy compat) and
    // the streaming buffer is cleared without ever opening an overlay.
    send(socket, { type: "speech_delta", msg_id: "m1", delta: "Hi only" });
    expect(useChatStore.getState().streamingAssistant?.ui).toBeNull();

    send(socket, {
      type: "assistant_msg",
      msg_id: "m1",
      speech: "Hi only",
      ui: [],
      proactive: false,
    });
    expect(useChatStore.getState().streamingAssistant).toBeNull();
  });
});
