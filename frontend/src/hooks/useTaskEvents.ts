import { useEffect, useRef, useState } from "react";
import type { DebugEvent, TaskWsFrame } from "../types/ws-debug";

/** Backend host — symmetric with `frontend/src/config.ts` (`WS_URL`). */
const TASK_WS_BASE = "ws://127.0.0.1:8000/ws/task";

const BACKOFF_STEPS_MS = [500, 1000, 2000, 4000, 8000, 10000];

export type UseTaskEventsResult = {
  /** Every event observed for this task — snapshot replay first (chronological),
   * then live tail in arrival order. Stable identity across re-renders. */
  events: DebugEvent[];
  /** `true` once the initial snapshot frame has been received and merged.
   * Lets the consumer distinguish "still loading" from "no events yet". */
  ready: boolean;
};

/**
 * Subscribe to `/ws/task/{taskId}` for the lifetime of the consumer
 * component.
 *
 * PRD 0006 / issue 0052: the WS uses a snapshot-then-tail protocol in a
 * single session. The first server frame is `{type: "snapshot", events}`;
 * each subsequent frame is `{type: "tail", event}`. The hook merges both
 * phases into a single ordered `events` array so the overlay renderer
 * doesn't need to care.
 *
 * Dedupe is by `(ts, source, summary)` tuple — a small overlap window can
 * exist between the snapshot copy and the live subscription. Two events
 * with the same tuple are treated as duplicates; the second is dropped.
 *
 * Auto-reconnect with exponential backoff so the overlay stays useful
 * across backend restarts and Vite HMR. When the hook unmounts (overlay
 * closed) the socket is closed cleanly.
 */
export function useTaskEvents(taskId: string | null): UseTaskEventsResult {
  const [events, setEvents] = useState<DebugEvent[]>([]);
  const [ready, setReady] = useState(false);

  const wsRef = useRef<WebSocket | null>(null);
  const reconnectTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const attemptRef = useRef(0);
  const closedByUserRef = useRef(false);
  // Set of `(ts|source|summary)` triplets we've already appended so
  // snapshot/tail overlap doesn't double-render. Reset on a fresh
  // connection (each `taskId` change re-opens the socket).
  const seenKeysRef = useRef<Set<string>>(new Set());

  useEffect(() => {
    // Clear state on every `taskId` change so a stale overlay (closing
    // task A, opening task B) doesn't bleed events.
    setEvents([]);
    setReady(false);
    seenKeysRef.current = new Set();

    if (taskId === null) {
      // Idle state — no socket needed. The component owning the overlay
      // typically renders nothing in this case.
      return;
    }

    closedByUserRef.current = false;

    const connect = () => {
      if (closedByUserRef.current) return;

      const ws = new WebSocket(`${TASK_WS_BASE}/${encodeURIComponent(taskId)}`);
      wsRef.current = ws;

      ws.onopen = () => {
        attemptRef.current = 0;
      };

      ws.onmessage = (event) => {
        if (typeof event.data !== "string") return;
        let parsed: TaskWsFrame;
        try {
          parsed = JSON.parse(event.data) as TaskWsFrame;
        } catch {
          return;
        }
        if (parsed.type === "snapshot") {
          // Phase 1 — replace any prior buffered events with the
          // server-authoritative snapshot. Mark ready so the consumer
          // can hide its "loading…" indicator.
          const fresh = parsed.events;
          const seen = new Set<string>();
          const merged: DebugEvent[] = [];
          for (const e of fresh) {
            const key = `${e.ts}|${e.source}|${e.summary}`;
            if (seen.has(key)) continue;
            seen.add(key);
            merged.push(e);
          }
          seenKeysRef.current = seen;
          setEvents(merged);
          setReady(true);
          return;
        }
        if (parsed.type === "tail") {
          const e = parsed.event;
          const key = `${e.ts}|${e.source}|${e.summary}`;
          if (seenKeysRef.current.has(key)) return;
          seenKeysRef.current.add(key);
          setEvents((prev) => [...prev, e]);
        }
      };

      ws.onerror = () => {
        // Browser follows up with `close`.
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
  }, [taskId]);

  return { events, ready };
}
