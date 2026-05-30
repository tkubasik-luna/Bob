import { type MouseEvent, useEffect, useMemo } from "react";
import type { ComponentDescriptor } from "../../types/ws";
import { NotImplementedSection } from "./NotImplementedSection";
import { sectionRegistry } from "./sectionRegistry";

type SectionsOverlayProps = {
  /** Ordered list of section descriptors to render. When `null`, the overlay
   * renders nothing — mounting an empty shell is reserved for the open state
   * alone (mirrors the former `MarkdownOverlay` / `MailOverlay` contract). An
   * empty array is treated like `null`. */
  sections: ComponentDescriptor[] | null;
  /** Called on Esc, X button, backdrop click, or footer `DISMISS`. The parent
   * owns the open/closed state; this component only signals intent. */
  onClose: () => void;
};

/**
 * The single overlay shell for a list of sections (PRD 0010 / issue 0066).
 * Replaces the standalone `MarkdownOverlay`: one corner-bracket frame, header,
 * scrollable stack of sections, and a global DISMISS / Esc / backdrop close
 * path shared by every section in the list. Each descriptor is mapped through
 * `sectionRegistry`; an unknown `component` falls back to a contained
 * `NotImplementedSection` (never the raw props, never a crash).
 *
 * Dismiss is multi-pathed: `Esc` (global keydown listener), the header `×`
 * button, the footer `DISMISS` action, and a click on the `.overlay-stage`
 * backdrop (clicks inside `.overlay-card` are swallowed so the card stays
 * mounted). The body scrolls past the viewport height via the `.ov-body` CSS
 * (`overflow-y` already styled in `hud.css`), so a long list / long Markdown
 * stays contained inside the card.
 *
 * PRD: prd/0010-adaptive-composite-ui.md — Issue: issues/0066-sections-list-pipeline-markdown.md
 */
export function SectionsOverlay({ sections, onClose }: SectionsOverlayProps) {
  const open = sections !== null && sections.length > 0;

  // Stable REF marker for the header. Derived from the section components +
  // count so it stays deterministic per payload (no flaky tests, no random
  // re-roll on every render). Falls back to a sentinel when closed.
  const ref = useMemo(
    () => (open && sections ? sectionsRefMarker(sections) : "0000"),
    [open, sections],
  );

  useEffect(() => {
    if (!open) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") onClose();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [open, onClose]);

  if (!open || sections === null) return null;

  const onBackdropClick = (e: MouseEvent<HTMLDivElement>) => {
    // Only fire when the user clicks the stage itself, not when a click inside
    // the card bubbles up.
    if (e.target === e.currentTarget) onClose();
  };

  const onCardClick = (e: MouseEvent<HTMLDivElement>) => {
    e.stopPropagation();
  };

  return (
    // biome-ignore lint/a11y/useKeyWithClickEvents: keyboard dismiss is wired globally via the Escape listener installed in `useEffect` above — the backdrop click is a redundant mouse affordance, not the primary dismiss path.
    <div className="overlay-stage" onClick={onBackdropClick}>
      <div className="overlay-beam" />
      {/* biome-ignore lint/a11y/useKeyWithClickEvents: clicks here only stop propagation so backdrop dismiss doesn't fire when the user clicks the card body — no keyboard equivalent is needed (focused buttons handle their own keys). */}
      <div
        className="overlay-card surface-notes"
        // biome-ignore lint/a11y/useSemanticElements: native <dialog> brings its own positioning + backdrop semantics that collide with the mockup chrome (`.overlay-stage` is our backdrop, the parent owns open/closed).
        role="dialog"
        aria-label="SECTIONS"
        onClick={onCardClick}
      >
        <span className="ov-corner tl" />
        <span className="ov-corner tr" />
        <span className="ov-corner bl" />
        <span className="ov-corner br" />

        <header className="ov-header">
          <div className="ov-header-left">
            <span className="ov-source-tag">BOB · SURFACING</span>
            <span className="ov-divider">/</span>
            <span className="ov-type-chip">SECTIONS</span>
          </div>
          <div className="ov-header-right">
            <span className="ov-id-tag">REF · SEC-{ref}</span>
            <button type="button" className="ov-close" onClick={onClose} aria-label="dismiss">
              <span className="ov-close-glyph">✕</span>
            </button>
          </div>
        </header>

        {/* Re-key on the payload marker so the CSS fade-in (`ov-card-in`) on
         * the body wrapper restarts in place when the list changes. Header /
         * footer stay mounted so the surrounding chrome never blinks. */}
        <div className="ov-body ov-sections" key={`body-${ref}`}>
          {sections.map((descriptor, index) => {
            const entry = sectionRegistry[descriptor.component];
            const key = `${descriptor.component}-${index}`;
            return (
              <div className="ov-section" key={key}>
                {entry ? (
                  <entry.Component props={descriptor.props as Record<string, unknown>} />
                ) : (
                  <NotImplementedSection name={descriptor.component} />
                )}
              </div>
            );
          })}
        </div>

        <footer className="ov-footer">
          <button type="button" className="ov-action ov-action-primary" aria-label="read aloud">
            <span className="ov-action-key">↵</span>
            <span>READ ALOUD</span>
          </button>
          <button type="button" className="ov-action" aria-label="open">
            <span className="ov-action-key">↗</span>
            <span>OPEN</span>
          </button>
          <button type="button" className="ov-action" aria-label="dismiss" onClick={onClose}>
            <span className="ov-action-key">ESC</span>
            <span>DISMISS</span>
          </button>
        </footer>
      </div>
    </div>
  );
}

/** Derive a 4-char hex from the section components + count using a
 * deterministic FNV-1a hash. The marker is purely cosmetic (header chip) —
 * stability matters for snapshot-style tests; collision likelihood doesn't. */
function sectionsRefMarker(sections: ComponentDescriptor[]): string {
  const sample = `${sections.length}:${sections.map((s) => s.component).join(",")}`.slice(0, 64);
  let hash = 0x811c9dc5;
  for (let i = 0; i < sample.length; i++) {
    hash ^= sample.charCodeAt(i);
    hash = Math.imul(hash, 0x01000193);
  }
  const hex = (hash >>> 0).toString(16).slice(-4).toUpperCase();
  return hex.padStart(4, "0");
}
