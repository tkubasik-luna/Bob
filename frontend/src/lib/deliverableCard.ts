// deliverableCard.ts — PURE projection of a generated deliverable into a single
// data-dock card (PRD 0014 / issue 0087).
//
// A "deliverable" is the `ComponentDescriptor[]` the overlay consumes as a
// stack: it comes either from a sub-task's `task_result.result_payload` or from
// Bob's own streamed `ui_payload` (a single descriptor wrapped into a list-of-
// one by the ingest layer). This module turns ONE deliverable into ONE card:
//
//   toCard(deliverable, task) -> { title, sub, type, sections }
//
// It is intentionally side-effect-free and React-free so the projection can be
// unit-tested in isolation (prior art: `lib/overlayHeuristic.test.ts`). The
// `deliverableStore` calls it lazily for rendering; nothing here touches the
// store, the WS, or the DOM.

import type { ComponentDescriptor } from "../types/ws";

/** The data-dock icon vocabulary, mirroring the mockup's `DATA_TYPE_LABEL`
 * keys (`Design Mockup/p3d-content.jsx`). The HUD only generates Mail (Gmail
 * connector), Markdown (synthesis), and WebResults (web search) sections today,
 * so `mail` / `doc` / `web` are the live types; `composite` is the glyph for a
 * heterogeneous stack (≥2 distinct section types). The remaining mockup types
 * (`video` / `contact` / `action`) are kept in the union so the icon set + CSS
 * can grow without a type change when the backend starts emitting them. */
export type DeliverableCardType =
  | "mail"
  | "doc"
  | "web"
  | "video"
  | "contact"
  | "action"
  | "composite";

/** The minimal task shape the projection reads — a structural subset of
 * `types/ws.ts::Task` so callers can pass a `chatStore` task straight through,
 * AND so a Bob `ui_payload` (which has no real Task) can synthesise a light
 * stand-in. `title` drives the card title; `goal` is the preferred `sub`. */
export type DeliverableCardTask = {
  title: string;
  goal?: string;
};

/** The projected card. `sections` is the deliverable passed straight through —
 * the overlay (opened on click) renders it as a stack, exactly as today. */
export type DeliverableCard = {
  /** Card heading — the Task title. */
  title: string;
  /** Secondary line — the task goal / a short content summary. */
  sub: string;
  /** Dominant section type → drives the icon (composite glyph when mixed). */
  type: DeliverableCardType;
  /** The descriptors the overlay consumes as a stack (unchanged). */
  sections: ComponentDescriptor[];
};

/** Map a single `ComponentDescriptor.component` name to a dock type. The
 * backend contract today is `Mail` / `Markdown`; an unknown / forward-compat
 * component (the catch-all branch of `ComponentDescriptor`) falls back to
 * `doc` — a neutral "generated artefact" glyph rather than a crash. */
function sectionType(descriptor: ComponentDescriptor): DeliverableCardType {
  switch (descriptor.component) {
    case "Mail":
      return "mail";
    case "Markdown":
      return "doc";
    case "WebResults":
      return "web";
    default:
      // Forward-compat: a component the frontend doesn't model yet still
      // produces a card (rendered as a generic document artefact).
      return "doc";
  }
}

/** Resolve the DOMINANT type across a deliverable's sections.
 *
 * - empty list           → `doc` (defensive; the dock still shows a card).
 * - all sections same    → that type.
 * - heterogeneous (≥2)   → `composite` (the mixed-stack glyph), per the issue:
 *   "icône = type dominant (glyph composite si sections hétérogènes)".
 *
 * "Dominant" only matters when types differ AND we choose NOT to collapse to
 * composite — but the issue is explicit that ANY heterogeneity yields the
 * composite glyph, so a mixed stack is always `composite` regardless of which
 * type is most frequent. */
function dominantType(sections: ComponentDescriptor[]): DeliverableCardType {
  if (sections.length === 0) return "doc";
  const first = sectionType(sections[0]);
  const heterogeneous = sections.some((s) => sectionType(s) !== first);
  return heterogeneous ? "composite" : first;
}

/** Best-effort one-line content summary used as a FALLBACK `sub` when the task
 * carries no `goal`. Reads only fields already present on the known props:
 *   - Mail   → the subject (most informative single line).
 *   - Markdown → the first non-empty line of the content, trimmed.
 * A multi-section stack summarises the FIRST section (the overlay shows the
 * rest). Returns `""` when nothing usable is found — the caller then falls back
 * to a generic count label. */
function contentSummary(sections: ComponentDescriptor[]): string {
  const first = sections[0];
  if (!first) return "";
  if (first.component === "Mail") {
    const subject = first.props.subject;
    return typeof subject === "string" ? subject.trim() : "";
  }
  if (first.component === "Markdown") {
    const content = first.props.content;
    if (typeof content !== "string") return "";
    const line = content
      .split("\n")
      .map((l) => l.trim())
      .find((l) => l.length > 0);
    return line ?? "";
  }
  if (first.component === "WebResults") {
    // Prefer the direct answer; else the top result's title.
    const answer = first.props.answer?.trim();
    if (answer) return answer;
    const top = first.props.results[0];
    return top ? top.title.trim() : "";
  }
  return "";
}

/** Compose the `sub` line. Priority:
 *   1. the task `goal` (the orchestrator's own short description), if any;
 *   2. a content summary derived from the first section;
 *   3. a generic "N éléments" / "1 élément" count label as a last resort.
 * Always returns a non-empty string so the card never renders a blank sub. */
function buildSub(task: DeliverableCardTask, sections: ComponentDescriptor[]): string {
  const goal = task.goal?.trim();
  if (goal) return goal;
  const summary = contentSummary(sections);
  if (summary) return summary;
  const n = sections.length;
  return n === 1 ? "1 élément" : `${n} éléments`;
}

/**
 * Project ONE deliverable + its originating task into ONE dock card.
 *
 * - `title` = the task title (verbatim).
 * - `sub`   = goal → content summary → count (see `buildSub`).
 * - `type`  = dominant section type, or `composite` if the stack is mixed.
 * - `sections` = the descriptors passed straight through (the overlay stack).
 *
 * Pure: returns a fresh object, mutates nothing, no I/O.
 */
export function toCard(
  deliverable: ComponentDescriptor[],
  task: DeliverableCardTask,
): DeliverableCard {
  const sections = deliverable;
  return {
    title: task.title,
    sub: buildSub(task, sections),
    type: dominantType(sections),
    sections,
  };
}
