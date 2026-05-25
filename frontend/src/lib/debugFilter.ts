import type { TreeNode } from "./groupEvents";
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

/**
 * Drop turn/task subtrees that contain no event matching `filters`. LLM call
 * nodes are kept iff at least one of their start/end events passes; event
 * nodes are kept iff their underlying event passes. The transform is
 * non-mutating: every node and every retained children array is a fresh
 * allocation, so callers can safely diff against the previous tree.
 *
 * Aggregates (`eventCount`, `taskCount`, `maxSeverity`, `startTs`, `endTs`)
 * are recomputed on the surviving subtree so header counts reflect the
 * post-filter state. `firstInputText` is preserved when the original input
 * event survives the filter; otherwise we recompute it from whatever's left.
 *
 * PRD: prd/0006-debug-view-grouped-tree.md — slice: issues/0044-debug-view-grouped-tree.md
 */
export function pruneEmptyNodes(
  tree: readonly TreeNode[],
  filters: DebugFilters,
): TreeNode[] {
  const out: TreeNode[] = [];
  for (const node of tree) {
    const pruned = pruneNode(node, filters);
    if (pruned !== null) out.push(pruned);
  }
  return out;
}

function pruneNode(node: TreeNode, filters: DebugFilters): TreeNode | null {
  switch (node.kind) {
    case "event":
      return passesFilters(node.event, filters) ? node : null;
    case "llm": {
      const startOk = passesFilters(node.start, filters);
      const endOk = node.end !== undefined ? passesFilters(node.end, filters) : false;
      return startOk || endOk ? node : null;
    }
    case "task": {
      const children: TreeNode[] = [];
      for (const c of node.children) {
        const pruned = pruneNode(c, filters);
        if (pruned !== null) children.push(pruned);
      }
      if (children.length === 0) return null;
      return rebuildTask(node, children);
    }
    case "turn": {
      const children: TreeNode[] = [];
      for (const c of node.children) {
        const pruned = pruneNode(c, filters);
        if (pruned !== null) children.push(pruned);
      }
      if (children.length === 0) return null;
      return rebuildTurn(node, children);
    }
  }
}

// --- aggregate recomputation (mirrors groupEvents' bottom-up pass) --------

import type { TaskNode, TurnNode } from "./groupEvents";
import type { DebugSeverity } from "../types/ws-debug";

function tsMs(iso: string): number {
  const t = Date.parse(iso);
  return Number.isFinite(t) ? t : 0;
}

function severityMax(a: DebugSeverity, b: DebugSeverity): DebugSeverity {
  return SEVERITY_ORDER[a] >= SEVERITY_ORDER[b] ? a : b;
}

function aggregate(children: TreeNode[]): {
  eventCount: number;
  taskCount: number;
  maxSeverity: DebugSeverity;
  startTs: string;
  endTs: string;
} {
  let eventCount = 0;
  let taskCount = 0;
  let maxSev: DebugSeverity = "trace";
  let startTs = "";
  let endTs = "";
  function bump(ts: string) {
    if (ts === "") return;
    if (startTs === "" || tsMs(ts) < tsMs(startTs)) startTs = ts;
    if (endTs === "" || tsMs(ts) > tsMs(endTs)) endTs = ts;
  }
  for (const child of children) {
    switch (child.kind) {
      case "event":
        eventCount += 1;
        maxSev = severityMax(maxSev, child.event.severity);
        bump(child.event.ts);
        break;
      case "llm":
        eventCount += 1;
        maxSev = severityMax(maxSev, child.maxSeverity);
        bump(child.start.ts);
        if (child.end !== undefined) bump(child.end.ts);
        break;
      case "task":
        taskCount += 1 + child.taskCount;
        eventCount += child.eventCount;
        maxSev = severityMax(maxSev, child.maxSeverity);
        bump(child.startTs);
        bump(child.endTs);
        break;
      case "turn":
        eventCount += child.eventCount;
        taskCount += child.taskCount;
        maxSev = severityMax(maxSev, child.maxSeverity);
        bump(child.startTs);
        bump(child.endTs);
        break;
    }
  }
  return { eventCount, taskCount, maxSeverity: maxSev, startTs, endTs };
}

function rebuildTask(orig: TaskNode, children: TreeNode[]): TaskNode {
  const agg = aggregate(children);
  return {
    kind: "task",
    id: orig.id,
    taskId: orig.taskId,
    title: orig.title,
    goal: orig.goal,
    startTs: agg.startTs,
    endTs: agg.endTs,
    eventCount: agg.eventCount,
    taskCount: agg.taskCount,
    maxSeverity: agg.maxSeverity,
    children,
  };
}

function rebuildTurn(orig: TurnNode, children: TreeNode[]): TurnNode {
  const agg = aggregate(children);
  // Try to keep firstInputText if the input event still survives.
  let firstInputText = orig.firstInputText;
  if (firstInputText !== null) {
    const stillThere = treeContainsSummary(children, firstInputText);
    if (!stillThere) firstInputText = fallbackFirstInputText(children);
  } else {
    firstInputText = fallbackFirstInputText(children);
  }
  return {
    kind: "turn",
    id: orig.id,
    turnId: orig.turnId,
    firstInputText,
    startTs: agg.startTs,
    endTs: agg.endTs,
    eventCount: agg.eventCount,
    taskCount: agg.taskCount,
    maxSeverity: agg.maxSeverity,
    children,
  };
}

function treeContainsSummary(nodes: TreeNode[], summary: string): boolean {
  for (const n of nodes) {
    if (n.kind === "event" && n.event.summary === summary) return true;
    if (n.kind === "llm" && n.start.summary === summary) return true;
    if (n.kind === "task" && treeContainsSummary(n.children, summary)) return true;
  }
  return false;
}

function fallbackFirstInputText(nodes: TreeNode[]): string | null {
  let firstAny: string | null = null;
  for (const n of nodes) {
    if (n.kind === "event") {
      if (firstAny === null) firstAny = n.event.summary;
      if (n.event.category === "input") return n.event.summary;
    } else if (n.kind === "llm") {
      if (firstAny === null) firstAny = n.start.summary;
    } else if (n.kind === "task") {
      const found = fallbackFirstInputText(n.children);
      if (found !== null) return found;
    }
  }
  return firstAny;
}
