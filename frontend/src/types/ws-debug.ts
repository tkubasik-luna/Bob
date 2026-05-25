/**
 * Wire contract for `/ws/debug` — mirror of `bob.debug_log.DebugEvent`.
 *
 * Keep in sync with `backend/src/bob/debug_log.py` (`to_dict()` shape and
 * the `DebugCategory` / `DebugSeverity` literal unions).
 */

export type DebugCategory = "input" | "llm" | "decision" | "task" | "output" | "voice" | "system";

export type DebugSeverity = "trace" | "debug" | "info" | "warn" | "error";

/**
 * Ordered tuple of all 7 categories. Single source of truth for both the
 * toolbar chip iteration and the default filter set construction. Frozen
 * `as const` so callers get the literal-union element type via `[number]`.
 */
export const DEBUG_CATEGORIES = [
  "input",
  "llm",
  "decision",
  "task",
  "output",
  "voice",
  "system",
] as const satisfies readonly DebugCategory[];

/**
 * Ordered tuple of severities from lowest (most verbose) to highest. Drives
 * both the `<select>` dropdown order and the `SEVERITY_ORDER` index map used
 * for `>=` comparison when filtering.
 */
export const DEBUG_SEVERITIES = [
  "trace",
  "debug",
  "info",
  "warn",
  "error",
] as const satisfies readonly DebugSeverity[];

/**
 * Numeric rank of each severity, lowest-to-highest. An event passes the
 * threshold when `SEVERITY_ORDER[event.severity] >= SEVERITY_ORDER[threshold]`.
 */
export const SEVERITY_ORDER: Record<DebugSeverity, number> = {
  trace: 0,
  debug: 1,
  info: 2,
  warn: 3,
  error: 4,
};

/**
 * UI-side filter state for the debug feed. Owned by `DebugView` in v1 — see
 * `issues/0040-debug-view-toolbar.md` for the rationale. Defaults: every
 * category ON, severity threshold `info` (so `trace`/`debug` are hidden).
 */
export type DebugFilters = {
  categoriesOn: ReadonlySet<DebugCategory>;
  severityThreshold: DebugSeverity;
};

export type DebugEvent = {
  /** ISO 8601 UTC instant with millisecond precision, e.g. `2026-05-25T14:23:01.123Z`. */
  ts: string;
  category: DebugCategory;
  severity: DebugSeverity;
  /** Dotted-path source of the emit site, e.g. `orchestrator.process_user_message`. */
  source: string;
  /** One-line human-readable description rendered as the primary text. */
  summary: string;
  /** Free-form detail payload — LLM messages, exception trace, ... */
  payload: Record<string, unknown>;
  /** UUID-like turn grouping; `null` until slice 0039 wires the ContextVar. */
  turn_id: string | null;
  /** Pairs `*_start` / `*_end` events for long ops (LLM, sub-task). */
  correlation_id: string | null;
  /**
   * Slice 0043: id of the enclosing sub-task when the event was emitted from
   * within a `SubAgentRunner.run` scope. `null` (or absent on legacy
   * snapshots) means "no enclosing sub-task". Optional in the TS type so a
   * pre-0043 buffer replay still decodes cleanly.
   */
  parent_task_id?: string | null;
  /** `true` when the event was streamed from the ring-buffer snapshot at
   * subscribe time; `false` for live events. */
  replayed: boolean;
};
