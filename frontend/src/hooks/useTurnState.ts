import { useEffect, useRef, useState } from "react";
import type { DebugEvent } from "../types/ws-debug";

/**
 * useTurnState — the HUD floor-indicator data source (PRD 0016 Annexe A.2 /
 * issue 0108).
 *
 * The voice loop emits a `turn_state` event on EVERY `TurnFsm` transition
 * (`bob.voice_loop._emit_turn_state`). It rides the `event_bus_v2` ring buffer
 * and surfaces on `/ws/debug` as a `DebugEvent` with `category === "voice"` and
 * `payload.ws_event = {type:"turn_state", turn_id, from, to, reason, ts}` (the
 * `emit_event` wrapper nests the wire payload under `ws_event`). The `.to` field
 * is the state the FSM just entered — that is who has the floor.
 *
 * This hook owns its OWN `/ws/debug` socket (symmetric with `useDebugWs`, with
 * the same exponential-backoff reconnect so it survives backend restarts / Vite
 * HMR) and tracks ONLY the latest floor state — it does not retain the event
 * stream. The floor starts at `idle` and reflects each `turn_state.to` as it
 * arrives; on `voice_stop` the FSM itself transitions back to `idle`, so no
 * client-side reset is needed.
 *
 * Why a separate socket rather than reusing `useDebugWs`: that hook is mounted
 * in the dedicated debug window, not the HUD. Sharing would couple the HUD to
 * the debug window's lifecycle. The `/ws/debug` firehose is cheap (the backend
 * already broadcasts it) and replays a snapshot on connect, so the indicator
 * rehydrates to the last known floor state on a fresh mount.
 */

/** Backend host — symmetric with `useDebugWs` / `frontend/src/config.ts`. */
const DEBUG_WS_URL = "ws://127.0.0.1:8000/ws/debug";

const BACKOFF_STEPS_MS = [500, 1000, 2000, 4000, 8000, 10000];

/** The four `TurnFsm` states (mirror of `bob.turn_fsm`). `idle` is the resting
 * floor (nobody has it); the other three map to listening / thinking / speaking
 * in the indicator. */
export type FloorState = "idle" | "user_speaking" | "thinking" | "bob_speaking";

const FLOOR_STATES: ReadonlySet<string> = new Set<FloorState>([
  "idle",
  "user_speaking",
  "thinking",
  "bob_speaking",
]);

/** Narrow an arbitrary `turn_state.to` value to a {@link FloorState}; unknown
 * strings (forward-compat with a future FSM state) are ignored by the caller. */
function asFloorState(value: unknown): FloorState | null {
  return typeof value === "string" && FLOOR_STATES.has(value) ? (value as FloorState) : null;
}

/** Extract the floor state from a `/ws/debug` event, or `null` when it is not a
 * voice `turn_state` frame. Defensive against malformed / partial payloads. */
export function floorFromEvent(event: DebugEvent): FloorState | null {
  if (event.category !== "voice") return null;
  const wsEvent = (event.payload as { ws_event?: unknown }).ws_event;
  if (typeof wsEvent !== "object" || wsEvent === null) return null;
  const frame = wsEvent as { type?: unknown; to?: unknown };
  if (frame.type !== "turn_state") return null;
  return asFloorState(frame.to);
}

/** Subscribe to the live floor state. Returns the latest `turn_state.to` (or
 * `idle` before any voice turn). Re-renders only when the floor actually
 * changes. */
export function useTurnState(): FloorState {
  const [floor, setFloor] = useState<FloorState>("idle");

  const wsRef = useRef<WebSocket | null>(null);
  const reconnectTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const attemptRef = useRef(0);
  const closedByUserRef = useRef(false);

  useEffect(() => {
    closedByUserRef.current = false;

    const connect = () => {
      if (closedByUserRef.current) return;

      const ws = new WebSocket(DEBUG_WS_URL);
      wsRef.current = ws;

      ws.onopen = () => {
        attemptRef.current = 0;
      };

      ws.onmessage = (event) => {
        if (typeof event.data !== "string") return;
        try {
          const parsed = JSON.parse(event.data) as DebugEvent;
          const next = floorFromEvent(parsed);
          if (next === null) return;
          // Only re-render on a real change (the firehose carries many
          // non-voice frames; this guards against redundant updates).
          setFloor((prev) => (prev === next ? prev : next));
        } catch {
          // Ignore malformed frames silently — best-effort like the debug feed.
        }
      };

      ws.onerror = () => {
        // The browser follows up with `close`; nothing to do here.
      };

      ws.onclose = () => {
        wsRef.current = null;
        if (closedByUserRef.current) return;
        const attempt = attemptRef.current;
        const delay = BACKOFF_STEPS_MS[Math.min(attempt, BACKOFF_STEPS_MS.length - 1)];
        attemptRef.current = attempt + 1;
        reconnectTimerRef.current = setTimeout(connect, delay);
      };
    };

    connect();

    return () => {
      closedByUserRef.current = true;
      if (reconnectTimerRef.current !== null) {
        clearTimeout(reconnectTimerRef.current);
        reconnectTimerRef.current = null;
      }
      const ws = wsRef.current;
      wsRef.current = null;
      if (ws) {
        ws.onopen = null;
        ws.onmessage = null;
        ws.onerror = null;
        ws.onclose = null;
        if (ws.readyState === WebSocket.OPEN || ws.readyState === WebSocket.CONNECTING) {
          ws.close();
        }
      }
    };
  }, []);

  return floor;
}
