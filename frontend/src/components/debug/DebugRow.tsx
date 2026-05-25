import { type CSSProperties, memo, useCallback, useMemo, useState } from "react";
import { turnIdHighlightBg, turnIdHighlightOutline } from "../../lib/turnColor";
import type { DebugEvent, DebugSeverity } from "../../types/ws-debug";
import { CATEGORY_PALETTE, CATEGORY_SHORT_LABEL } from "./DebugToolbar";
import { HighlightedJson } from "./HighlightedJson";

type DebugRowProps = {
  event: DebugEvent;
  /**
   * `turn_id` of the currently-highlighted turn (set by clicking a chip on a
   * `TurnNode` header), or `null` when no highlight is active. The row
   * renders its "highlighted" variant when this matches its own non-null
   * `turn_id`.
   */
  highlightedTurnId: string | null;
  /**
   * Bubbled to the parent so it can lift the highlighted-turn state and
   * schedule the 5s auto-clear. Kept on the row's props for compatibility
   * with `DebugTree` (which also passes it down to `TurnNode`).
   */
  onTurnClick: (turnId: string) => void;
};

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

function formatTimestamp(iso: string): string {
  const t = iso.indexOf("T");
  if (t < 0) return iso;
  const tail = iso.slice(t + 1);
  return tail.endsWith("Z") ? tail.slice(0, -1) : tail;
}

/**
 * Render a single debug feed leaf row with click-to-expand. Slice 0044
 * dropped the per-row `turn_id` chip — the chip now lives on the parent
 * `TurnNode` header, and the per-`turn_id` color tint comes from there too
 * (slice 0045 wires the border tint on the children container).
 *
 * Expand toggles a JSON dump of the event `payload` plus the full `source`
 * / `turn_id` / `correlation_id`. The expand state is local to the row.
 *
 * Wrapped in `React.memo` so rows whose props haven't shifted skip render
 * cycles triggered by sibling state changes.
 *
 * PRD: prd/0006-debug-view-grouped-tree.md — slice: issues/0044-debug-view-grouped-tree.md
 */
function DebugRowImpl({ event, highlightedTurnId }: DebugRowProps) {
  const [expanded, setExpanded] = useState(false);
  const palette = CATEGORY_PALETTE[event.category];
  const turnId = event.turn_id;

  const prettyPayload = useMemo(() => JSON.stringify(event.payload, null, 2), [event.payload]);

  const isHighlighted = turnId !== null && highlightedTurnId === turnId;

  const toggleExpand = useCallback(() => {
    setExpanded((v) => !v);
  }, []);

  const rowStyle: CSSProperties = {
    whiteSpace: "pre-wrap",
    wordBreak: "break-word",
    color: severityColor(event.severity),
    cursor: "pointer",
    padding: "2px 6px",
    margin: "0 -6px",
    borderRadius: "3px",
    outline:
      isHighlighted && turnId
        ? `1px solid ${turnIdHighlightOutline(turnId)}`
        : "1px solid transparent",
    background: isHighlighted && turnId ? turnIdHighlightBg(turnId) : "transparent",
    transition: "background 160ms ease, outline-color 160ms ease",
  };

  const categoryChipStyle: CSSProperties = {
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
    // biome-ignore lint/a11y/useKeyWithClickEvents: the debug view is a dev-only window; full keyboard semantics are out of scope for v1
    <div style={rowStyle} onClick={toggleExpand}>
      <span style={{ opacity: 0.55 }}>[{formatTimestamp(event.ts)}]</span>{" "}
      <span style={categoryChipStyle}>{CATEGORY_SHORT_LABEL[event.category]}</span>
      <span>{event.summary}</span>
      {expanded ? <ExpandedDetails event={event} prettyPayload={prettyPayload} /> : null}
    </div>
  );
}

function ExpandedDetails({
  event,
  prettyPayload,
}: {
  event: DebugEvent;
  prettyPayload: string;
}) {
  return (
    <div
      style={{
        marginTop: "6px",
        padding: "8px 10px",
        background: "rgba(255, 255, 255, 0.03)",
        border: "1px solid rgba(255, 255, 255, 0.08)",
        borderRadius: "4px",
        fontSize: "11px",
        lineHeight: "1.45",
        color: "rgba(223, 239, 255, 0.85)",
        cursor: "default",
      }}
    >
      <DetailRow label="source" value={event.source} />
      <DetailRow label="turn_id" value={event.turn_id ?? "—"} />
      {event.correlation_id !== null ? (
        <DetailRow label="correlation_id" value={event.correlation_id} />
      ) : null}
      <div
        style={{
          marginTop: "6px",
          opacity: 0.55,
          fontSize: "10px",
          textTransform: "uppercase",
          letterSpacing: "0.04em",
        }}
      >
        payload
      </div>
      <HighlightedJson json={prettyPayload} />
    </div>
  );
}

function DetailRow({ label, value }: { label: string; value: string }) {
  return (
    <div style={{ display: "flex", gap: "8px", marginBottom: "2px" }}>
      <span
        style={{
          opacity: 0.55,
          fontSize: "10px",
          textTransform: "uppercase",
          letterSpacing: "0.04em",
          minWidth: "92px",
        }}
      >
        {label}
      </span>
      <span style={{ wordBreak: "break-all" }}>{value}</span>
    </div>
  );
}

export const DebugRow = memo(DebugRowImpl);
