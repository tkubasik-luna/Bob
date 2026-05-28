import { type MouseEvent, useEffect, useMemo } from "react";
import type { MailProps } from "../../types/ws";

type MailOverlayProps = {
  /** Mail payload to render. When `null`, the component renders nothing —
   * mounting an empty card is reserved for the open state alone, mirroring
   * `MarkdownOverlay`. */
  mail: MailProps | null;
  /** Called on Esc, X button, backdrop click, or footer `DISMISS`. The
   * parent owns the open/closed state; this component only signals intent. */
  onClose: () => void;
  /** Test seam — defaults to the Tauri-aware `openExternal` below, which
   * tries the optional `@tauri-apps/plugin-shell` package and falls back to
   * `window.open`. Tests can pass a `vi.fn()` here to assert OPEN clicks
   * route through the right channel without faking a Tauri runtime. */
  openExternal?: (url: string) => void;
};

/**
 * Centred mail overlay card. Sibling of `MarkdownOverlay`, sharing the same
 * `.overlay-stage` / `.overlay-card` chrome (corner brackets, header strip,
 * footer actions) but rendering an email-shaped body (avatar + from +
 * timestamp + flags + subject + snippet + attachments).
 *
 * Mirrors the `EmailBody` block from `Design Mockup/overlay.jsx` and reuses
 * the `.ov-email-*` / `.ov-flag` / `.ov-attach` / `.ov-avatar-grad-1` CSS
 * rules already defined in `hud.css`.
 *
 * Dismiss is multi-pathed: `Esc` (global keydown listener), the header `×`
 * button, the footer `DISMISS` action, and a click on the `.overlay-stage`
 * backdrop. `READ ALOUD` is a no-op placeholder for MVP. `OPEN` opens
 * `gmailWebUrl` in the user's browser via Tauri shell-open when available,
 * else `window.open`.
 *
 * PRD: prd/0007-gmail-mail-overlay.md — Issue: issues/0053-mail-ui-component-overlay.md
 */
export function MailOverlay({ mail, onClose, openExternal = openExternal_ }: MailOverlayProps) {
  // Stable REF marker for the header (`MAI-xxxx`). We derive it from the
  // messageId so the same email keeps the same marker across re-renders /
  // unmount-remount, and so tests can pin it without flakiness.
  const ref = useMemo(() => (mail !== null ? mailRefMarker(mail.messageId) : "0000"), [mail]);

  useEffect(() => {
    if (mail === null) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") onClose();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [mail, onClose]);

  if (mail === null) return null;

  const onBackdropClick = (e: MouseEvent<HTMLDivElement>) => {
    if (e.target === e.currentTarget) onClose();
  };

  const onCardClick = (e: MouseEvent<HTMLDivElement>) => {
    e.stopPropagation();
  };

  const onOpenClick = () => openExternal(mail.gmailWebUrl);

  const initials = computeInitials(mail.from.name, mail.from.email);
  const timestamp = formatTimestamp(mail.receivedAt);
  const flags = mail.flags ?? [];
  const attachments = mail.attachments ?? [];

  return (
    // biome-ignore lint/a11y/useKeyWithClickEvents: keyboard dismiss is wired globally via the Escape listener installed in `useEffect` above — the backdrop click is a redundant mouse affordance, not the primary dismiss path.
    <div className="overlay-stage" onClick={onBackdropClick}>
      <div className="overlay-beam" />
      {/* biome-ignore lint/a11y/useKeyWithClickEvents: clicks here only stop propagation so backdrop dismiss doesn't fire when the user clicks the card body — no keyboard equivalent is needed (focused buttons handle their own keys). */}
      <div
        className="overlay-card surface-email"
        // biome-ignore lint/a11y/useSemanticElements: native <dialog> brings its own positioning + backdrop semantics that collide with the mockup chrome (`.overlay-stage` is our backdrop, the parent owns open/closed).
        role="dialog"
        aria-label="MAIL"
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
            <span className="ov-type-chip">INBOX</span>
          </div>
          <div className="ov-header-right">
            <span className="ov-id-tag">REF · MAI-{ref}</span>
            <button type="button" className="ov-close" onClick={onClose} aria-label="dismiss">
              <span className="ov-close-glyph">✕</span>
            </button>
          </div>
        </header>

        <div className="ov-body" key={`body-${ref}`}>
          <div className="ov-email">
            <div className="ov-email-meta">
              <div className="ov-avatar ov-avatar-grad-1">{initials}</div>
              <div className="ov-email-meta-text">
                <div className="ov-email-from">
                  <span className="ov-email-name">{mail.from.name}</span>
                  {mail.from.role !== undefined && mail.from.role.length > 0 ? (
                    <span className="ov-email-role">{mail.from.role}</span>
                  ) : null}
                </div>
                <div className="ov-email-addr">
                  {mail.from.email}
                  {timestamp.length > 0 ? `  ·  ${timestamp}` : ""}
                </div>
              </div>
              {flags.length > 0 ? (
                <div className="ov-email-flags">
                  {flags.map((flag) => (
                    <span key={flag} className={`ov-flag ov-flag-${flag}`} data-flag={flag}>
                      {flag.toUpperCase()}
                    </span>
                  ))}
                </div>
              ) : null}
            </div>

            <h2 className="ov-email-subject">{mail.subject}</h2>

            <p className="ov-email-body">{mail.bodyPreview}</p>

            {attachments.length > 0 ? (
              <div className="ov-email-attachments">
                {attachments.map((att) => (
                  <span key={`${att.name}-${att.sizeBytes}`} className="ov-attach">
                    <span className="ov-attach-icon">▤</span>
                    <span className="ov-attach-name">{att.name}</span>
                    <span className="ov-attach-size">{formatBytes(att.sizeBytes)}</span>
                  </span>
                ))}
              </div>
            ) : null}
          </div>
        </div>

        <footer className="ov-footer">
          <button type="button" className="ov-action ov-action-primary" aria-label="read aloud">
            <span className="ov-action-key">↵</span>
            <span>READ ALOUD</span>
          </button>
          <button type="button" className="ov-action" aria-label="open" onClick={onOpenClick}>
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

/** Open an external URL in the user's default browser.
 *
 * `window.open(url, '_blank')` is the MVP path here: the Tauri v2 webview
 * forwards it to the OS browser when the URL host isn't in the app's
 * window list (default behaviour). The PRD calls out a future
 * `@tauri-apps/plugin-shell` `open` upgrade path — when that plugin lands
 * in `package.json`, swap this body for an explicit `shell.open(url)`
 * call (the test seam at the `MailOverlay` props level means we don't
 * have to change the component signature when we do).
 *
 * `noopener,noreferrer` keeps the new window from getting a handle back
 * onto our `window` object (basic anti-pivot hygiene for a webview). */
function openExternal_(url: string): void {
  if (typeof window === "undefined") return;
  window.open(url, "_blank", "noopener,noreferrer");
}

/** Derive 1-2 uppercase initials from `name` (preferred) or the local part
 * of `email`. Used as the avatar label — purely cosmetic, no security
 * weight, so we keep it simple and tolerant of weird inputs. */
function computeInitials(name: string, email: string): string {
  const source = name.trim().length > 0 ? name.trim() : (email.split("@")[0] ?? "");
  // Split on whitespace / common punctuation, take the first letter of the
  // first two non-empty tokens. Falls back to the first letter of the source
  // and finally `?` if everything is empty.
  const tokens = source.split(/[\s.\-_]+/).filter((t) => t.length > 0);
  if (tokens.length >= 2) {
    return (tokens[0][0] + tokens[1][0]).toUpperCase();
  }
  if (tokens.length === 1) {
    return tokens[0].slice(0, 2).toUpperCase();
  }
  return "?";
}

/** Format an ISO 8601 timestamp as `HH:MM today` / `HH:MM yesterday` /
 * `DD MMM HH:MM` depending on recency. Best-effort: bad input collapses
 * to an empty string so we don't render `Invalid Date`. */
function formatTimestamp(iso: string): string {
  const date = new Date(iso);
  if (Number.isNaN(date.getTime())) return "";
  const hh = String(date.getHours()).padStart(2, "0");
  const mm = String(date.getMinutes()).padStart(2, "0");
  const time = `${hh}:${mm}`;
  const now = new Date();
  const same = (a: Date, b: Date) =>
    a.getFullYear() === b.getFullYear() &&
    a.getMonth() === b.getMonth() &&
    a.getDate() === b.getDate();
  if (same(date, now)) return `${time} today`;
  const yesterday = new Date(now);
  yesterday.setDate(now.getDate() - 1);
  if (same(date, yesterday)) return `${time} yesterday`;
  const months = [
    "Jan",
    "Feb",
    "Mar",
    "Apr",
    "May",
    "Jun",
    "Jul",
    "Aug",
    "Sep",
    "Oct",
    "Nov",
    "Dec",
  ];
  return `${String(date.getDate()).padStart(2, "0")} ${months[date.getMonth()]} ${time}`;
}

/** Pretty-print a byte count as `KB` / `MB` / `GB`. Mirrors the mockup
 * `2.4 MB` / `18 KB` formatting. */
function formatBytes(bytes: number): string {
  if (!Number.isFinite(bytes) || bytes < 0) return "—";
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(0)} KB`;
  if (bytes < 1024 * 1024 * 1024) return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
  return `${(bytes / (1024 * 1024 * 1024)).toFixed(2)} GB`;
}

/** Derive a 4-char hex from `messageId` so the `REF · MAI-xxxx` chip stays
 * stable for a given email. Same FNV-1a strategy as `MarkdownOverlay`'s
 * `markdownRefMarker` — purely cosmetic, collision likelihood doesn't
 * matter. */
function mailRefMarker(messageId: string): string {
  const sample = messageId.slice(0, 32);
  let hash = 0x811c9dc5;
  for (let i = 0; i < sample.length; i++) {
    hash ^= sample.charCodeAt(i);
    hash = Math.imul(hash, 0x01000193);
  }
  const hex = (hash >>> 0).toString(16).slice(-4).toUpperCase();
  return hex.padStart(4, "0");
}
