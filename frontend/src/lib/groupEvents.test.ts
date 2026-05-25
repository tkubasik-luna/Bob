import { describe, expect, test } from "vitest";
import type { DebugCategory, DebugEvent, DebugSeverity } from "../types/ws-debug";
import { type LlmCallNode, type TaskNode, type TurnNode, groupEvents } from "./groupEvents";

let _tsCounter = 0;
function makeEvent(opts: Partial<DebugEvent> & { category?: DebugCategory } = {}): DebugEvent {
  _tsCounter += 1;
  const tsBase = new Date(Date.UTC(2026, 0, 1, 0, 0, 0, _tsCounter));
  return {
    ts: opts.ts ?? tsBase.toISOString(),
    category: opts.category ?? "system",
    severity: opts.severity ?? "info",
    source: opts.source ?? "test.case",
    summary: opts.summary ?? "evt",
    payload: opts.payload ?? {},
    turn_id: opts.turn_id ?? null,
    correlation_id: opts.correlation_id ?? null,
    parent_task_id: opts.parent_task_id ?? null,
    replayed: opts.replayed ?? false,
  };
}

function tsAt(msFromBase: number): string {
  return new Date(Date.UTC(2026, 0, 1, 0, 0, 0, msFromBase)).toISOString();
}

describe("groupEvents - LLM fusion", () => {
  test("LLM start + end with same correlation_id collapse into one LlmCallNode", () => {
    const start: DebugEvent = makeEvent({
      category: "llm",
      summary: "LLM call démarré",
      payload: { messages: [{ role: "user", content: "hi" }], model: "gpt-x" },
      correlation_id: "abc",
      turn_id: "T1",
      ts: tsAt(100),
    });
    const end: DebugEvent = makeEvent({
      category: "llm",
      summary: "LLM call terminé",
      payload: { latency_ms: 250, tokens_in: 42, tokens_out: 17, model: "gpt-x" },
      correlation_id: "abc",
      turn_id: "T1",
      ts: tsAt(350),
    });
    const tree = groupEvents([start, end]);
    expect(tree).toHaveLength(1);
    const turn = tree[0] as TurnNode;
    expect(turn.kind).toBe("turn");
    expect(turn.children).toHaveLength(1);
    const llm = turn.children[0] as LlmCallNode;
    expect(llm.kind).toBe("llm");
    expect(llm.correlationId).toBe("abc");
    expect(llm.model).toBe("gpt-x");
    expect(llm.latencyMs).toBe(250);
    expect(llm.tokensIn).toBe(42);
    expect(llm.tokensOut).toBe(17);
    expect(llm.end).toBeDefined();
  });

  test("LLM start without end stays in-flight (end == undefined)", () => {
    const start = makeEvent({
      category: "llm",
      payload: { messages: [], model: "gpt-x" },
      correlation_id: "abc",
      turn_id: "T1",
    });
    const tree = groupEvents([start]);
    const turn = tree[0] as TurnNode;
    const llm = turn.children[0] as LlmCallNode;
    expect(llm.kind).toBe("llm");
    expect(llm.end).toBeUndefined();
    expect(llm.latencyMs).toBeNull();
  });
});

describe("groupEvents - task nesting", () => {
  test("task B with parent_task_id == A becomes child of TaskNode A", () => {
    const events: DebugEvent[] = [
      // A spawn (emitted with no enclosing task)
      makeEvent({
        category: "task",
        summary: "spawn A",
        payload: { task_id: "A", title: "A title", goal: "A goal" },
        turn_id: "T1",
      }),
      // event inside A
      makeEvent({
        category: "system",
        summary: "inside A",
        parent_task_id: "A",
        turn_id: "T1",
      }),
      // B spawn (emitted from within A → event-level parent_task_id == A)
      makeEvent({
        category: "task",
        summary: "spawn B",
        payload: { task_id: "B", title: "B title" },
        parent_task_id: "A",
        turn_id: "T1",
      }),
      // event inside B
      makeEvent({
        category: "system",
        summary: "inside B",
        parent_task_id: "B",
        turn_id: "T1",
      }),
    ];
    const tree = groupEvents(events);
    const turn = tree[0] as TurnNode;
    const taskA = turn.children.find((c) => c.kind === "task" && c.taskId === "A") as TaskNode;
    expect(taskA).toBeDefined();
    expect(taskA.title).toBe("A title");
    expect(taskA.goal).toBe("A goal");
    const taskB = taskA.children.find((c) => c.kind === "task" && c.taskId === "B") as TaskNode;
    expect(taskB).toBeDefined();
    expect(taskB.title).toBe("B title");
    expect(turn.taskCount).toBe(2);
  });
});

describe("groupEvents - orphan events", () => {
  test("events with no turn_id and no parent_task_id surface as root EventNodes in order", () => {
    const e1 = makeEvent({ summary: "orphan 1", ts: tsAt(10) });
    const e2 = makeEvent({ summary: "orphan 2", ts: tsAt(20) });
    const tree = groupEvents([e1, e2]);
    expect(tree).toHaveLength(2);
    expect(tree[0].kind).toBe("event");
    expect(tree[1].kind).toBe("event");
    if (tree[0].kind === "event") expect(tree[0].event.summary).toBe("orphan 1");
    if (tree[1].kind === "event") expect(tree[1].event.summary).toBe("orphan 2");
  });

  test("orphan events stay chronologically interleaved with turns", () => {
    const orphan1 = makeEvent({ summary: "before", ts: tsAt(10) });
    const turnEvt = makeEvent({ summary: "in T1", turn_id: "T1", ts: tsAt(20) });
    const orphan2 = makeEvent({ summary: "after", ts: tsAt(30) });
    const tree = groupEvents([orphan1, turnEvt, orphan2]);
    expect(tree).toHaveLength(3);
    expect(tree[0].kind).toBe("event");
    expect(tree[1].kind).toBe("turn");
    expect(tree[2].kind).toBe("event");
  });
});

describe("groupEvents - multiple turns interleaved", () => {
  test("two turns at the root remain ordered by first event ts", () => {
    const events: DebugEvent[] = [
      makeEvent({ summary: "T1 input", category: "input", turn_id: "T1", ts: tsAt(10) }),
      makeEvent({ summary: "T2 input", category: "input", turn_id: "T2", ts: tsAt(20) }),
      makeEvent({ summary: "T1 follow", turn_id: "T1", ts: tsAt(30) }),
    ];
    const tree = groupEvents(events);
    expect(tree).toHaveLength(2);
    const [first, second] = tree as TurnNode[];
    expect(first.turnId).toBe("T1");
    expect(second.turnId).toBe("T2");
  });
});

describe("groupEvents - firstInputText extraction", () => {
  test("uses summary of first event with category 'input'", () => {
    const events = [
      makeEvent({ category: "system", summary: "noise", turn_id: "T1", ts: tsAt(10) }),
      makeEvent({ category: "input", summary: "what is the weather", turn_id: "T1", ts: tsAt(20) }),
    ];
    const tree = groupEvents(events);
    const turn = tree[0] as TurnNode;
    expect(turn.firstInputText).toBe("what is the weather");
  });

  test("falls back to first event's summary when no input category present", () => {
    const events = [
      makeEvent({ category: "system", summary: "first thing", turn_id: "T1", ts: tsAt(10) }),
      makeEvent({ category: "system", summary: "second thing", turn_id: "T1", ts: tsAt(20) }),
    ];
    const tree = groupEvents(events);
    const turn = tree[0] as TurnNode;
    expect(turn.firstInputText).toBe("first thing");
  });
});

describe("groupEvents - maxSeverity aggregation", () => {
  test("turn header severity == max of its descendants", () => {
    const events: DebugEvent[] = [
      makeEvent({ severity: "info" as DebugSeverity, turn_id: "T1" }),
      makeEvent({ severity: "warn" as DebugSeverity, turn_id: "T1" }),
      makeEvent({ severity: "error" as DebugSeverity, turn_id: "T1" }),
    ];
    const tree = groupEvents(events);
    const turn = tree[0] as TurnNode;
    expect(turn.maxSeverity).toBe("error");
  });

  test("task subtree severity bubbles up to its parent turn", () => {
    const events: DebugEvent[] = [
      makeEvent({
        category: "task",
        summary: "spawn A",
        severity: "info",
        payload: { task_id: "A", title: "A" },
        turn_id: "T1",
      }),
      makeEvent({ severity: "error", parent_task_id: "A", turn_id: "T1" }),
    ];
    const tree = groupEvents(events);
    const turn = tree[0] as TurnNode;
    expect(turn.maxSeverity).toBe("error");
    const task = turn.children.find((c) => c.kind === "task") as TaskNode;
    expect(task.maxSeverity).toBe("error");
  });
});

describe("groupEvents - self-cycle guard (regression)", () => {
  // SubAgentRunner emits progress/done/fail/ask_user from INSIDE start_task(T)
  // scope. Those events carry both `parent_task_id == T` (from the ContextVar)
  // AND `payload.task_id == T` (for client identification). Without the guard,
  // Pass 1 would set T's parentTaskId to T itself → Pass 4 pushes T into its
  // own children → infinite recursion in `computeTaskAggregates` (stack
  // overflow crashing the debug window).
  test("event with parent_task_id == payload.task_id does not create a self-cycle", () => {
    const events: DebugEvent[] = [
      makeEvent({
        category: "task",
        summary: "spawn A",
        payload: { task_id: "A", title: "A" },
        turn_id: "T1",
      }),
      makeEvent({
        category: "task",
        summary: "A done",
        payload: { task_id: "A", title: "A", result: "ok" },
        parent_task_id: "A",
        turn_id: "T1",
      }),
    ];
    // The two assertions matter: (1) the call returns at all (no overflow),
    // (2) task A appears as a child of the turn — NOT as a child of itself.
    const tree = groupEvents(events);
    const turn = tree[0] as TurnNode;
    const task = turn.children.find((c) => c.kind === "task") as TaskNode;
    expect(task.taskId).toBe("A");
    expect(task.children.some((c) => c.kind === "task" && c.taskId === "A")).toBe(false);
  });
});
