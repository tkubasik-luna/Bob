/**
 * Wire contract for `/ws/debug` — mirror of `bob.debug_log.DebugEvent`.
 *
 * Keep in sync with `backend/src/bob/debug_log.py` (`to_dict()` shape and
 * the `DebugCategory` / `DebugSeverity` literal unions).
 */

export type DebugCategory = "input" | "llm" | "decision" | "task" | "output" | "voice" | "system";

export type DebugSeverity = "trace" | "debug" | "info" | "warn" | "error";

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
  /** `true` when the event was streamed from the ring-buffer snapshot at
   * subscribe time; `false` for live events. */
  replayed: boolean;
};
