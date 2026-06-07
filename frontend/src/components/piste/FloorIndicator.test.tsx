import { act, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, test, vi } from "vitest";
import { floorFromEvent } from "../../hooks/useTurnState";
import type { DebugEvent } from "../../types/ws-debug";

// The floor indicator is driven by `turn_state` voice events arriving on the
// `/ws/debug` firehose (via `useTurnState`, which owns its own socket). We mock
// the global WebSocket so the test can push synthetic frames and assert the
// rendered floor state — mirroring the MockSocket pattern in
// `hooks/useDebugWs.test.ts`.
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

import { FloorIndicator } from "./FloorIndicator";

let originalWebSocket: typeof WebSocket;

/** Build a `/ws/debug` event whose payload nests a `turn_state` frame under
 * `ws_event` — exactly how `event_bus_v2.emit_event` wraps the wire payload. */
function turnStateEvent(to: string, from = "idle"): DebugEvent {
  return {
    ts: "2026-06-07T10:00:00.000Z",
    category: "voice",
    severity: "info",
    source: "bob.voice_loop.turn_state",
    summary: `turn_state ${from}->${to}`,
    payload: {
      ws_event: { type: "turn_state", turn_id: "t1", from, to, reason: "test", ts: 1 },
    },
    turn_id: "t1",
    correlation_id: null,
    replayed: false,
  };
}

function dispatch(socket: MockSocket, event: DebugEvent) {
  socket.onmessage?.({ data: JSON.stringify(event) });
}

describe("FloorIndicator", () => {
  beforeEach(() => {
    originalWebSocket = globalThis.WebSocket;
    sockets.list.length = 0;
    // biome-ignore lint/suspicious/noExplicitAny: minimal WS stub for the test
    globalThis.WebSocket = MockSocket as any;
  });

  afterEach(() => {
    globalThis.WebSocket = originalWebSocket;
    vi.clearAllMocks();
  });

  test("starts at idle before any voice event", () => {
    render(<FloorIndicator />);
    expect(screen.getByTestId("floor-indicator")).toHaveAttribute("data-floor", "idle");
    expect(screen.getByTestId("floor-indicator")).toHaveTextContent(/veille/i);
  });

  test("reflects each turn_state transition (user_speaking → thinking → bob_speaking → idle)", () => {
    render(<FloorIndicator />);
    const socket = sockets.list.at(-1);
    expect(socket).toBeDefined();
    if (!socket) return;
    const el = screen.getByTestId("floor-indicator");

    act(() => {
      socket.readyState = MockSocket.OPEN;
      socket.onopen?.();
      dispatch(socket, turnStateEvent("user_speaking"));
    });
    expect(el).toHaveAttribute("data-floor", "user_speaking");
    expect(el).toHaveTextContent(/écoute/i);

    act(() => dispatch(socket, turnStateEvent("thinking", "user_speaking")));
    expect(el).toHaveAttribute("data-floor", "thinking");
    expect(el).toHaveTextContent(/réflexion/i);

    act(() => dispatch(socket, turnStateEvent("bob_speaking", "thinking")));
    expect(el).toHaveAttribute("data-floor", "bob_speaking");
    expect(el).toHaveTextContent(/réponse/i);

    act(() => dispatch(socket, turnStateEvent("idle", "bob_speaking")));
    expect(el).toHaveAttribute("data-floor", "idle");
  });

  test("ignores non-voice events and other voice event types", () => {
    render(<FloorIndicator />);
    const socket = sockets.list.at(-1);
    if (!socket) return;
    const el = screen.getByTestId("floor-indicator");

    act(() => {
      socket.readyState = MockSocket.OPEN;
      socket.onopen?.();
      // a non-voice category event
      dispatch(socket, {
        ...turnStateEvent("bob_speaking"),
        category: "llm",
      });
      // a voice event that is NOT a turn_state (e.g. stt_partial)
      dispatch(socket, {
        ...turnStateEvent("bob_speaking"),
        payload: { ws_event: { type: "stt_partial", turn_id: "t1", text: "hi", ts: 1 } },
      });
    });
    // Floor stayed idle — neither frame moved it.
    expect(el).toHaveAttribute("data-floor", "idle");
  });

  test("becomes active (is-active) only when the floor is non-idle", () => {
    render(<FloorIndicator />);
    const socket = sockets.list.at(-1);
    if (!socket) return;
    const el = screen.getByTestId("floor-indicator");
    expect(el.className).not.toMatch(/is-active/);

    act(() => {
      socket.readyState = MockSocket.OPEN;
      socket.onopen?.();
      dispatch(socket, turnStateEvent("bob_speaking"));
    });
    expect(el.className).toMatch(/is-active/);
  });
});

describe("floorFromEvent", () => {
  test("extracts the .to floor state from a voice turn_state event", () => {
    expect(floorFromEvent(turnStateEvent("thinking"))).toBe("thinking");
  });

  test("returns null for non-voice events", () => {
    expect(floorFromEvent({ ...turnStateEvent("thinking"), category: "system" })).toBeNull();
  });

  test("returns null for a voice event that is not a turn_state", () => {
    const ev: DebugEvent = {
      ...turnStateEvent("thinking"),
      payload: { ws_event: { type: "bargein", turn_id: "t1" } },
    };
    expect(floorFromEvent(ev)).toBeNull();
  });

  test("returns null for an unknown .to value (forward-compat)", () => {
    const ev: DebugEvent = {
      ...turnStateEvent("thinking"),
      payload: { ws_event: { type: "turn_state", to: "some_future_state" } },
    };
    expect(floorFromEvent(ev)).toBeNull();
  });

  test("is defensive against a missing/malformed payload", () => {
    expect(floorFromEvent({ ...turnStateEvent("thinking"), payload: {} })).toBeNull();
    expect(
      floorFromEvent({ ...turnStateEvent("thinking"), payload: { ws_event: null } }),
    ).toBeNull();
  });
});
