import { describe, expect, it } from "vitest";
import type { AgentTimelineItem } from "../store/activityFeedStore";
import type { AgentActivityStatus, Task, TaskState } from "../types/ws";
import { buildBobFlow, buildSubFlow } from "./threadFlow";

const reasoning = (text: string): AgentTimelineItem => ({ kind: "reasoning", text });
/** A curated Jarvis spawn chip (`_jarvis_orchestration_chip("délègue", …)`). */
const delegate = (title: string): AgentTimelineItem => ({
  kind: "chip",
  activityKind: "tool_call",
  label: `délègue : ${title}`,
  status: "ok",
});
/** A non-spawn orchestration chip (transmet / annule) — also `tool_call`, but
 * must NOT consume a task slot. */
const orchestration = (label: string): AgentTimelineItem => ({
  kind: "chip",
  activityKind: "tool_call",
  label,
  status: "ok",
});
const tool = (
  label: string,
  status: AgentActivityStatus,
  extra: { args?: string; result?: string } = {},
): AgentTimelineItem => ({ kind: "chip", activityKind: "tool_call", label, status, ...extra });
const lifecycle = (): AgentTimelineItem => ({
  kind: "chip",
  activityKind: "started",
  label: "started",
  status: "info",
});

const task = (id: string, state: TaskState = "running", createdAt = id): Task => ({
  id,
  title: id,
  goal: `goal-${id}`,
  state,
  createdAt,
});

describe("buildBobFlow", () => {
  it("interleaves reasoning runs around a delegated task in arrival order", () => {
    const a = task("a", "running", "2026-01-01T00:00:00Z");
    expect(
      buildBobFlow([reasoning("Je regarde."), delegate("a"), reasoning("Je tiens le fil.")], [a]),
    ).toEqual([
      { kind: "reflection", text: "Je regarde.", source: "reasoning" },
      { kind: "tasks", tasks: [a] },
      { kind: "reflection", text: "Je tiens le fil.", source: "reasoning" },
    ]);
  });

  it("maps délègue chips to tasks in createdAt order regardless of input order", () => {
    const a = task("a", "running", "2026-01-01T00:00:00Z");
    const b = task("b", "running", "2026-01-01T00:00:01Z");
    // tasks passed unsorted; the k-th délègue chip takes the k-th createdAt task.
    expect(buildBobFlow([reasoning("x"), delegate("a"), delegate("b")], [b, a])).toEqual([
      { kind: "reflection", text: "x", source: "reasoning" },
      { kind: "tasks", tasks: [a, b] },
    ]);
  });

  it("coalesces consecutive delegations into one group; a reasoning splits them", () => {
    const a = task("a", "running", "2026-01-01T00:00:00Z");
    const b = task("b", "running", "2026-01-01T00:00:01Z");
    const c = task("c", "running", "2026-01-01T00:00:02Z");
    expect(
      buildBobFlow(
        [delegate("a"), delegate("b"), reasoning("entre-deux"), delegate("c")],
        [a, b, c],
      ),
    ).toEqual([
      { kind: "tasks", tasks: [a, b] },
      { kind: "reflection", text: "entre-deux", source: "reasoning" },
      { kind: "tasks", tasks: [c] },
    ]);
  });

  it("does not consume a task on a transmet/annule chip (only délègue spawns)", () => {
    const a = task("a", "running", "2026-01-01T00:00:00Z");
    const b = task("b", "running", "2026-01-01T00:00:01Z");
    // Realistic order: A is délégué'd, later transmet'd, then B is délégué'd.
    expect(
      buildBobFlow(
        [reasoning("x"), delegate("a"), orchestration("transmet : a"), delegate("b")],
        [a, b],
      ),
    ).toEqual([
      { kind: "reflection", text: "x", source: "reasoning" },
      { kind: "tasks", tasks: [a, b] },
    ]);
  });

  it("trails tasks with no délègue chip as a final group (rehydrate / race)", () => {
    const a = task("a", "running", "2026-01-01T00:00:00Z");
    const b = task("b", "running", "2026-01-01T00:00:01Z");
    expect(buildBobFlow([reasoning("x"), delegate("a")], [a, b])).toEqual([
      { kind: "reflection", text: "x", source: "reasoning" },
      { kind: "tasks", tasks: [a] },
      { kind: "tasks", tasks: [b] },
    ]);
  });

  it("lists rehydrated tasks (empty timeline) with no réflexion", () => {
    const a = task("a", "done", "2026-01-01T00:00:00Z");
    expect(buildBobFlow([], [a])).toEqual([{ kind: "tasks", tasks: [a] }]);
  });

  it("prepends the narrated réflexion line on a degraded (no-reasoning) backend", () => {
    const a = task("a", "done", "2026-01-01T00:00:00Z");
    expect(buildBobFlow([delegate("a")], [a])).toEqual([
      { kind: "reflection", text: "délègue : a — terminé.", source: "narrated" },
      { kind: "tasks", tasks: [a] },
    ]);
  });

  it("treats whitespace-only reasoning as absent (narrates instead)", () => {
    const a = task("a", "done", "2026-01-01T00:00:00Z");
    const flow = buildBobFlow([reasoning("  \n"), delegate("a")], [a]);
    expect(flow[0]).toEqual({
      kind: "reflection",
      text: "délègue : a — terminé.",
      source: "narrated",
    });
    expect(flow[1]).toEqual({ kind: "tasks", tasks: [a] });
  });

  it("returns nothing for an empty timeline + no tasks", () => {
    expect(buildBobFlow([], [])).toEqual([]);
    expect(buildBobFlow(undefined, [])).toEqual([]);
  });
});

describe("buildSubFlow", () => {
  it("renders réflexion runs and tool calls in arrival order", () => {
    expect(
      buildSubFlow([reasoning("a"), tool("t1", "ok"), reasoning("b"), tool("t2", "running")]),
    ).toEqual([
      { kind: "reflection", text: "a", source: "reasoning" },
      {
        kind: "tool",
        chip: { kind: "chip", activityKind: "tool_call", label: "t1", status: "ok" },
      },
      { kind: "reflection", text: "b", source: "reasoning" },
      {
        kind: "tool",
        chip: { kind: "chip", activityKind: "tool_call", label: "t2", status: "running" },
      },
    ]);
  });

  it("renders a tool_retrieval chip as its own flow node (issue 0092)", () => {
    const retrieval: AgentTimelineItem = {
      kind: "chip",
      activityKind: "tool_retrieval",
      label: "Sélection d'outils (1)",
      status: "info",
      args: "web_search  —  scores: web_search (9) · gmail_search (0)",
    };
    expect(
      buildSubFlow([retrieval, reasoning("Je cherche l'actu."), tool("web_search", "ok")]),
    ).toEqual([
      { kind: "tool", chip: retrieval },
      { kind: "reflection", text: "Je cherche l'actu.", source: "reasoning" },
      {
        kind: "tool",
        chip: { kind: "chip", activityKind: "tool_call", label: "web_search", status: "ok" },
      },
    ]);
  });

  it("coalesces a running + settled chip of the same tool into one block", () => {
    expect(
      buildSubFlow([
        reasoning("Je cherche."),
        tool("gmail.search", "running", { args: "q=daniela" }),
        tool("gmail.search", "ok", { args: "q=daniela", result: "12 messages" }),
      ]),
    ).toEqual([
      { kind: "reflection", text: "Je cherche.", source: "reasoning" },
      {
        kind: "tool",
        chip: {
          kind: "chip",
          activityKind: "tool_call",
          label: "gmail.search",
          status: "ok",
          args: "q=daniela",
          result: "12 messages",
        },
      },
    ]);
  });

  it("pairs settling chips to their own running node by label (parallel calls)", () => {
    const flow = buildSubFlow([
      tool("t1", "running"),
      tool("t2", "running"),
      tool("t1", "ok", { result: "r1" }),
      tool("t2", "ok", { result: "r2" }),
    ]);
    expect(flow).toHaveLength(2);
    expect(flow[0]).toMatchObject({
      kind: "tool",
      chip: { label: "t1", status: "ok", result: "r1" },
    });
    expect(flow[1]).toMatchObject({
      kind: "tool",
      chip: { label: "t2", status: "ok", result: "r2" },
    });
  });

  it("keeps a settled chip with no prior running as its own node", () => {
    expect(buildSubFlow([tool("t1", "ok", { result: "done" })])).toEqual([
      {
        kind: "tool",
        chip: {
          kind: "chip",
          activityKind: "tool_call",
          label: "t1",
          status: "ok",
          result: "done",
        },
      },
    ]);
  });

  it("narrates from lifecycle chips when nothing concrete streamed", () => {
    expect(buildSubFlow([lifecycle()])).toEqual([
      { kind: "reflection", text: "Je traite la demande.", source: "narrated" },
    ]);
  });

  it("returns nothing for an empty / undefined timeline", () => {
    expect(buildSubFlow([])).toEqual([]);
    expect(buildSubFlow(undefined)).toEqual([]);
  });
});
