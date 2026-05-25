import { useEffect, useRef, useState } from "react";
import type { DebugEvent } from "../types/ws-debug";

/** Backend host — symmetric with `frontend/src/config.ts` (`WS_URL`). */
const DEBUG_WS_URL = "ws://127.0.0.1:8000/ws/debug";

const BACKOFF_STEPS_MS = [500, 1000, 2000, 4000, 8000, 10000];

type UseDebugWsResult = {
  events: DebugEvent[];
};

/**
 * Subscribe to the `/ws/debug` firehose for the lifetime of the consumer
 * component and expose every received :class:`DebugEvent` as an append-only
 * state array.
 *
 * Slice 0038 is the tracer-bullet shape: the hook simply accumulates events
 * in mount order — no filtering, no pause, no cap. Later slices (0040 /
 * 0041 / 0042) add toolbar filters, pause / clear, and a bounded local
 * buffer. Connection is reopened with exponential backoff on close so the
 * window stays useful across backend restarts and Vite HMR reloads.
 */
export function useDebugWs(): UseDebugWsResult {
  const [events, setEvents] = useState<DebugEvent[]>([]);

  // Owns the live socket, the reconnect timer, and the back-off attempt
  // counter. Kept in refs so the cleanup closure sees the latest values
  // without retriggering the effect.
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
          setEvents((prev) => [...prev, parsed]);
        } catch {
          // Ignore malformed frames silently — debug feed is best-effort.
        }
      };

      ws.onerror = () => {
        // The browser will follow up with `close`; nothing to do here.
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

  return { events };
}
