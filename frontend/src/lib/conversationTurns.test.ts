import { describe, expect, it } from "vitest";
import type { AgentTimelineItem, JarvisSegment } from "../store/activityFeedStore";
import type { ChatMessage, Task, TaskState } from "../types/ws";
import { buildConversation } from "./conversationTurns";

const reasoning = (text: string): AgentTimelineItem => ({ kind: "reasoning", text });
const delegate = (title: string): AgentTimelineItem => ({
  kind: "chip",
  activityKind: "tool_call",
  label: `délègue : ${title}`,
  status: "ok",
});
const user = (content: string, id: string): ChatMessage => ({ id, role: "user", content });
const assistant = (content: string, id: string, proactive = false): ChatMessage => ({
  id,
  role: "assistant",
  content,
  ...(proactive ? { proactive: true } : {}),
});
const seg = (msgId: string, start: number): JarvisSegment => ({ msgId, start });
const task = (id: string, state: TaskState = "running", createdAt = id): Task => ({
  id,
  title: id,
  goal: `goal-${id}`,
  state,
  createdAt,
});

const base = {
  messages: [] as ChatMessage[],
  timeline: [] as AgentTimelineItem[],
  tasks: [] as Task[],
  segments: [] as JarvisSegment[],
  pending: null as number | null,
  streamingSpeech: "",
};

describe("buildConversation", () => {
  it("builds one block for a completed turn (prompt + flow + answer)", () => {
    const turns = buildConversation({
      ...base,
      messages: [user("salut", "m0"), assistant("voici", "A1")],
      timeline: [reasoning("je pense"), delegate("t1")],
      tasks: [task("t1")],
      segments: [seg("A1", 0)],
    });
    expect(turns).toEqual([
      {
        key: "A1",
        prompt: "salut",
        proactive: false,
        flow: [
          { kind: "reflection", text: "je pense", source: "reasoning" },
          { kind: "tasks", tasks: [task("t1")] },
        ],
        answerText: "voici",
        inFlight: false,
      },
    ]);
  });

  it("splits two turns by msg_id, scoping each turn's tasks", () => {
    const t1 = task("t1", "running", "2026-01-01T00:00:00Z");
    const t2 = task("t2", "running", "2026-01-01T00:00:01Z");
    const turns = buildConversation({
      ...base,
      messages: [
        user("un", "m0"),
        assistant("r1", "A1"),
        user("deux", "m1"),
        assistant("r2", "A2"),
      ],
      timeline: [reasoning("p1"), delegate("t1"), reasoning("p2"), delegate("t2")],
      tasks: [t2, t1],
      segments: [seg("A1", 0), seg("A2", 2)],
    });
    expect(turns.map((t) => t.key)).toEqual(["A1", "A2"]);
    expect(turns[0].flow).toEqual([
      { kind: "reflection", text: "p1", source: "reasoning" },
      { kind: "tasks", tasks: [t1] },
    ]);
    expect(turns[1].flow).toEqual([
      { kind: "reflection", text: "p2", source: "reasoning" },
      { kind: "tasks", tasks: [t2] },
    ]);
  });

  it("renders the in-flight turn from the pending start with the streaming reply", () => {
    const turns = buildConversation({
      ...base,
      messages: [user("salut", "m0"), assistant("r1", "A1"), user("encore", "m1")],
      timeline: [reasoning("p1"), reasoning("p2")],
      segments: [seg("A1", 0)],
      pending: 1,
      streamingSpeech: "je réponds…",
    });
    expect(turns).toHaveLength(2);
    // The completed turn's slice ends at the pending start.
    expect(turns[0]).toMatchObject({ key: "A1", inFlight: false, answerText: "r1" });
    expect(turns[0].flow).toEqual([{ kind: "reflection", text: "p1", source: "reasoning" }]);
    // The live turn carries the streaming answer + the trailing reasoning.
    expect(turns[1]).toMatchObject({
      key: "live",
      prompt: "encore",
      inFlight: true,
      answerText: "je réponds…",
    });
    expect(turns[1].flow).toEqual([{ kind: "reflection", text: "p2", source: "reasoning" }]);
  });

  it("renders a proactive push as a prompt-less block with an empty flow", () => {
    const turns = buildConversation({
      ...base,
      messages: [assistant("synthèse spontanée", "P1", true)],
    });
    expect(turns).toEqual([
      {
        key: "P1",
        prompt: "",
        proactive: true,
        flow: [],
        answerText: "synthèse spontanée",
        inFlight: false,
      },
    ]);
  });

  it("renders rehydrated tasks alone when there is no conversation (reconnect)", () => {
    const t1 = task("t1", "done");
    const turns = buildConversation({ ...base, tasks: [t1] });
    expect(turns).toEqual([
      {
        key: "orphan-tasks",
        prompt: "",
        proactive: false,
        flow: [{ kind: "tasks", tasks: [t1] }],
        answerText: "",
        inFlight: false,
      },
    ]);
  });

  it("gives a turn with no committed segment an empty flow (pre-feature / reload)", () => {
    const turns = buildConversation({
      ...base,
      messages: [user("salut", "m0"), assistant("r", "A1")],
      timeline: [reasoning("orphan reasoning")],
      segments: [],
    });
    expect(turns).toHaveLength(1);
    expect(turns[0]).toMatchObject({ key: "A1", answerText: "r", flow: [] });
  });

  it("trails a task with no délègue chip onto the last turn", () => {
    const t1 = task("t1", "done", "2026-01-01T00:00:00Z");
    const t2 = task("t2", "running", "2026-01-01T00:00:01Z");
    const turns = buildConversation({
      ...base,
      messages: [user("salut", "m0"), assistant("r", "A1")],
      timeline: [reasoning("pense"), delegate("t1")],
      tasks: [t1, t2],
      segments: [seg("A1", 0)],
    });
    expect(turns).toHaveLength(1);
    // t1 came from the slice's délègue chip; t2 (no chip) trails as its own group.
    const flow = turns[0].flow;
    expect(flow[0]).toEqual({ kind: "reflection", text: "pense", source: "reasoning" });
    expect(flow.at(-1)).toEqual({ kind: "tasks", tasks: [t2] });
    expect(flow).toContainEqual({ kind: "tasks", tasks: [t1] });
  });

  it("returns nothing for an empty session", () => {
    expect(buildConversation({ ...base })).toEqual([]);
  });
});
