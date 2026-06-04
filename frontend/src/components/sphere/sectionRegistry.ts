import type { ComponentType } from "react";
import { DocSurface } from "./DocSurface";
import { MailCard } from "./MailCard";
import { WebResultsCard } from "./WebResultsCard";

/** A section renderer + its auto-open weight.
 *
 * - `Component` receives the descriptor's `props` bag (validated server-side,
 *   so unknown keys are tolerated here).
 * - `structured` flags a non-text, layout-bearing section (a Mail card, a map,
 *   …). Historically it drove the `SphereUI` auto-open heuristic; the overlay
 *   is now CLICK-ONLY (PRD 0014 / issue 0088 — auto-open removed in the
 *   foundation), so the flag is retained as descriptive metadata for any future
 *   consumer rather than gating open. The `Markdown` entry is `structured:
 *   false` (text-only Document); `Mail` is `structured: true`. */
export type SectionEntry = {
  Component: ComponentType<{ props: Record<string, unknown> }>;
  structured: boolean;
};

/**
 * Maps a `ComponentDescriptor.component` name to its renderer. Keys MUST match
 * the `component` field the backend emits (the same contract as the legacy
 * top-level `componentRegistry`). A name absent here renders as a
 * `NotImplementedSection` in `SectionsOverlay` rather than crashing — so the
 * registry can grow on the backend ahead of the frontend.
 *
 * PRD: prd/0010-adaptive-composite-ui.md — Issue: issues/0067-multi-mail-sections.md
 */
export const sectionRegistry: Record<string, SectionEntry> = {
  // `Markdown` renders through the Document surface (mockup `DocSurface`), which
  // reuses the existing react-markdown renderer for the body (PRD 0014 / 0088).
  Markdown: { Component: DocSurface, structured: false },
  Mail: { Component: MailCard, structured: true },
  // Web search results (PRD: web-search) — a ranked list of clickable sources.
  WebResults: { Component: WebResultsCard, structured: true },
};
