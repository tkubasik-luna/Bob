import { useEffect, useRef } from "react";
import { useDebugWs } from "../../hooks/useDebugWs";
import type { DebugEvent } from "../../types/ws-debug";

/**
 * Bare tracer-bullet feed for the debug window (PRD 0005, slice 0038).
 *
 * Single column, monospace, newest at the bottom, auto-scrolls to the
 * latest event on every append via a layout-less `useEffect`. No toolbar,
 * no filters, no expand, no smart scroll — those land in slices 0040 /
 * 0041 / 0042. Each line is rendered as
 * `[HH:MM:SS.mmm] [category] summary`.
 */
export function DebugView() {
  const { events } = useDebugWs();
  const containerRef = useRef<HTMLDivElement | null>(null);
  const eventCount = events.length;

  // Tail-style autoscroll: jump to the bottom whenever a new event lands.
  // Intentionally naive — slice 0042 adds "pause on user scroll-up" and a
  // "N new events" badge. For now we always follow. The dep on
  // `eventCount` is the *trigger*, not consumed inside the body, so biome
  // can't see it — silence the rule because the dep is intentional.
  // biome-ignore lint/correctness/useExhaustiveDependencies: eventCount is the autoscroll trigger
  useEffect(() => {
    const el = containerRef.current;
    if (!el) return;
    el.scrollTop = el.scrollHeight;
  }, [eventCount]);

  return (
    <div
      ref={containerRef}
      style={{
        position: "absolute",
        inset: 0,
        overflowY: "auto",
        background: "#02060e",
        color: "#dfefff",
        fontFamily: '"JetBrains Mono", ui-monospace, monospace',
        fontSize: "12px",
        lineHeight: "1.5",
        padding: "12px 16px",
        boxSizing: "border-box",
      }}
    >
      {events.length === 0 ? (
        <div style={{ opacity: 0.45 }}>En attente d'événements…</div>
      ) : (
        events.map((event, idx) => <DebugLine key={`${event.ts}-${idx}`} event={event} />)
      )}
    </div>
  );
}

function DebugLine({ event }: { event: DebugEvent }) {
  return (
    <div style={{ whiteSpace: "pre-wrap", wordBreak: "break-word" }}>
      <span style={{ opacity: 0.55 }}>[{formatTimestamp(event.ts)}]</span>{" "}
      <span style={{ opacity: 0.7 }}>[{event.category}]</span> {event.summary}
    </div>
  );
}

/**
 * Strip the date prefix from the wire-format timestamp so the feed shows
 * `14:23:01.123` instead of the full `2026-05-25T14:23:01.123Z`. Falls
 * back to the raw string if the format is unexpected — the tracer slice
 * should never crash on a malformed timestamp.
 */
function formatTimestamp(iso: string): string {
  const t = iso.indexOf("T");
  if (t < 0) return iso;
  const tail = iso.slice(t + 1);
  // Drop the trailing `Z` so the display stays compact.
  return tail.endsWith("Z") ? tail.slice(0, -1) : tail;
}
