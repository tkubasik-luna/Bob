import type { ChangeEvent } from "react";
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
};

/**
 * Top-of-window filter strip for the debug view. Renders one chip per
 * category (toggleable) and one `<select>` for the severity threshold.
 *
 * Defaults and behaviour are owned by the parent (`DebugView`); the toolbar
 * is a controlled component that only emits change intents. No persistence
 * in v1 — each app session resets to "all categories ON, threshold `info`".
 *
 * PRD: prd/0005-debug-view.md — slice: issues/0040-debug-view-toolbar.md
 */
export function DebugToolbar({ filters, onToggleCategory, onChangeSeverity }: DebugToolbarProps) {
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
      <div style={{ marginLeft: "auto", display: "flex", alignItems: "center", gap: "6px" }}>
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
