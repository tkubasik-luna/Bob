/**
 * Transform a flat `DebugEvent[]` stream into the hierarchical tree the
 * debug view renders since slice 0044. The output is a sealed union of node
 * variants (`TurnNode | TaskNode | LlmCallNode | EventNode`) so every
 * recursive UI surface only needs a single `switch` on `node.kind`.
 *
 * Grouping rules (see `issues/0044-debug-view-grouped-tree.md`):
 *  1. LLM call start/end events sharing the same `correlation_id` fuse into a
 *     single `LlmCallNode` carrying `model`, `latencyMs`, `tokensIn`, `tokensOut`.
 *     A start without a matching end (in-flight call) keeps `end == undefined`.
 *  2. Tasks nest via `parent_task_id`: a task B whose spawn event was emitted
 *     inside task A becomes a child of TaskNode A.
 *  3. Events with a `turn_id` group under a `TurnNode`.
 *  4. Events with neither `turn_id` nor `parent_task_id` surface as root
 *     `EventNode`s, chronologically interleaved with the turns.
 *
 * The transform is pure (no side effects, no I/O) and preserves chronological
 * order at every level — the input list is assumed to be wall-clock-ordered
 * (which it is on the wire: events are pushed in emit order and replayed in
 * the same order from the snapshot ring buffer).
 *
 * PRD: prd/0006-debug-view-grouped-tree.md — slice: issues/0044-debug-view-grouped-tree.md
 */

import type { DebugEvent, DebugSeverity } from "../types/ws-debug";
import { SEVERITY_ORDER } from "../types/ws-debug";

/** Tree node discriminated by `kind`. */
export type TreeNode = TurnNode | TaskNode | LlmCallNode | EventNode;

export type TurnNode = {
  kind: "turn";
  /** Stable id used as React key + slice 0045 expand-state Map key. */
  id: string;
  turnId: string;
  /** First `input` event's `summary` (or first event's `summary` as fallback). */
  firstInputText: string | null;
  startTs: string;
  endTs: string;
  /** Number of raw events under this turn (recursive). */
  eventCount: number;
  /** Number of task nodes under this turn (recursive). */
  taskCount: number;
  /** Highest severity seen in the subtree — drives the header status icon. */
  maxSeverity: DebugSeverity;
  children: TreeNode[];
};

export type TaskNode = {
  kind: "task";
  id: string;
  taskId: string;
  title: string | null;
  goal: string | null;
  startTs: string;
  endTs: string;
  eventCount: number;
  /** Recursive count of nested TaskNodes (excludes self). */
  taskCount: number;
  maxSeverity: DebugSeverity;
  children: TreeNode[];
};

export type LlmCallNode = {
  kind: "llm";
  id: string;
  correlationId: string;
  model: string | null;
  /** Wall-clock latency derived from `(end.ts - start.ts)` in ms. */
  latencyMs: number | null;
  tokensIn: number | null;
  tokensOut: number | null;
  /** Start event (always present — an LlmCallNode is created from a start). */
  start: DebugEvent;
  /** End event when paired; `undefined` for an in-flight call. */
  end: DebugEvent | undefined;
  maxSeverity: DebugSeverity;
};

export type EventNode = {
  kind: "event";
  id: string;
  event: DebugEvent;
};

/** Marker on a payload-level `task_id` used to discover a task's parent. */
type TaskMeta = {
  title: string | null;
  goal: string | null;
  /** Task this task was spawned from (event-level `parent_task_id` of the
   *  spawn emit), or `null` for top-level tasks. */
  parentTaskId: string | null;
  /** Turn this task first appeared under. */
  turnId: string | null;
};

/** Sentinel string for "no parent task" / "no turn" bucket keys. */
const ROOT_BUCKET = "__root__";

/** Numeric severity rank of an event, monotonic with `SEVERITY_ORDER`. */
function severityRank(s: DebugSeverity): number {
  return SEVERITY_ORDER[s];
}

function maxSeverity(a: DebugSeverity, b: DebugSeverity): DebugSeverity {
  return severityRank(a) >= severityRank(b) ? a : b;
}

/** Read `parent_task_id` tolerating the legacy "absent field" shape. */
function parentTaskOf(e: DebugEvent): string | null {
  return e.parent_task_id ?? null;
}

/** Heuristic: an LLM event is the "end" of a pair when it carries a
 *  `latency_ms` in the payload OR is an explicit error/no-choices outcome.
 *  The start event from `llm_client` always has `tokens_prompt_estimate`
 *  (and never `latency_ms`); the end variants all carry `latency_ms`. */
function isLlmEnd(e: DebugEvent): boolean {
  if (e.category !== "llm") return false;
  const p = e.payload as Record<string, unknown>;
  return typeof p.latency_ms === "number";
}

function isLlmStart(e: DebugEvent): boolean {
  if (e.category !== "llm") return false;
  return !isLlmEnd(e);
}

/**
 * Parse the wire-format ISO timestamp into a `Date.getTime()` integer. Falls
 * back to `0` on malformed input so a single bad event can't take down the
 * whole transform.
 */
function tsMs(iso: string): number {
  const t = Date.parse(iso);
  return Number.isFinite(t) ? t : 0;
}

/**
 * Pretty-print extraction of "title" + "goal" from a task spawn payload.
 * The wire payload uses snake_case (`task_id`, `title`, `goal`). We tolerate
 * absence of either field — sub-task progress / done events also carry
 * `title` but no `goal`, which is fine.
 */
function readTaskMeta(payload: Record<string, unknown>): {
  title: string | null;
  goal: string | null;
} {
  const title = typeof payload.title === "string" ? payload.title : null;
  const goal = typeof payload.goal === "string" ? payload.goal : null;
  return { title, goal };
}

/**
 * Main entry point. Returns a fresh array of root `TreeNode`s. Pure: never
 * mutates the input array or any event.
 */
export function groupEvents(events: readonly DebugEvent[]): TreeNode[] {
  if (events.length === 0) return [];

  // Pass 1: discover all task ids and their metadata. A task id can be
  // discovered two ways: (a) as `payload.task_id` on a spawn/progress/done
  // emit, (b) as `parent_task_id` on any event emitted inside the task.
  // The richest metadata comes from the spawn event, so we prefer values
  // from any event with both `payload.title` and `payload.task_id` set.
  const taskMeta = new Map<string, TaskMeta>();

  function touchTask(taskId: string, defaults: Partial<TaskMeta>): void {
    const existing = taskMeta.get(taskId);
    if (existing === undefined) {
      taskMeta.set(taskId, {
        title: defaults.title ?? null,
        goal: defaults.goal ?? null,
        parentTaskId: defaults.parentTaskId ?? null,
        turnId: defaults.turnId ?? null,
      });
      return;
    }
    // Upgrade nulls with new info; never overwrite a known value.
    if (existing.title === null && defaults.title != null) existing.title = defaults.title;
    if (existing.goal === null && defaults.goal != null) existing.goal = defaults.goal;
    if (existing.parentTaskId === null && defaults.parentTaskId != null) {
      existing.parentTaskId = defaults.parentTaskId;
    }
    if (existing.turnId === null && defaults.turnId != null) existing.turnId = defaults.turnId;
  }

  for (const e of events) {
    const eventParent = parentTaskOf(e);
    if (eventParent !== null) {
      // Events emitted INSIDE a task — the task's id is `eventParent`.
      touchTask(eventParent, { turnId: e.turn_id });
    }
    // Spawn / progress / done emits carry `payload.task_id` and (for spawn)
    // a `title` / `goal`. The task's parent is the spawn event's
    // *event-level* `parent_task_id`.
    const p = e.payload as Record<string, unknown>;
    const payloadTaskId = typeof p.task_id === "string" ? p.task_id : null;
    if (payloadTaskId !== null) {
      const meta = readTaskMeta(p);
      touchTask(payloadTaskId, {
        title: meta.title,
        goal: meta.goal,
        parentTaskId: eventParent,
        turnId: e.turn_id,
      });
    }
  }

  // Pass 2: pre-build a TaskNode shell for every discovered task id. We need
  // them to exist up front so children can attach by id during pass 3 even
  // when the parent task's first event arrives later in the stream.
  const taskNodes = new Map<string, TaskNode>();
  for (const [taskId, meta] of taskMeta) {
    taskNodes.set(taskId, {
      kind: "task",
      id: `task:${taskId}`,
      taskId,
      title: meta.title,
      goal: meta.goal,
      startTs: "",
      endTs: "",
      eventCount: 0,
      taskCount: 0,
      maxSeverity: "trace",
      children: [],
    });
  }

  // Pass 3: walk the events in order, producing tree nodes. We track the
  // per-turn child list (and an interleaved root list for `turn_id == null
  // && parent_task_id == null` events) by stable id. LLM call pairing
  // happens in this pass — a start event reserves an LlmCallNode and a
  // matching end event fills its tail without producing a separate node.
  const turnNodes = new Map<string, TurnNode>();
  const rootList: TreeNode[] = [];
  const llmInFlight = new Map<string, LlmCallNode>();

  /** Append a node into the appropriate container (turn child / task child /
   *  root list) given an event's `turn_id` + `parent_task_id`. */
  function placeNode(node: TreeNode, owningTurnId: string | null, owningTaskId: string | null) {
    if (owningTaskId !== null) {
      const task = taskNodes.get(owningTaskId);
      if (task !== undefined) {
        task.children.push(node);
        return;
      }
      // Fallback (unreachable in practice — pass 1 discovers every task).
    }
    if (owningTurnId !== null) {
      const turn = getOrCreateTurn(owningTurnId);
      turn.children.push(node);
      return;
    }
    rootList.push(node);
  }

  function getOrCreateTurn(turnId: string): TurnNode {
    let node = turnNodes.get(turnId);
    if (node === undefined) {
      node = {
        kind: "turn",
        id: `turn:${turnId}`,
        turnId,
        firstInputText: null,
        startTs: "",
        endTs: "",
        eventCount: 0,
        taskCount: 0,
        maxSeverity: "trace",
        children: [],
      };
      turnNodes.set(turnId, node);
      rootList.push(node);
    }
    return node;
  }

  for (const e of events) {
    const eventTaskId = parentTaskOf(e);

    // LLM start: reserve a node, track in-flight by correlation_id.
    if (isLlmStart(e) && e.correlation_id !== null) {
      const p = e.payload as Record<string, unknown>;
      const model = typeof p.model === "string" ? p.model : null;
      const node: LlmCallNode = {
        kind: "llm",
        id: `llm:${e.correlation_id}`,
        correlationId: e.correlation_id,
        model,
        latencyMs: null,
        tokensIn: null,
        tokensOut: null,
        start: e,
        end: undefined,
        maxSeverity: e.severity,
      };
      llmInFlight.set(e.correlation_id, node);
      placeNode(node, e.turn_id, eventTaskId);
      continue;
    }

    // LLM end: fold into the in-flight node if we have one, otherwise treat
    // as a standalone EventNode (out-of-order replay / missing start).
    if (isLlmEnd(e) && e.correlation_id !== null) {
      const reserved = llmInFlight.get(e.correlation_id);
      if (reserved !== undefined) {
        reserved.end = e;
        const startMs = tsMs(reserved.start.ts);
        const endMs = tsMs(e.ts);
        reserved.latencyMs = endMs - startMs;
        const p = e.payload as Record<string, unknown>;
        if (typeof p.tokens_in === "number") reserved.tokensIn = p.tokens_in;
        if (typeof p.tokens_out === "number") reserved.tokensOut = p.tokens_out;
        if (reserved.model === null && typeof p.model === "string") reserved.model = p.model;
        reserved.maxSeverity = maxSeverity(reserved.maxSeverity, e.severity);
        llmInFlight.delete(e.correlation_id);
        continue;
      }
      // fall through: render as plain event
    }

    // Default: an EventNode placed in the right container.
    const node: EventNode = {
      kind: "event",
      id: `event:${e.ts}:${e.source}:${e.correlation_id ?? ""}:${e.summary}`,
      event: e,
    };
    placeNode(node, e.turn_id, eventTaskId);
  }

  // Pass 4: place each TaskNode under its parent task (if known), else its
  // turn, else the root list. Pass 3 only ever pushes events/llm nodes via
  // `placeNode`; tasks are exclusively placed here so we can't double-insert.
  for (const [taskId, meta] of taskMeta) {
    const task = taskNodes.get(taskId);
    if (task === undefined) continue;
    if (meta.parentTaskId !== null) {
      const parent = taskNodes.get(meta.parentTaskId);
      if (parent !== undefined) {
        parent.children.push(task);
        continue;
      }
    }
    if (meta.turnId !== null) {
      const turn = getOrCreateTurn(meta.turnId);
      turn.children.push(task);
    } else {
      rootList.push(task);
    }
  }

  // Pass 5: compute aggregates (eventCount / taskCount / maxSeverity /
  // startTs / endTs / firstInputText) bottom-up.
  for (const turn of turnNodes.values()) {
    computeTurnAggregates(turn);
  }
  for (const task of taskNodes.values()) {
    computeTaskAggregates(task);
  }

  // Re-sort root list chronologically (turns + orphan events interleaved).
  // Each root node has a startTs computed above (turns) or its event ts.
  rootList.sort((a, b) => tsMs(rootStartTs(a)) - tsMs(rootStartTs(b)));

  return rootList;
}

function rootStartTs(n: TreeNode): string {
  switch (n.kind) {
    case "turn":
      return n.startTs;
    case "task":
      return n.startTs;
    case "llm":
      return n.start.ts;
    case "event":
      return n.event.ts;
  }
}

/** Aggregate event/task counts, severity, and timestamps across a subtree. */
function aggregateSubtree(children: TreeNode[]): {
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
        maxSev = maxSeverity(maxSev, child.event.severity);
        bump(child.event.ts);
        break;
      case "llm":
        // LLM nodes are themselves *one* logical event (a call), but the
        // start + optional end are visible payloads. We count them as a
        // single event for header purposes — matches the operator's mental
        // model (one LLM call line in the feed).
        eventCount += 1;
        maxSev = maxSeverity(maxSev, child.maxSeverity);
        bump(child.start.ts);
        if (child.end !== undefined) bump(child.end.ts);
        break;
      case "task":
        taskCount += 1 + child.taskCount;
        eventCount += child.eventCount;
        maxSev = maxSeverity(maxSev, child.maxSeverity);
        bump(child.startTs);
        bump(child.endTs);
        break;
      case "turn":
        // Should not happen — turns never nest inside other nodes. Defensive.
        eventCount += child.eventCount;
        taskCount += child.taskCount;
        maxSev = maxSeverity(maxSev, child.maxSeverity);
        bump(child.startTs);
        bump(child.endTs);
        break;
    }
  }

  return { eventCount, taskCount, maxSeverity: maxSev, startTs, endTs };
}

function computeTaskAggregates(task: TaskNode): void {
  // Recurse into children that are tasks first so their aggregates are ready.
  for (const c of task.children) {
    if (c.kind === "task") computeTaskAggregates(c);
  }
  const agg = aggregateSubtree(task.children);
  task.eventCount = agg.eventCount;
  task.taskCount = agg.taskCount;
  task.maxSeverity = agg.maxSeverity;
  task.startTs = agg.startTs;
  task.endTs = agg.endTs;
}

function computeTurnAggregates(turn: TurnNode): void {
  for (const c of turn.children) {
    if (c.kind === "task") computeTaskAggregates(c);
  }
  const agg = aggregateSubtree(turn.children);
  turn.eventCount = agg.eventCount;
  turn.taskCount = agg.taskCount;
  turn.maxSeverity = agg.maxSeverity;
  turn.startTs = agg.startTs;
  turn.endTs = agg.endTs;
  turn.firstInputText = extractFirstInputText(turn);
}

/**
 * Find a representative "input" text for a turn header. Preference order:
 *   1. First descendant EventNode whose `category === 'input'` — its
 *      `summary` is what the user typed.
 *   2. Else: `summary` of the first descendant event of any kind.
 *
 * We walk recursively because the user-input event may live inside a task
 * (rare, but the contract should still degrade gracefully).
 */
function extractFirstInputText(turn: TurnNode): string | null {
  let firstAny: string | null = null;

  function visit(nodes: TreeNode[]): string | null {
    for (const n of nodes) {
      if (n.kind === "event") {
        if (firstAny === null) firstAny = n.event.summary;
        if (n.event.category === "input") return n.event.summary;
      } else if (n.kind === "llm") {
        if (firstAny === null) firstAny = n.start.summary;
      } else if (n.kind === "task") {
        const found = visit(n.children);
        if (found !== null) return found;
      }
    }
    return null;
  }

  const inputHit = visit(turn.children);
  return inputHit ?? firstAny;
}
