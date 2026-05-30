import type { MailProps } from "../../types/ws";

type MailCardProps = {
  /** Section props bag (validated server-side). The renderable mail lives here;
   * it is narrowed to `MailProps` at render time. A malformed payload (missing
   * the required `from`/`subject`) renders an empty card rather than crashing
   * the overlay (PRD 0010 robustness bar). */
  props: Record<string, unknown>;
  /** Test seam — defaults to the Tauri-aware `openExternal` below, which tries
   * `window.open` (the Tauri webview forwards external hosts to the OS
   * browser). Tests pass a `vi.fn()` to assert OPEN clicks route through the
   * right channel with the card's `gmailWebUrl`. */
  openExternal?: (url: string) => void;
  /** Test seam — `READ ALOUD` is a no-op placeholder for MVP (same contract as
   * the former `MailOverlay`). Tests pass a `vi.fn()` to assert the button is
   * wired without faking a TTS runtime. Defaults to a no-op. */
  onReadAloud?: (mail: MailProps) => void;
};

/**
 * Chrome-free body of a single Gmail message, rendered through the section
 * registry inside `SectionsOverlay`. This is the port of the former
 * `MailOverlay` body MINUS the overlay chrome (no corner-brackets, no header,
 * no global footer / dismiss paths — those live ONCE in `SectionsOverlay`).
 *
 * A list of mails therefore renders as a vertical stack of self-contained
 * `MailCard`s inside the single shared overlay shell (PRD 0010 / issue 0067 —
 * fixes the "3 derniers mails" bug where only one card ever appeared).
 *
 * Each card carries its own INLINE actions: `OPEN` (browses to `gmailWebUrl`
 * via the test seam) and `READ ALOUD` (a no-op placeholder for MVP). These are
 * per-card so the user can act on any mail in the stack independently.
 *
 * PRD: prd/0010-adaptive-composite-ui.md — Issue: issues/0067-multi-mail-sections.md
 */
export function MailCard({ props, openExternal = openExternal_, onReadAloud }: MailCardProps) {
  const mail = asMailProps(props);
  if (mail === null) return null;

  const initials = computeInitials(mail.from.name, mail.from.email);
  const timestamp = formatTimestamp(mail.receivedAt);
  const flags = mail.flags ?? [];
  const attachments = mail.attachments ?? [];

  const onOpenClick = () => openExternal(mail.gmailWebUrl);
  const onReadAloudClick = () => onReadAloud?.(mail);

  return (
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

      {/* Per-card inline actions — distinct from the overlay's global footer.
       * `READ ALOUD` is a no-op placeholder for MVP; `OPEN` browses to the
       * mail's `gmailWebUrl`. Neither dismisses the overlay. */}
      <div className="ov-email-actions">
        <button
          type="button"
          className="ov-action ov-action-primary"
          aria-label="read aloud"
          onClick={onReadAloudClick}
        >
          <span className="ov-action-key">↵</span>
          <span>READ ALOUD</span>
        </button>
        <button type="button" className="ov-action" aria-label="open" onClick={onOpenClick}>
          <span className="ov-action-key">↗</span>
          <span>OPEN</span>
        </button>
      </div>
    </div>
  );
}

/** Narrow a section props bag to `MailProps`, or `null` when it lacks the
 * minimum required shape (`from.name` / `from.email` / `gmailWebUrl`). The
 * server validates the descriptor before it reaches the wire, so this is a
 * defensive guard against a malformed payload rather than the primary contract:
 * a bad card renders nothing instead of crashing the whole sections stack. */
function asMailProps(props: Record<string, unknown>): MailProps | null {
  const from = props.from;
  if (typeof from !== "object" || from === null) return null;
  const fromName = (from as Record<string, unknown>).name;
  const fromEmail = (from as Record<string, unknown>).email;
  if (typeof fromName !== "string" || typeof fromEmail !== "string") return null;
  if (typeof props.gmailWebUrl !== "string") return null;
  if (typeof props.subject !== "string") return null;
  return props as unknown as MailProps;
}

/** Open an external URL in the user's default browser.
 *
 * `window.open(url, '_blank')` is the MVP path: the Tauri v2 webview forwards
 * it to the OS browser when the URL host isn't in the app's window list. When
 * `@tauri-apps/plugin-shell` lands, swap this body for `shell.open(url)` — the
 * `openExternal` prop seam means the component signature stays stable. */
function openExternal_(url: string): void {
  if (typeof window === "undefined") return;
  window.open(url, "_blank", "noopener,noreferrer");
}

/** Derive 1-2 uppercase initials from `name` (preferred) or the local part of
 * `email`. Purely cosmetic avatar label — tolerant of weird inputs. */
function computeInitials(name: string, email: string): string {
  const source = name.trim().length > 0 ? name.trim() : (email.split("@")[0] ?? "");
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
 * `DD MMM HH:MM` depending on recency. Best-effort: bad input collapses to an
 * empty string so we don't render `Invalid Date`. */
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
