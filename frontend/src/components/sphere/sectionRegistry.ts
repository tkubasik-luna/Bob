import type { ComponentType } from "react";
import { MailCard } from "./MailCard";
import { MarkdownSection } from "./MarkdownSection";

/** A section renderer + its auto-open weight.
 *
 * - `Component` receives the descriptor's `props` bag (validated server-side,
 *   so unknown keys are tolerated here).
 * - `structured` flags a non-text, layout-bearing section (a Mail card, a map,
 *   …). It drives the `SphereUI` auto-open heuristic: a list with ≥1 structured
 *   section opens unconditionally, while a text-only list (Markdown) defers to
 *   the `shouldOverlayResponse` text heuristic. The `Markdown` entry is
 *   `structured: false` (text-only); `Mail` is `structured: true` so a list
 *   containing a mail auto-opens the overlay regardless of the text heuristic
 *   (issue 0067). */
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
  Markdown: { Component: MarkdownSection, structured: false },
  Mail: { Component: MailCard, structured: true },
};
