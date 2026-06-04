import { type MouseEvent, useCallback, useEffect, useMemo } from "react";
import type { ComponentDescriptor } from "../../types/ws";
import "./SectionsOverlay.css";
import { NotImplementedSection } from "./NotImplementedSection";
import { overlayChip, overlayRefMarker, overlaySpeechText } from "./overlayArtifact";
import { sectionRegistry } from "./sectionRegistry";

type SectionsOverlayProps = {
  /** Ordered list of section descriptors to render. When `null`, the overlay
   * renders nothing — mounting an empty shell is reserved for the open state
   * alone. An empty array is treated like `null`. */
  sections: ComponentDescriptor[] | null;
  /** Called on Esc, the header `✕`, a backdrop click, or the footer `FERMER`.
   * The parent owns the open/closed state; this component only signals intent. */
  onClose: () => void;
  /** Test seam — "LIRE À VOIX HAUTE" speaks the flattened artifact text.
   * Defaults to the Web Speech API path (`speakViaSynthesis`), which is a
   * no-op when the browser lacks `speechSynthesis` (e.g. jsdom). Tests pass a
   * `vi.fn()` to assert the button feeds the right text to TTS without faking a
   * synthesis runtime. */
  speak?: (text: string) => void;
  /** Test seam — "OUVRIR" browses to the first openable artifact's URL (a
   * Mail's `gmailWebUrl` today). Defaults to the Tauri-aware `openExternal`
   * below. A stack with nothing openable leaves the button inert. */
  openExternal?: (url: string) => void;
};

/**
 * The single fullscreen overlay shell for a stack of section descriptors,
 * re-skinned to the Piste 3D · Nacre mockup chrome (PRD 0014 / issue 0088 —
 * ported from `Design Mockup/p3d-overlay.jsx` + the `ov-*` rules in
 * `Design Mockup/p3d.css`). It is opened ONLY on a click of a dock card
 * (`SphereUI` hands its `openOverlay` to `DataSlot`); the legacy auto-open
 * paths were removed in the foundation, so there is no auto-open here.
 *
 * Chrome: a blurred `.ov-scrim` backdrop, a projection `.ov-beam`, then the
 * `.ov-card` with four corner brackets, a mono header (`BOB · GÉNÉRÉ` source
 * tag + a per-stack type chip + `RÉF · SEC-XXXX`), a scrollable body holding the
 * ordered STACK of surfaces (feature 0011 preserved — a composite deliverable
 * renders as a vertical column), and a footer of global actions.
 *
 * Each descriptor maps through `sectionRegistry` (Markdown → the Document
 * surface, Mail → the Mail surface); an unknown `component` falls back to a
 * contained `NotImplementedSection` rather than crashing.
 *
 * Footer actions: `LIRE À VOIX HAUTE` (wired to TTS — speaks the flattened
 * artifact text), `OUVRIR` (browses to the first openable artifact), and
 * `FERMER` (dismiss). Dismiss is multi-pathed: `Esc` (global keydown), the
 * header `✕`, the footer `FERMER`, and a click on the `.ov-stage` backdrop
 * (clicks inside `.ov-card` are swallowed so the card stays mounted).
 *
 * CSS reconciliation: the mockup `ov-*` rules live in the co-located
 * `SectionsOverlay.css`, scoped under the `.p3d-ov` wrapper so they supersede
 * the legacy same-named `.ov-*` rules in `styles/hud.css` (which is left
 * untouched). The shell uses the mockup's `.ov-stage` / `.ov-card` class names
 * — distinct from the legacy `.overlay-stage` / `.overlay-card` — so the old
 * shell rules simply stop matching.
 *
 * PRD: prd/0014-hud-piste-3d-nacre.md — Issue: issues/0088-overlay-reskin-typed-surfaces.md
 */
export function SectionsOverlay({
  sections,
  onClose,
  speak = speakViaSynthesis,
  openExternal = openExternal_,
}: SectionsOverlayProps) {
  const open = sections !== null && sections.length > 0;

  // Stable header chip + REF marker, derived from the section components so they
  // stay deterministic per payload. Fall back to sentinels when closed.
  const chip = useMemo(
    () => (open && sections ? overlayChip(sections) : "SURFACE"),
    [open, sections],
  );
  const ref = useMemo(
    () => (open && sections ? overlayRefMarker(sections) : "0000"),
    [open, sections],
  );

  const onReadAloud = useCallback(() => {
    if (!sections) return;
    const text = overlaySpeechText(sections);
    if (text.length > 0) speak(text);
  }, [sections, speak]);

  const onOpen = useCallback(() => {
    if (!sections) return;
    const url = firstOpenableUrl(sections);
    if (url !== null) openExternal(url);
  }, [sections, openExternal]);

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
    <div className="p3d-ov ov-stage" onClick={onBackdropClick}>
      <div className="ov-scrim" />
      <div className="ov-beam" />
      {/* biome-ignore lint/a11y/useKeyWithClickEvents: clicks here only stop propagation so backdrop dismiss doesn't fire when the user clicks the card body — no keyboard equivalent is needed (focused buttons handle their own keys). */}
      <div
        className="ov-card"
        // biome-ignore lint/a11y/useSemanticElements: native <dialog> brings its own positioning + backdrop semantics that collide with the mockup chrome (`.ov-stage` is our backdrop, the parent owns open/closed).
        role="dialog"
        aria-label="DONNÉE GÉNÉRÉE"
        onClick={onCardClick}
      >
        <span className="ov-corner tl" />
        <span className="ov-corner tr" />
        <span className="ov-corner bl" />
        <span className="ov-corner br" />

        <header className="ov-header">
          <div className="ov-header-left">
            <span className="ov-source-tag">BOB · GÉNÉRÉ</span>
            <span className="ov-divider">/</span>
            <span className="ov-type-chip">{chip}</span>
          </div>
          <div className="ov-header-right">
            <span className="ov-id-tag">RÉF · SEC-{ref}</span>
            <button type="button" className="ov-close" onClick={onClose} aria-label="fermer">
              <span className="ov-close-glyph">✕</span>
            </button>
          </div>
        </header>

        {/* Re-key on the payload marker so the CSS fade-in (`ov-card-in`) on the
         * body wrapper restarts in place when the list changes. Header / footer
         * stay mounted so the surrounding chrome never blinks. */}
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
          <button
            type="button"
            className="ov-action ov-action-primary"
            aria-label="read aloud"
            onClick={onReadAloud}
          >
            <span className="ov-action-key">↵</span>
            <span>LIRE À VOIX HAUTE</span>
          </button>
          <button type="button" className="ov-action" aria-label="open" onClick={onOpen}>
            <span className="ov-action-key">↗</span>
            <span>OUVRIR</span>
          </button>
          <button type="button" className="ov-action" aria-label="dismiss" onClick={onClose}>
            <span className="ov-action-key">ÉCHAP</span>
            <span>FERMER</span>
          </button>
        </footer>
      </div>
    </div>
  );
}

/** The URL the global `OUVRIR` action browses to: the first artifact that
 * carries one (a Mail's `gmailWebUrl` today). Returns `null` when the stack has
 * nothing openable (a pure Document, say), leaving the button inert. */
function firstOpenableUrl(sections: ComponentDescriptor[]): string | null {
  for (const section of sections) {
    if (section.component === "Mail") {
      const url = (section.props as Record<string, unknown>).gmailWebUrl;
      if (typeof url === "string" && url.length > 0) return url;
    }
    if (section.component === "WebResults") {
      const results = (section.props as Record<string, unknown>).results;
      if (Array.isArray(results)) {
        for (const result of results) {
          if (result && typeof result === "object") {
            const url = (result as Record<string, unknown>).url;
            if (typeof url === "string" && url.length > 0) return url;
          }
        }
      }
    }
  }
  return null;
}

/** Speak `text` through the browser Web Speech API. Self-contained (no deps, no
 * backend round-trip, no SphereUI wiring): cancels any in-flight utterance,
 * then queues a fresh one in French to match the HUD copy. A no-op when the
 * runtime lacks `speechSynthesis` (jsdom under test, older webviews) so callers
 * never need to feature-detect. */
function speakViaSynthesis(text: string): void {
  if (typeof window === "undefined") return;
  const synth = window.speechSynthesis;
  if (!synth || typeof SpeechSynthesisUtterance === "undefined") return;
  synth.cancel();
  const utterance = new SpeechSynthesisUtterance(text);
  utterance.lang = "fr-FR";
  synth.speak(utterance);
}

/** Open an external URL in the user's default browser. `window.open(url,
 * '_blank')` is the MVP path: the Tauri v2 webview forwards it to the OS browser
 * when the URL host isn't in the app's window list. Mirrors the per-card seam in
 * `MailCard`. */
function openExternal_(url: string): void {
  if (typeof window === "undefined") return;
  window.open(url, "_blank", "noopener,noreferrer");
}
