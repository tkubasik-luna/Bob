import { type MouseEvent, useEffect, useMemo } from "react";
import ReactMarkdown, { type Components } from "react-markdown";
import remarkGfm from "remark-gfm";

type MarkdownOverlayProps = {
  /** Markdown payload to render. When `null`, the component renders nothing —
   * mounting an empty card is reserved for the open state alone. */
  content: string | null;
  /** Called on Esc, X button, backdrop click, or footer `DISMISS`. The parent
   * owns the open/closed state; this component only signals intent. */
  onClose: () => void;
};

/**
 * Centred markdown overlay card. Port of `Design Mockup/overlay.jsx`
 * `OverlayCard` + `NotesBody`. Renders the live assistant payload via
 * `react-markdown` + `remark-gfm`, with classes wired to the mockup CSS
 * (`.md-h1`, `.md-p`, `.md-ul`, `.md-pre`, …) already present in `hud.css`.
 *
 * Dismiss is multi-pathed: `Esc` (global keydown listener), the header `×`
 * button, the footer `DISMISS` action, and a click on the `.overlay-stage`
 * backdrop (clicks inside `.overlay-card` are swallowed so the card stays
 * mounted). The cross-fade animation declared in `hud.css` (`ov-card-in`)
 * runs each time the body changes — we re-key the body wrapper on `content`
 * so a new payload retriggers the fade-in while the header / footer remain
 * mounted.
 *
 * PRD: prd/0004-sphere-hud-ui.md — Issue: issues/0031-markdown-overlay-auto-trigger.md
 */
export function MarkdownOverlay({ content, onClose }: MarkdownOverlayProps) {
  // Stable REF marker for the header. We hash the first 32 chars of content
  // so the marker is deterministic per payload (no flaky tests, no random
  // strings re-rolled on every render). Falls back to a sentinel when the
  // overlay is closed — we never render in that case but `useMemo` still has
  // to compute *something*.
  const ref = useMemo(() => (content !== null ? markdownRefMarker(content) : "0000"), [content]);

  useEffect(() => {
    if (content === null) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") onClose();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [content, onClose]);

  if (content === null) return null;

  const onBackdropClick = (e: MouseEvent<HTMLDivElement>) => {
    // Only fire when the user clicks the stage itself, not when a click
    // inside the card bubbles up. The card stops propagation explicitly
    // below to make the rule symmetrical.
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
        aria-label="MARKDOWN"
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
            <span className="ov-type-chip">MARKDOWN</span>
          </div>
          <div className="ov-header-right">
            <span className="ov-id-tag">REF · NTS-{ref}</span>
            <button type="button" className="ov-close" onClick={onClose} aria-label="dismiss">
              <span className="ov-close-glyph">✕</span>
            </button>
          </div>
        </header>

        {/* Re-key on content change so the CSS fade-in (`ov-card-in`) on the
         * body wrapper restarts in place. Header / footer stay mounted so
         * the surrounding chrome never blinks. */}
        <div className="ov-body" key={`body-${ref}`}>
          <article className="ov-md">
            <ReactMarkdown remarkPlugins={[remarkGfm]} components={MD_COMPONENTS}>
              {content}
            </ReactMarkdown>
          </article>
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

/** Components map handed to `ReactMarkdown` so the rendered tree picks up the
 * mockup classes (`.md-h1`, `.md-p`, `.md-quote`, …) already styled in
 * `hud.css`. GFM tables (thead/tbody/tr/th/td) and inline elements use the
 * default DOM tags — `.ov-md`'s descendant selectors handle the rest. */
const MD_COMPONENTS: Components = {
  h1: ({ children, ...props }) => (
    <h1 className="md-h md-h1" {...props}>
      {children}
    </h1>
  ),
  h2: ({ children, ...props }) => (
    <h2 className="md-h md-h2" {...props}>
      {children}
    </h2>
  ),
  h3: ({ children, ...props }) => (
    <h3 className="md-h md-h3" {...props}>
      {children}
    </h3>
  ),
  p: ({ children, ...props }) => (
    <p className="md-p" {...props}>
      {children}
    </p>
  ),
  blockquote: ({ children, ...props }) => (
    <blockquote className="md-quote" {...props}>
      {children}
    </blockquote>
  ),
  ul: ({ children, ...props }) => (
    <ul className="md-ul" {...props}>
      {children}
    </ul>
  ),
  ol: ({ children, ...props }) => (
    <ol className="md-ol" {...props}>
      {children}
    </ol>
  ),
  pre: ({ children, ...props }) => (
    <pre className="md-pre" {...props}>
      {children}
    </pre>
  ),
  hr: (props) => <hr className="md-hr" {...props} />,
  a: ({ children, href, ...props }) => (
    <a className="md-link" href={href} target="_blank" rel="noreferrer" {...props}>
      {children}
    </a>
  ),
  code: ({ children, className, ...props }) => {
    // `react-markdown` v10 emits a single `code` slot for both inline and
    // fenced code; fenced blocks live inside our `pre` mapping so we only
    // need to style the inline path here. Heuristic: a fenced block carries
    // a `language-foo` class, an inline span doesn't.
    const isFenced = typeof className === "string" && className.startsWith("language-");
    if (isFenced) {
      return (
        <code className={className} {...props}>
          {children}
        </code>
      );
    }
    return (
      <code className="md-inline-code" {...props}>
        {children}
      </code>
    );
  },
};

/** Derive a 4-char hex from the first 32 characters of `content` using a
 * deterministic FNV-1a hash. The marker is purely cosmetic (header chip) —
 * stability matters for snapshot-style tests; collision likelihood doesn't. */
function markdownRefMarker(content: string): string {
  const sample = content.slice(0, 32);
  // FNV-1a 32-bit. Constants are the spec values; the multiplication is
  // expressed with `Math.imul` so the rollover behaviour is well-defined.
  let hash = 0x811c9dc5;
  for (let i = 0; i < sample.length; i++) {
    hash ^= sample.charCodeAt(i);
    hash = Math.imul(hash, 0x01000193);
  }
  // Mask to 16 bits → 4 hex chars, uppercase to match the mockup chrome.
  const hex = (hash >>> 0).toString(16).slice(-4).toUpperCase();
  return hex.padStart(4, "0");
}
