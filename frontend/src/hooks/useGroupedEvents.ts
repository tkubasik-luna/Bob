/**
 * `useMemo` wrapper around :func:`groupEvents`. Lives next to the other
 * debug-view hooks so `DebugView` can pull a single import for the grouped
 * tree without having to remember to memoize the pure helper itself.
 *
 * Identity guarantee: when called with the same `events` array reference,
 * the returned tree is also the same reference. Critical for downstream
 * `React.memo` row components (and the slice 0045 expand-state Map keyed by
 * node id) to skip re-renders on unrelated state changes.
 *
 * PRD: prd/0006-debug-view-grouped-tree.md — slice: issues/0044-debug-view-grouped-tree.md
 */

import { useMemo } from "react";
import { groupEvents, type TreeNode } from "../lib/groupEvents";
import type { DebugEvent } from "../types/ws-debug";

export function useGroupedEvents(events: readonly DebugEvent[]): TreeNode[] {
  return useMemo(() => groupEvents(events), [events]);
}
