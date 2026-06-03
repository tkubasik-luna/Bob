import { describe, expect, it } from "vitest";
import type { AgentTimelineItem } from "../store/activityFeedStore";
import type { AgentActivityStatus } from "../types/ws";
import { reflectionNarrator } from "./reflectionNarrator";

const reasoning = (text: string): AgentTimelineItem => ({ kind: "reasoning", text });
const toolChip = (label: string, status: AgentActivityStatus): AgentTimelineItem => ({
  kind: "chip",
  activityKind: "tool_call",
  label,
  status,
});
const lifecycleChip = (
  activityKind: "started" | "finished" | "stall",
  status: AgentActivityStatus,
): AgentTimelineItem => ({ kind: "chip", activityKind, label: activityKind, status });

describe("reflectionNarrator", () => {
  // ── reasoning present → primes verbatim ───────────────────────────────────
  it("primes the real reasoning text when present", () => {
    expect(reflectionNarrator([reasoning("Je trie les messages de Daniela.")])).toEqual({
      text: "Je trie les messages de Daniela.",
      kind: "reasoning",
    });
  });

  it("concatenates reasoning runs split by a chip", () => {
    expect(
      reflectionNarrator([
        reasoning("Deux demandes : "),
        toolChip("gmail.search", "ok"),
        reasoning("je tiens le fil."),
      ]),
    ).toEqual({ text: "Deux demandes : je tiens le fil.", kind: "reasoning" });
  });

  it("reasoning wins over chips (prime over narrate) even when both present", () => {
    const r = reflectionNarrator([reasoning("monologue"), toolChip("gmail.search", "running")]);
    expect(r.kind).toBe("reasoning");
    expect(r.text).toBe("monologue");
  });

  // ── reasoning absent → narration derived from chips ───────────────────────
  it("narrates a running tool call as a delegation line", () => {
    expect(reflectionNarrator([toolChip("gmail.search", "running")])).toEqual({
      text: "Je délègue : gmail.search…",
      kind: "narrated",
    });
  });

  it("narrates the last settled tool call when nothing is in flight", () => {
    expect(
      reflectionNarrator([toolChip("gmail.search", "ok"), toolChip("calendar.read", "ok")]),
    ).toEqual({ text: "calendar.read — terminé.", kind: "narrated" });
  });

  it("prefers an in-flight tool call over an already-settled one", () => {
    const r = reflectionNarrator([
      toolChip("gmail.search", "ok"),
      toolChip("calendar.read", "running"),
    ]);
    expect(r).toEqual({ text: "Je délègue : calendar.read…", kind: "narrated" });
  });

  it("narrates a failed tool call as an échec line", () => {
    expect(reflectionNarrator([toolChip("drive.read", "error")])).toEqual({
      text: "drive.read — échec.",
      kind: "narrated",
    });
  });

  it("falls back to a generic working line for lifecycle-only chips", () => {
    expect(reflectionNarrator([lifecycleChip("started", "info")])).toEqual({
      text: "Je traite la demande.",
      kind: "narrated",
    });
  });

  // ── partial / empty events ────────────────────────────────────────────────
  it("returns empty for an undefined timeline (nothing streamed yet)", () => {
    expect(reflectionNarrator(undefined)).toEqual({ text: "", kind: "empty" });
  });

  it("returns empty for an empty timeline", () => {
    expect(reflectionNarrator([])).toEqual({ text: "", kind: "empty" });
  });

  it("treats whitespace-only reasoning as absent and narrates instead", () => {
    // A stray newline arrived on the reasoning channel before any real token,
    // followed by a tool-call chip: the narrated fallback must still kick in.
    expect(reflectionNarrator([reasoning("\n  "), toolChip("gmail.search", "running")])).toEqual({
      text: "Je délègue : gmail.search…",
      kind: "narrated",
    });
  });

  it("whitespace-only reasoning with no chips stays empty", () => {
    expect(reflectionNarrator([reasoning("   ")])).toEqual({ text: "", kind: "empty" });
  });

  it("preserves leading/trailing whitespace WITHIN primed reasoning (caller trims for display)", () => {
    // Only the absence check trims; a non-blank reasoning run is passed through
    // verbatim so streaming spacing is preserved while tokens land.
    expect(reflectionNarrator([reasoning("  thinking…  ")])).toEqual({
      text: "  thinking…  ",
      kind: "reasoning",
    });
  });
});
