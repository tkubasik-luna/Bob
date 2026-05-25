import { type CSSProperties, type MouseEvent, memo, useCallback, useMemo, useState } from "react";
import {
  shortTurnId,
  turnIdColor,
  turnIdHighlightBg,
  turnIdHighlightOutline,
} from "../../lib/turnColor";
import type { DebugEvent, DebugSeverity } from "../../types/ws-debug";
import { CATEGORY_PALETTE, CATEGORY_SHORT_LABEL } from "./DebugToolbar";

type DebugRowProps = {
  event: DebugEvent;
  /**
   * `turn_id` of the currently-highlighted turn (set by clicking a chip in
   * any row), or `null` when no highlight is active. The row renders its
   * "highlighted" variant when this matches its own non-null `turn_id`.
   */
  highlightedTurnId: string | null;
  /**
   * Bubbled to the parent so it can lift the highlighted-turn state and
   * schedule the 5s auto-clear. Receives the clicked row's `turn_id`.
   */
  onTurnClick: (turnId: string) => void;
};

/**
 * Severity-driven text color for a feed row. Mirrors `DebugView.severityColor`
 * — kept private to this module so each row file owns its own visual rules.
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

/**
 * Strip the date prefix from the wire-format timestamp so the feed shows
 * `14:23:01.123` instead of the full `2026-05-25T14:23:01.123Z`. Falls back
 * to the raw string when the format is unexpected — the tracer slice should
 * never crash on a malformed timestamp.
 */
function formatTimestamp(iso: string): string {
  const t = iso.indexOf("T");
  if (t < 0) return iso;
  const tail = iso.slice(t + 1);
  return tail.endsWith("Z") ? tail.slice(0, -1) : tail;
}

/**
 * Render a single debug feed row with click-to-expand and a per-`turn_id`
 * color chip.
 *
 * Expand toggles a JSON dump of the event `payload` plus the full `source` /
 * `turn_id` / `correlation_id`. The expand state is local to the row — the
 * parent doesn't need to track which rows are open. The `turn_id` chip is
 * `stopPropagation`'d so clicking it never toggles the expand.
 *
 * The pretty-printed JSON is memoized on the `payload` identity so even a
 * 30-message LLM `messages` array doesn't get re-`JSON.stringify`'d on every
 * parent re-render.
 *
 * Wrapped in `React.memo` so rows whose props haven't shifted skip render
 * cycles triggered by sibling state changes (e.g. one row's expand toggle
 * shouldn't re-render the other N-1 rows).
 *
 * PRD: prd/0005-debug-view.md — slice: issues/0041-debug-view-row-expand.md
 */
function DebugRowImpl({ event, highlightedTurnId, onTurnClick }: DebugRowProps) {
  const [expanded, setExpanded] = useState(false);
  const palette = CATEGORY_PALETTE[event.category];
  const turnId = event.turn_id;

  // Pre-compute the JSON string only when we have a payload. `useMemo` is
  // cheap-on-equal and keeps the result across expand toggles; without it
  // every expand would re-stringify a potentially large LLM payload.
  const prettyPayload = useMemo(() => JSON.stringify(event.payload, null, 2), [event.payload]);

  const isHighlighted = turnId !== null && highlightedTurnId === turnId;

  const toggleExpand = useCallback(() => {
    setExpanded((v) => !v);
  }, []);

  const onChipClick = useCallback(
    (e: MouseEvent<HTMLButtonElement>) => {
      e.stopPropagation();
      if (turnId !== null) {
        onTurnClick(turnId);
      }
    },
    [turnId, onTurnClick],
  );

  // Outer row container. We use an outline (transparent → colored when
  // highlighted) so the layout doesn't shift between states, and a subtle
  // background tint for visual confirmation.
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

  const turnChipBaseStyle: CSSProperties = {
    display: "inline-block",
    marginLeft: "8px",
    padding: "0 6px",
    borderRadius: "3px",
    fontSize: "10px",
    fontWeight: 600,
    letterSpacing: "0.04em",
    fontFamily: "inherit",
    cursor: "pointer",
    verticalAlign: "baseline",
  };

  return (
    // biome-ignore lint/a11y/useKeyWithClickEvents: the debug view is a dev-only window; full keyboard semantics are out of scope for v1
    <div style={rowStyle} onClick={toggleExpand}>
      <span style={{ opacity: 0.55 }}>[{formatTimestamp(event.ts)}]</span>{" "}
      <span style={categoryChipStyle}>{CATEGORY_SHORT_LABEL[event.category]}</span>
      <span>{event.summary}</span>
      {turnId !== null ? (
        <button
          type="button"
          onClick={onChipClick}
          aria-label={`Highlight turn ${shortTurnId(turnId)}`}
          title={`turn_id: ${turnId}`}
          style={{
            ...turnChipBaseStyle,
            border: `1px solid ${turnIdColor(turnId)}`,
            background: isHighlighted ? turnIdColor(turnId) : "transparent",
            color: isHighlighted ? "#02060e" : turnIdColor(turnId),
          }}
        >
          {shortTurnId(turnId)}
        </button>
      ) : null}
      {expanded ? <ExpandedDetails event={event} prettyPayload={prettyPayload} /> : null}
    </div>
  );
}

/**
 * Inline detail panel rendered below the row when `expanded` is true. Shows
 * the pretty-printed JSON payload plus the immutable metadata that doesn't
 * fit on the summary line. Split out so React can skip re-rendering it when
 * only sibling rows change (its props are stable refs while the parent row
 * is expanded).
 */
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

/**
 * Minimal regex-based JSON syntax highlighter. We avoid pulling in a
 * dependency (`react-json-view`, prism, …) because the payload structure is
 * fully under our control and a 4-token highlighter is enough to make the
 * dump readable. Tokens: object keys (rendered as a key color), strings,
 * numbers, booleans+null. Anything else (commas, braces, whitespace)
 * inherits the parent text color.
 *
 * Memoized on the input string so collapsing/expanding the row doesn't
 * re-tokenize a 30-message LLM dump.
 */
const HighlightedJson = memo(function HighlightedJson({ json }: { json: string }) {
  const tokens = useMemo(() => tokenizeJson(json), [json]);
  return (
    <pre
      style={{
        margin: "4px 0 0 0",
        padding: "8px 10px",
        background: "rgba(0, 0, 0, 0.32)",
        borderRadius: "3px",
        overflowX: "auto",
        whiteSpace: "pre",
        fontFamily: "inherit",
        fontSize: "11px",
        lineHeight: "1.45",
        color: "rgba(223, 239, 255, 0.88)",
      }}
    >
      {tokens.map((tok, i) => (
        // biome-ignore lint/suspicious/noArrayIndexKey: token slices are positional, index is the natural identity
        <span key={i} style={tok.style}>
          {tok.text}
        </span>
      ))}
    </pre>
  );
});

type JsonToken = { text: string; style: CSSProperties };

const TOKEN_STYLE = {
  key: { color: "#7dd3fc" }, // cyan-300
  string: { color: "#bef264" }, // lime-300
  number: { color: "#fcd34d" }, // amber-300
  literal: { color: "#f0abfc" }, // fuchsia-300
  plain: {} as CSSProperties,
} as const satisfies Record<string, CSSProperties>;

/**
 * Single regex with named alternatives — runs in O(n) on the pretty-printed
 * JSON. The `key` arm only matches when the string is immediately followed
 * by `:` so that string *values* don't get colored like keys.
 */
const JSON_TOKEN_RE =
  // strings (potentially as key when followed by :), numbers, true/false/null
  /"(?:\\.|[^"\\])*"(?:\s*:)?|-?\d+(?:\.\d+)?(?:[eE][+-]?\d+)?|\btrue\b|\bfalse\b|\bnull\b/g;

function tokenizeJson(json: string): JsonToken[] {
  const out: JsonToken[] = [];
  let lastIndex = 0;
  for (const match of json.matchAll(JSON_TOKEN_RE)) {
    const start = match.index ?? 0;
    if (start > lastIndex) {
      out.push({ text: json.slice(lastIndex, start), style: TOKEN_STYLE.plain });
    }
    const raw = match[0];
    if (raw.startsWith('"')) {
      // String literal — key vs value depending on trailing `:`
      if (raw.endsWith(":") || raw.match(/"\s*:$/)) {
        // Split off the trailing `:` (and any whitespace) so it stays
        // in the plain color.
        const colonIdx = raw.lastIndexOf(":");
        const stringPart = raw.slice(0, colonIdx).trimEnd();
        const between = raw.slice(stringPart.length, colonIdx);
        out.push({ text: stringPart, style: TOKEN_STYLE.key });
        if (between.length > 0) out.push({ text: between, style: TOKEN_STYLE.plain });
        out.push({ text: ":", style: TOKEN_STYLE.plain });
      } else {
        out.push({ text: raw, style: TOKEN_STYLE.string });
      }
    } else if (raw === "true" || raw === "false" || raw === "null") {
      out.push({ text: raw, style: TOKEN_STYLE.literal });
    } else {
      out.push({ text: raw, style: TOKEN_STYLE.number });
    }
    lastIndex = start + raw.length;
  }
  if (lastIndex < json.length) {
    out.push({ text: json.slice(lastIndex), style: TOKEN_STYLE.plain });
  }
  return out;
}

export const DebugRow = memo(DebugRowImpl);
