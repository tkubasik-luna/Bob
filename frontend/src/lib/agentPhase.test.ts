import { describe, expect, it } from "vitest";
import type { AgentTimelineItem } from "../store/activityFeedStore";
import { deriveAgentPhase } from "./agentPhase";

const reasoning = (text: string): AgentTimelineItem => ({ kind: "reasoning", text });
const chip = (status: "running" | "ok"): AgentTimelineItem => ({
  kind: "chip",
  activityKind: "tool_call",
  label: "gmail.search",
  status,
});

describe("deriveAgentPhase", () => {
  it("terminal failed → error (regardless of timeline)", () => {
    expect(deriveAgentPhase([reasoning("x")], "failed")).toEqual({ key: "error", status: "error" });
  });

  it("terminal done → done", () => {
    expect(deriveAgentPhase([reasoning("x")], "done")).toEqual({ key: "done", status: "done" });
  });

  it("empty + running → waiting", () => {
    expect(deriveAgentPhase(undefined, undefined)).toEqual({ key: "waiting", status: "running" });
    expect(deriveAgentPhase([], "running")).toEqual({ key: "waiting", status: "running" });
  });

  it("reasoning streamed, nothing in flight → thinking", () => {
    expect(deriveAgentPhase([reasoning("hmm")], "running")).toEqual({
      key: "thinking",
      status: "running",
    });
  });

  it("trailing running chip → tool (even after reasoning)", () => {
    expect(deriveAgentPhase([reasoning("hmm"), chip("running")], "running")).toEqual({
      key: "tool",
      status: "running",
    });
  });

  it("settled chip after reasoning → back to thinking (tool no longer in flight)", () => {
    expect(deriveAgentPhase([reasoning("hmm"), chip("ok")], undefined)).toEqual({
      key: "thinking",
      status: "running",
    });
  });

  it("turn-bracketed agent (Jarvis): turnActive=false settles to done", () => {
    expect(
      deriveAgentPhase([reasoning("done thinking")], undefined, { turnActive: false }),
    ).toEqual({ key: "done", status: "done" });
  });

  it("turn-bracketed agent: turnActive=true keeps thinking", () => {
    expect(deriveAgentPhase([reasoning("…")], undefined, { turnActive: true })).toEqual({
      key: "thinking",
      status: "running",
    });
  });

  it("running chip wins over turnActive=false (genuinely in-flight tool)", () => {
    expect(deriveAgentPhase([chip("running")], undefined, { turnActive: false })).toEqual({
      key: "tool",
      status: "running",
    });
  });
});
