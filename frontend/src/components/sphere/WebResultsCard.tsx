import type { WebResultsProps } from "../../types/ws";

type WebResultsCardProps = {
  /** Section props bag (validated server-side against `WEB_RESULTS`). Narrowed
   * to `WebResultsProps` at render time; a malformed payload (no `query` /
   * `results`) renders an empty card rather than crashing the overlay stack
   * (PRD 0010 robustness bar — same defensive contract as `MailCard`). */
  props: Record<string, unknown>;
  /** Test seam — opening a result browses to its URL. Defaults to the
   * Tauri-aware `openExternal` below (the webview forwards external hosts to
   * the OS browser). Tests pass a `vi.fn()` to assert clicks route the right
   * URL without a real navigation. */
  openExternal?: (url: string) => void;
};

/**
 * Web search results surface — chrome-free body of a ranked `web_search`
 * result, rendered through the section registry inside `SectionsOverlay`.
 * Styled with the Piste 3D · Nacre `ov-web-*` rules (co-located in
 * `SectionsOverlay.css`): an optional Tavily direct `answer` lead, then a list
 * of {title, url, snippet} rows. Each title is a button that opens the source
 * in the browser — so the user can act on ANY result independently (the shell's
 * global `OUVRIR` only targets the first). It is the body ONLY — the corner
 * brackets, header, and global footer live once in `SectionsOverlay`.
 *
 * PRD: web-search.
 */
export function WebResultsCard({ props, openExternal = openExternal_ }: WebResultsCardProps) {
  const data = asWebResultsProps(props);
  if (data === null) return null;

  const answer = data.answer?.trim();

  return (
    <div className="ov-web">
      {answer && answer.length > 0 ? <p className="ov-web-answer">{answer}</p> : null}

      <ul className="ov-web-list">
        {data.results.map((result, index) => (
          <li className="ov-web-item" key={`${result.url}-${index}`}>
            <button
              type="button"
              className="ov-web-title"
              onClick={() => openExternal(result.url)}
              title={result.url}
            >
              <span>{result.title}</span>
              <span className="ov-web-open" aria-hidden="true">
                ↗
              </span>
            </button>
            <div className="ov-web-url">{prettyUrl(result.url)}</div>
            {result.snippet && result.snippet.length > 0 ? (
              <p className="ov-web-snippet">{result.snippet}</p>
            ) : null}
          </li>
        ))}
      </ul>
    </div>
  );
}

/** Narrow a section props bag to `WebResultsProps`, or `null` when it lacks the
 * minimum shape (`query` string + `results` array). Malformed result entries
 * (no string `url`) are dropped rather than crashing the stack; a missing
 * `title` falls back to the URL. The server validates the descriptor before the
 * wire, so this is defence-in-depth, not the primary contract. */
function asWebResultsProps(props: Record<string, unknown>): WebResultsProps | null {
  if (typeof props.query !== "string") return null;
  const rawResults = props.results;
  if (!Array.isArray(rawResults)) return null;

  const results: WebResultsProps["results"] = [];
  for (const item of rawResults) {
    if (typeof item !== "object" || item === null) continue;
    const rec = item as Record<string, unknown>;
    if (typeof rec.url !== "string" || rec.url.length === 0) continue;
    const title = typeof rec.title === "string" && rec.title.length > 0 ? rec.title : rec.url;
    results.push({
      title,
      url: rec.url,
      snippet: typeof rec.snippet === "string" ? rec.snippet : undefined,
    });
  }

  return {
    query: props.query,
    answer: typeof props.answer === "string" ? props.answer : undefined,
    results,
  };
}

/** Compact a URL to `host + path` for the muted source line (drops the scheme
 * and a bare trailing slash). Falls back to the raw string when the URL can't
 * be parsed — never throws. */
function prettyUrl(url: string): string {
  try {
    const parsed = new URL(url);
    const path = parsed.pathname === "/" ? "" : parsed.pathname;
    return `${parsed.host}${path}`;
  } catch {
    return url;
  }
}

/** Open an external URL in the user's default browser. `window.open(url,
 * '_blank')` is the MVP path: the Tauri v2 webview forwards it to the OS
 * browser when the URL host isn't in the app's window list. Mirrors the per-card
 * seam in `MailCard`; swap to `@tauri-apps/plugin-shell` later via the prop. */
function openExternal_(url: string): void {
  if (typeof window === "undefined") return;
  window.open(url, "_blank", "noopener,noreferrer");
}
