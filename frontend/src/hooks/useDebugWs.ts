import { useCallback, useEffect, useRef, useState } from "react";
import type { DebugEvent } from "../types/ws-debug";

/** Backend host — symmetric with `frontend/src/config.ts` (`WS_URL`). */
const DEBUG_WS_URL = "ws://127.0.0.1:8000/ws/debug";

const BACKOFF_STEPS_MS = [500, 1000, 2000, 4000, 8000, 10000];

type UseDebugWsResult = {
  /** Visible events — what the feed renders. Frozen while `paused = true`. */
  events: DebugEvent[];
  /** `true` while pause is engaged; new events go to the pending buffer. */
  paused: boolean;
  /** Toggle / set the pause state. Mirrors the `useState` setter shape. */
  setPaused: (next: boolean | ((prev: boolean) => boolean)) => void;
  /**
   * Empty the visible feed and any buffered-during-pause events. Backend ring
   * buffer is left untouched, so hide/re-show the debug window replays from
   * the snapshot again.
   */
  clear: () => void;
  /** Count of events received while paused, waiting to be flushed on resume. */
  pendingCount: number;
};

/**
 * Subscribe to the `/ws/debug` firehose for the lifetime of the consumer
 * component and expose every received :class:`DebugEvent` as an append-only
 * state array.
 *
 * Slice 0042 adds the `tail -f` ergonomics around the raw firehose: a
 * `paused` flag that diverts incoming frames into an in-memory buffer
 * (`pendingEventsRef`) until the user resumes, plus a `clear()` action that
 * empties the visible feed without touching the backend ring buffer.
 *
 * The pending buffer lives in a `useRef` (not state) so high-frequency frames
 * arriving while paused don't trigger React re-renders — only `pendingCount`
 * does, and it ticks once per arrival. On resume, the ref is flushed into
 * `events` in arrival order (already FIFO) and reset to an empty array.
 *
 * Connection is reopened with exponential backoff on close so the window
 * stays useful across backend restarts and Vite HMR reloads.
 */
export function useDebugWs(): UseDebugWsResult {
  const [events, setEvents] = useState<DebugEvent[]>([]);
  const [paused, setPausedState] = useState(false);
  const [pendingCount, setPendingCount] = useState(0);

  // Owns the live socket, the reconnect timer, and the back-off attempt
  // counter. Kept in refs so the cleanup closure sees the latest values
  // without retriggering the effect.
  const wsRef = useRef<WebSocket | null>(null);
  const reconnectTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const attemptRef = useRef(0);
  const closedByUserRef = useRef(false);

  // Pause-time buffer. While `pausedRef.current` is `true`, the `onmessage`
  // handler appends to this ref instead of `events`. The ref is read at the
  // top of the handler on every frame, so toggling `paused` doesn't need to
  // tear down and rebuild the WebSocket — the next frame just picks the right
  // sink. Kept as a ref (not state) so each frame is O(1) regardless of
  // arrival frequency.
  const pausedRef = useRef(false);
  const pendingEventsRef = useRef<DebugEvent[]>([]);

  // `setPaused` mirrors React's setter shape and keeps the ref / pending
  // buffer in lockstep with state. On a `true → false` transition we flush
  // the buffered events into `events` (chronological order = arrival order =
  // already FIFO) and reset the pending counter.
  //
  // The ref + flush both run synchronously *outside* the React state updater
  // so a frame arriving on the same microtask as a pause toggle sees the new
  // value immediately — putting them inside `setPausedState((prev) => …)` is
  // tempting but defers the side effect until React runs the updater, which
  // can be a tick later under batching and lets a frame slip through into
  // the wrong sink.
  const setPaused = useCallback((next: boolean | ((prev: boolean) => boolean)) => {
    const prev = pausedRef.current;
    const value = typeof next === "function" ? next(prev) : next;
    if (value === prev) return;
    pausedRef.current = value;
    if (prev && !value) {
      // Resuming — flush the pending buffer into the visible feed.
      const pending = pendingEventsRef.current;
      if (pending.length > 0) {
        pendingEventsRef.current = [];
        setEvents((current) => [...current, ...pending]);
        setPendingCount(0);
      }
    }
    setPausedState(value);
  }, []);

  // `clear` wipes both the visible feed and any frames buffered during a
  // pause. The backend ring buffer is intentionally untouched — hiding then
  // re-showing the debug window will replay the full snapshot again.
  const clear = useCallback(() => {
    pendingEventsRef.current = [];
    setEvents([]);
    setPendingCount(0);
  }, []);

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
          if (pausedRef.current) {
            pendingEventsRef.current = [...pendingEventsRef.current, parsed];
            setPendingCount(pendingEventsRef.current.length);
          } else {
            setEvents((prev) => [...prev, parsed]);
          }
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

  return { events, paused, setPaused, clear, pendingCount };
}
