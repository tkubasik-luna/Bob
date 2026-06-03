import type { AgentTimelineItem } from "../store/activityFeedStore";

/**
 * PRD 0014 / issue 0085 — narrated-fallback for the BOB card's « Réflexion »
 * section.
 *
 * The card binds to the MAIN Bob/Jarvis thread, whose lane key is the fixed
 * `agent_ref="jarvis"` (see `orchestrator.JARVIS_AGENT_REF`). Its
 * `activityFeedStore.timelineByAgent["jarvis"]` is the SAME interleaved
 * reasoning + chip stream a sub-agent lane carries: a reasoning-capable backend
 * streams `reasoning_delta` (→ `reasoning` items) and every backend emits
 * `agent_activity` chips (→ `chip` items) for its orchestration steps
 * (delegations, synthesis, …).
 *
 * Bob does NOT use the native LM-Studio transport (it would break tool-calling),
 * so a non-reasoning model / the Claude CLI bridge never streams a `reasoning`
 * channel — the « Réflexion » section would otherwise be empty. This module is
 * the deterministic fallback: when reasoning text IS present it PRIMES (passes
 * the concatenated reasoning through verbatim); when absent it DERIVES a single
 * reflection line from the chip stream, in the same spirit as
 * `lib/agentActivityPanel.ts` (a pure function of the timeline, no React).
 *
 * Pure + exported so the (small, finite) behaviour is unit-testable without
 * React, the WS, or the store wiring.
 */

/** The shape the BOB card consumes for its « Réflexion » section. */
export type Reflection = {
  /** The reflection line to render (may be empty — see `kind`). */
  text: string;
  /** Where the line came from:
   *  - `reasoning` — the real streamed chain-of-thought (primed verbatim);
   *  - `narrated`  — derived from the chip stream (no reasoning channel);
   *  - `empty`     — nothing observed yet (caller renders the impatience hint). */
  kind: "reasoning" | "narrated" | "empty";
};

/** A chip item, narrowed off the interleaved timeline. */
type ChipItem = Extract<AgentTimelineItem, { kind: "chip" }>;

/** Concatenate every reasoning run in the timeline into one monologue. A burst
 * of `reasoning_delta`s already coalesces into a single trailing item in the
 * store, but multiple runs (reasoning → chip → reasoning) leave several — join
 * them so the section shows the whole thought, matching the mockup's single
 * `think-body`. */
function reasoningText(timeline: readonly AgentTimelineItem[]): string {
  let out = "";
  for (const it of timeline) {
    if (it.kind === "reasoning") out += it.text;
  }
  return out;
}

/**
 * Derive a single narrated reflection line from the chip stream — the honest
 * fallback when no reasoning channel exists. The line describes what Bob is
 * doing from its most salient chip, so the section reads as a terse monologue
 * rather than staying blank:
 *
 *   - a tool call in flight     → "Je délègue : {label}…"
 *   - a settled tool call       → "{label} — terminé." (last settled action)
 *   - only a lifecycle bookend  → "Je traite la demande."
 *
 * Chips are already redacted server-side (no mail content in `label`), so the
 * derived line is safe to show verbatim. A `tool_call` still in flight wins over
 * a settled one (it is the live action); among settled chips the LAST one is
 * narrated. With no actionable chip at all we fall back to a generic working
 * line so the section is never empty while Bob is busy.
 */
function narrateFromChips(chips: readonly ChipItem[]): string {
  // A tool call still running is the live action — narrate it first.
  const running = chips.find((c) => c.activityKind === "tool_call" && c.status === "running");
  if (running) return `Je délègue : ${running.label}…`;

  // Otherwise narrate the most recent SETTLED tool call (the latest concrete
  // step Bob took), scanning from the end.
  for (let i = chips.length - 1; i >= 0; i--) {
    const c = chips[i];
    if (c.activityKind === "tool_call") {
      return c.status === "error" ? `${c.label} — échec.` : `${c.label} — terminé.`;
    }
  }

  // Only lifecycle / incident bookends (started / finished / stall / …) — no
  // concrete action to name yet, but Bob IS working: a generic line beats blank.
  return "Je traite la demande.";
}

/**
 * Reduce the Bob lane's interleaved timeline to the « Réflexion » line.
 *
 * Rules:
 *   - REASONING PRIMES. If any `reasoning` text streamed, return it verbatim
 *     (concatenated across runs) — the real chain-of-thought always wins over a
 *     narration, even when chips are also present.
 *   - NARRATED FALLBACK. With no reasoning text but ≥1 chip, derive a line from
 *     the chip stream (see `narrateFromChips`).
 *   - EMPTY. Nothing observed (no reasoning, no chip) → empty line; the caller
 *     decides whether to show an impatience hint or nothing at all.
 *
 * Whitespace-only reasoning is treated as absent so a stray newline never
 * suppresses the narrated fallback. Never mutates the input.
 */
export function reflectionNarrator(timeline: readonly AgentTimelineItem[] | undefined): Reflection {
  const items = timeline ?? [];
  const reasoning = reasoningText(items);
  if (reasoning.trim()) {
    return { text: reasoning, kind: "reasoning" };
  }
  const chips = items.filter((it): it is ChipItem => it.kind === "chip");
  if (chips.length > 0) {
    return { text: narrateFromChips(chips), kind: "narrated" };
  }
  return { text: "", kind: "empty" };
}
