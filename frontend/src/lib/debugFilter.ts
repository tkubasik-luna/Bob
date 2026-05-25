import { type DebugEvent, type DebugFilters, SEVERITY_ORDER } from "../types/ws-debug";

/**
 * Pure, allocation-light predicate that decides whether a single event passes
 * the active filter state. Exported alongside :func:`filterEvents` so callers
 * can short-circuit on a hot path without paying the cost of a full array
 * copy. The comparison is "Set membership AND severity rank `>=` threshold".
 *
 * PRD: prd/0005-debug-view.md — slice: issues/0040-debug-view-toolbar.md
 */
export function passesFilters(event: DebugEvent, filters: DebugFilters): boolean {
  if (!filters.categoriesOn.has(event.category)) return false;
  return SEVERITY_ORDER[event.severity] >= SEVERITY_ORDER[filters.severityThreshold];
}

/**
 * Return a new array containing only the events that pass `filters`,
 * preserving input order. The caller is expected to wrap this in `useMemo`
 * keyed on `(events, filters)` so we don't reallocate on every render.
 */
export function filterEvents(events: readonly DebugEvent[], filters: DebugFilters): DebugEvent[] {
  return events.filter((e) => passesFilters(e, filters));
}
