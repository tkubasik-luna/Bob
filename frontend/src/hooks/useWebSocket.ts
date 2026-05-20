import { useCallback, useEffect, useRef, useState } from "react";
import type { ClientMessage, ConnectionStatus, ServerMessage } from "../types/ws";

type Options = {
  url: string;
  onMessage: (msg: ServerMessage) => void;
};

type UseWebSocketResult = {
  status: ConnectionStatus;
  send: (msg: ClientMessage) => void;
};

const BACKOFF_STEPS_MS = [500, 1000, 2000, 4000, 8000, 10000];

/**
 * Wraps the native WebSocket with:
 *  - exponential backoff reconnect (500ms → 10s ceiling),
 *  - outbound queue that flushes on reconnect,
 *  - connection-status reporting.
 *
 * The `onMessage` callback is read via a ref so callers don't have to memoize it.
 */
export function useWebSocket({ url, onMessage }: Options): UseWebSocketResult {
  const [status, setStatus] = useState<ConnectionStatus>("connecting");

  const wsRef = useRef<WebSocket | null>(null);
  const queueRef = useRef<string[]>([]);
  const attemptRef = useRef(0);
  const reconnectTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const onMessageRef = useRef(onMessage);
  const closedByUserRef = useRef(false);

  // Keep latest callback reachable from stable handlers.
  useEffect(() => {
    onMessageRef.current = onMessage;
  }, [onMessage]);

  const flushQueue = useCallback(() => {
    const ws = wsRef.current;
    if (!ws || ws.readyState !== WebSocket.OPEN) return;
    const queued = queueRef.current;
    queueRef.current = [];
    for (const raw of queued) {
      ws.send(raw);
    }
  }, []);

  const connect = useCallback(() => {
    if (closedByUserRef.current) return;
    setStatus("connecting");

    const ws = new WebSocket(url);
    wsRef.current = ws;

    ws.onopen = () => {
      attemptRef.current = 0;
      setStatus("open");
      flushQueue();
    };

    ws.onmessage = (event) => {
      if (typeof event.data !== "string") return;
      try {
        const parsed = JSON.parse(event.data) as ServerMessage;
        onMessageRef.current(parsed);
      } catch {
        // Ignore malformed frames silently for now; logging arrives in 0007.
      }
    };

    ws.onerror = () => {
      // The browser will follow up with `close`; nothing else to do here.
    };

    ws.onclose = () => {
      wsRef.current = null;
      setStatus("closed");
      if (closedByUserRef.current) return;

      const attempt = attemptRef.current;
      const delay = BACKOFF_STEPS_MS[Math.min(attempt, BACKOFF_STEPS_MS.length - 1)];
      attemptRef.current = attempt + 1;
      reconnectTimerRef.current = setTimeout(connect, delay);
    };
  }, [url, flushQueue]);

  useEffect(() => {
    closedByUserRef.current = false;
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
  }, [connect]);

  const send = useCallback((msg: ClientMessage) => {
    const raw = JSON.stringify(msg);
    const ws = wsRef.current;
    if (ws && ws.readyState === WebSocket.OPEN) {
      ws.send(raw);
    } else {
      queueRef.current.push(raw);
    }
  }, []);

  return { status, send };
}
