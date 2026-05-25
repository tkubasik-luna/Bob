import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useDebugWs } from "../../hooks/useDebugWs";
import { filterEvents } from "../../lib/debugFilter";
import {
  DEBUG_CATEGORIES,
  type DebugCategory,
  type DebugFilters,
  type DebugSeverity,
} from "../../types/ws-debug";
import { DebugRow } from "./DebugRow";
import { DebugToolbar } from "./DebugToolbar";

/** Auto-clear delay for the per-`turn_id` highlight, in milliseconds. */
const TURN_HIGHLIGHT_TTL_MS = 5000;

/**
 * Debug window root. Renders a filter toolbar at the top and a scrollable
 * monospace feed below it. Filter state (active categories + severity
 * threshold) lives in this component rather than in `useDebugWs` because the
 * hook's mission is socket lifecycle / buffering — keeping UI-only state out
 * of it preserves a focused contract and lets future consumers subscribe to
 * the raw firehose without inheriting toolbar concerns.
 *
 * Row rendering (click-to-expand + per-`turn_id` color chip) is delegated to
 * `DebugRow`. The "currently highlighted turn_id" lives here (one source of
 * truth for the whole feed) and propagates down to every row so they can
 * render the highlighted variant. Auto-clear is a single shared timeout —
 * resetting when a new turn is clicked.
 *
 * Auto-scroll is the slice 0038 naïve "jump to bottom on every append".
 * Slice 0042 will refine it with pause-on-scroll-up + "N new" badge.
 *
 * PRD: prd/0005-debug-view.md — slice: issues/0041-debug-view-row-expand.md
 */
export function DebugView() {
  const { events } = useDebugWs();
  const containerRef = useRef<HTMLDivElement | null>(null);

  const [filters, setFilters] = useState<DebugFilters>(() => ({
    categoriesOn: new Set<DebugCategory>(DEBUG_CATEGORIES),
    severityThreshold: "info",
  }));

  const [highlightedTurnId, setHighlightedTurnId] = useState<string | null>(null);

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

  const onTurnClick = useCallback((turnId: string) => {
    setHighlightedTurnId(turnId);
  }, []);

  // Single shared 5s auto-clear timer. Re-arms whenever the highlighted
  // turn_id changes (including from one chip to another mid-flight). Cleared
  // on unmount or before the next arm fires.
  useEffect(() => {
    if (highlightedTurnId === null) return;
    const handle = window.setTimeout(() => {
      setHighlightedTurnId(null);
    }, TURN_HIGHLIGHT_TTL_MS);
    return () => {
      window.clearTimeout(handle);
    };
  }, [highlightedTurnId]);

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
          filteredEvents.map((event, idx) => (
            <DebugRow
              key={`${event.ts}-${idx}`}
              event={event}
              highlightedTurnId={highlightedTurnId}
              onTurnClick={onTurnClick}
            />
          ))
        )}
      </div>
    </div>
  );
}
