import { type CSSProperties, useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useDebugWs } from "../../hooks/useDebugWs";
import { filterEvents } from "../../lib/debugFilter";
import {
  DEBUG_CATEGORIES,
  type DebugCategory,
  type DebugEvent,
  type DebugFilters,
  type DebugSeverity,
} from "../../types/ws-debug";
import { CATEGORY_PALETTE, CATEGORY_SHORT_LABEL, DebugToolbar } from "./DebugToolbar";

/**
 * Debug window root. Renders a filter toolbar at the top and a scrollable
 * monospace feed below it. Filter state (active categories + severity
 * threshold) lives in this component rather than in `useDebugWs` because the
 * hook's mission is socket lifecycle / buffering — keeping UI-only state out
 * of it preserves a focused contract and lets future consumers subscribe to
 * the raw firehose without inheriting toolbar concerns.
 *
 * Auto-scroll is the slice 0038 naïve "jump to bottom on every append".
 * Slice 0042 will refine it with pause-on-scroll-up + "N new" badge.
 *
 * PRD: prd/0005-debug-view.md — slice: issues/0040-debug-view-toolbar.md
 */
export function DebugView() {
  const { events } = useDebugWs();
  const containerRef = useRef<HTMLDivElement | null>(null);

  const [filters, setFilters] = useState<DebugFilters>(() => ({
    categoriesOn: new Set<DebugCategory>(DEBUG_CATEGORIES),
    severityThreshold: "info",
  }));

  const onToggleCategory = useCallback((category: DebugCategory) => {
    setFilters((prev) => {
      const next = new Set(prev.categoriesOn);
      if (next.has(category)) {
        next.delete(category);
      } else {
        next.add(category);
      }
      return { ...prev, categoriesOn: next };
    });
  }, []);

  const onChangeSeverity = useCallback((severity: DebugSeverity) => {
    setFilters((prev) => ({ ...prev, severityThreshold: severity }));
  }, []);

  const filteredEvents = useMemo(() => filterEvents(events, filters), [events, filters]);
  const filteredCount = filteredEvents.length;

  // Tail-style autoscroll: jump to the bottom whenever a new visible event
  // lands. Driven by `filteredCount` (not `events.length`) so toggling a
  // filter that removes the latest row doesn't try to scroll past the
  // truncated end. The dep on `filteredCount` is the *trigger*, not consumed
  // inside the body, so biome can't see it — silence the rule because the
  // dep is intentional.
  // biome-ignore lint/correctness/useExhaustiveDependencies: filteredCount is the autoscroll trigger
  useEffect(() => {
    const el = containerRef.current;
    if (!el) return;
    el.scrollTop = el.scrollHeight;
  }, [filteredCount]);

  return (
    <div
      style={{
        position: "absolute",
        inset: 0,
        display: "flex",
        flexDirection: "column",
        background: "var(--bg, #02060e)",
        color: "var(--ink, #dfefff)",
        fontFamily: '"JetBrains Mono", ui-monospace, monospace',
      }}
    >
      <DebugToolbar
        filters={filters}
        onToggleCategory={onToggleCategory}
        onChangeSeverity={onChangeSeverity}
      />
      <div
        ref={containerRef}
        style={{
          flex: 1,
          overflowY: "auto",
          fontSize: "12px",
          lineHeight: "1.5",
          padding: "12px 16px",
          boxSizing: "border-box",
        }}
      >
        {filteredEvents.length === 0 ? (
          <div style={{ opacity: 0.45 }}>
            {events.length === 0
              ? "En attente d'événements…"
              : "Aucun événement ne correspond aux filtres actifs."}
          </div>
        ) : (
          filteredEvents.map((event, idx) => <DebugLine key={`${event.ts}-${idx}`} event={event} />)
        )}
      </div>
    </div>
  );
}

/**
 * Severity-driven text color for a feed row. `warn` reuses the HUD
 * `--warn` token, `error` reuses `--err`; `trace` desaturates to a dim grey
 * so high-frequency rows don't dominate the eye; `debug` / `info` render in
 * the neutral ink color.
 */
function severityColor(severity: DebugSeverity): string {
  switch (severity) {
    case "warn":
      return "var(--warn, #ffb300)";
    case "error":
      return "var(--err, #ff3d3d)";
    case "trace":
      return "rgba(223, 239, 255, 0.40)";
    default:
      return "var(--ink, #dfefff)";
  }
}

function DebugLine({ event }: { event: DebugEvent }) {
  const palette = CATEGORY_PALETTE[event.category];
  const lineStyle: CSSProperties = {
    whiteSpace: "pre-wrap",
    wordBreak: "break-word",
    color: severityColor(event.severity),
  };
  const chipStyle: CSSProperties = {
    display: "inline-block",
    minWidth: "62px",
    textAlign: "center",
    padding: "0 6px",
    marginRight: "8px",
    borderRadius: "3px",
    border: `1px solid ${palette.border}`,
    background: palette.bg,
    color: palette.fg,
    fontSize: "10px",
    fontWeight: 600,
    letterSpacing: "0.04em",
    textTransform: "uppercase",
  };
  return (
    <div style={lineStyle}>
      <span style={{ opacity: 0.55 }}>[{formatTimestamp(event.ts)}]</span>{" "}
      <span style={chipStyle}>{CATEGORY_SHORT_LABEL[event.category]}</span>
      {event.summary}
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
