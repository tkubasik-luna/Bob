import type { CSSProperties, ChangeEvent } from "react";
import {
  DEBUG_CATEGORIES,
  DEBUG_SEVERITIES,
  type DebugCategory,
  type DebugFilters,
  type DebugSeverity,
} from "../../types/ws-debug";

type DebugToolbarProps = {
  filters: DebugFilters;
  onToggleCategory: (category: DebugCategory) => void;
  onChangeSeverity: (severity: DebugSeverity) => void;
  /** Current pause state — drives the Pause/Resume button label + active styling. */
  paused: boolean;
  /** Toggle pause/resume; also exposed via the Space keybind on the window. */
  onTogglePause: () => void;
  /** Clear the visible feed locally — backend ring buffer is untouched. */
  onClear: () => void;
  /** Visible event count, shown as `Clear (N)` for quick at-a-glance feedback. */
  visibleCount: number;
  /**
   * Events buffered while paused. Shown as a chip next to the Pause label so
   * the operator can tell how much will flush on resume without clicking.
   */
  pendingCount: number;
};

/**
 * Top-of-window filter strip for the debug view. Renders one chip per
 * category (toggleable), a `<select>` for the severity threshold, and (since
 * slice 0042) two action buttons: Pause/Resume and Clear.
 *
 * Defaults and behaviour are owned by the parent (`DebugView`); the toolbar
 * is a controlled component that only emits change intents. No persistence
 * in v1 — each app session resets to "all categories ON, threshold `info`".
 *
 * PRD: prd/0005-debug-view.md — slice: issues/0042-debug-view-tail-scroll.md
 */
export function DebugToolbar({
  filters,
  onToggleCategory,
  onChangeSeverity,
  paused,
  onTogglePause,
  onClear,
  visibleCount,
  pendingCount,
}: DebugToolbarProps) {
  const onSeverityChange = (e: ChangeEvent<HTMLSelectElement>) => {
    onChangeSeverity(e.target.value as DebugSeverity);
  };

  return (
    <div
      style={{
        display: "flex",
        flexWrap: "wrap",
        alignItems: "center",
        gap: "8px",
        padding: "8px 12px",
        borderBottom: "1px solid rgba(255, 255, 255, 0.08)",
        background: "rgba(255, 255, 255, 0.02)",
        fontFamily: '"JetBrains Mono", ui-monospace, monospace',
        fontSize: "12px",
        color: "var(--ink, #dfefff)",
      }}
    >
      <div style={{ display: "flex", flexWrap: "wrap", gap: "6px" }}>
        {DEBUG_CATEGORIES.map((category) => {
          const on = filters.categoriesOn.has(category);
          const palette = CATEGORY_PALETTE[category];
          return (
            <button
              key={category}
              type="button"
              onClick={() => onToggleCategory(category)}
              aria-pressed={on}
              aria-label={`Toggle ${category}`}
              style={{
                cursor: "pointer",
                fontFamily: "inherit",
                fontSize: "11px",
                fontWeight: 600,
                letterSpacing: "0.04em",
                textTransform: "uppercase",
                padding: "3px 8px",
                borderRadius: "999px",
                border: `1px solid ${palette.border}`,
                background: on ? palette.bg : "transparent",
                color: on ? palette.fg : "rgba(223, 239, 255, 0.35)",
                opacity: on ? 1 : 0.45,
                transition: "opacity 120ms ease, background 120ms ease",
              }}
            >
              {CATEGORY_SHORT_LABEL[category]}
            </button>
          );
        })}
      </div>
      <div style={{ marginLeft: "auto", display: "flex", alignItems: "center", gap: "8px" }}>
        <button
          type="button"
          onClick={onTogglePause}
          aria-pressed={paused}
          aria-label={paused ? "Resume feed" : "Pause feed"}
          title="Toggle pause (Space)"
          style={actionButtonStyle(paused)}
        >
          {paused ? "▶ Resume" : "⏸ Pause"}
          {paused && pendingCount > 0 ? (
            <span style={pendingBadgeStyle}>+{pendingCount}</span>
          ) : null}
        </button>
        <button
          type="button"
          onClick={onClear}
          aria-label="Clear visible feed"
          title="Clear visible feed (backend buffer kept)"
          style={actionButtonStyle(false)}
        >
          {visibleCount > 0 ? `Clear (${visibleCount})` : "Clear"}
        </button>
        <label
          htmlFor="debug-severity"
          style={{
            opacity: 0.6,
            fontSize: "11px",
            textTransform: "uppercase",
            letterSpacing: "0.04em",
          }}
        >
          Severity ≥
        </label>
        <select
          id="debug-severity"
          value={filters.severityThreshold}
          onChange={onSeverityChange}
          style={{
            fontFamily: "inherit",
            fontSize: "11px",
            padding: "2px 6px",
            borderRadius: "4px",
            border: "1px solid rgba(255, 255, 255, 0.12)",
            background: "rgba(0, 0, 0, 0.3)",
            color: "var(--ink, #dfefff)",
          }}
        >
          {DEBUG_SEVERITIES.map((sev) => (
            <option key={sev} value={sev}>
              {sev}
            </option>
          ))}
        </select>
      </div>
    </div>
  );
}

/**
 * Shared style for Pause and Clear. Same dimensions as a category chip so the
 * toolbar reads as a single horizontal band. `active = true` renders the
 * "currently engaged" variant used by Pause when the feed is frozen.
 */
function actionButtonStyle(active: boolean): CSSProperties {
  return {
    cursor: "pointer",
    fontFamily: "inherit",
    fontSize: "11px",
    fontWeight: 600,
    letterSpacing: "0.04em",
    textTransform: "uppercase",
    padding: "3px 10px",
    borderRadius: "999px",
    border: `1px solid ${active ? "rgba(125, 211, 252, 0.65)" : "rgba(255, 255, 255, 0.18)"}`,
    background: active ? "rgba(125, 211, 252, 0.22)" : "transparent",
    color: active ? "#dbeafe" : "rgba(223, 239, 255, 0.78)",
    display: "inline-flex",
    alignItems: "center",
    gap: "6px",
    transition: "background 120ms ease, border-color 120ms ease, color 120ms ease",
  };
}

/** Inline pending counter rendered on the Pause button while frozen. */
const pendingBadgeStyle: CSSProperties = {
  display: "inline-block",
  padding: "0 6px",
  borderRadius: "999px",
  background: "rgba(2, 6, 14, 0.55)",
  color: "#dbeafe",
  fontSize: "10px",
  fontWeight: 700,
  letterSpacing: "0.02em",
};

/**
 * Visual palette for the 7 categories. PRD-defined mapping (input=blue,
 * llm=purple, decision=cyan, task=green, output=yellow, voice=pink,
 * system=gray). Tailwind hex equivalents picked to keep the chips visually
 * distinct against the dark `--bg` background. Mirrored by the row chip
 * renderer in `DebugView`.
 */
export const CATEGORY_PALETTE: Record<DebugCategory, { fg: string; bg: string; border: string }> = {
  input: {
    fg: "#dbeafe",
    bg: "rgba(59, 130, 246, 0.28)",
    border: "rgba(59, 130, 246, 0.55)",
  },
  llm: {
    fg: "#ede9fe",
    bg: "rgba(168, 85, 247, 0.28)",
    border: "rgba(168, 85, 247, 0.55)",
  },
  decision: {
    fg: "#cffafe",
    bg: "rgba(34, 211, 238, 0.28)",
    border: "rgba(34, 211, 238, 0.55)",
  },
  task: {
    fg: "#dcfce7",
    bg: "rgba(34, 197, 94, 0.28)",
    border: "rgba(34, 197, 94, 0.55)",
  },
  output: {
    fg: "#fef9c3",
    bg: "rgba(234, 179, 8, 0.28)",
    border: "rgba(234, 179, 8, 0.55)",
  },
  voice: {
    fg: "#fce7f3",
    bg: "rgba(236, 72, 153, 0.28)",
    border: "rgba(236, 72, 153, 0.55)",
  },
  system: {
    fg: "#e5e7eb",
    bg: "rgba(148, 163, 184, 0.24)",
    border: "rgba(148, 163, 184, 0.50)",
  },
};

/**
 * Compact label shown both on the toolbar chip and on the inline row chip.
 * Shortened where the full word would crowd the timestamp column.
 */
export const CATEGORY_SHORT_LABEL: Record<DebugCategory, string> = {
  input: "INPUT",
  llm: "LLM",
  decision: "DECISION",
  task: "TASK",
  output: "OUT",
  voice: "VOICE",
  system: "SYS",
};
