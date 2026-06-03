import { MarkdownSection } from "./MarkdownSection";

type DocSurfaceProps = {
  /** Section props bag (validated server-side). The renderable text lives under
   * `content`; optional `file` / `lines` drive the mono meta strip. A
   * non-string / missing `content` renders an empty doc rather than crashing
   * the overlay (PRD 0010 robustness bar). */
  props: Record<string, unknown>;
};

/**
 * Document surface for a `Markdown` descriptor, rendered through the section
 * registry inside `SectionsOverlay` (PRD 0014 / issue 0088). This is the port
 * of the mockup's `DocSurface` markdown branch (`Design Mockup/p3d-overlay.jsx`
 * → `.ov-md`): a mono meta strip (filename · line count · reading estimate)
 * over the rendered Markdown body.
 *
 * Unlike the mockup — which ships a hand-rolled mini Markdown parser
 * (`renderMd`) — the body REUSES the existing, far more robust `react-markdown`
 * renderer via `MarkdownSection` (GFM, code fences, links, tables). That keeps
 * one Markdown engine in the app and inherits its defensive empty-content
 * handling. Only the chrome (the `.ov-md-meta` strip + the `.ov-md` doc styling
 * in `SectionsOverlay.css`) is ported from the mockup.
 *
 * The meta strip is optional: it only renders when the descriptor carries a
 * `file` name. `lines` falls back to a count derived from the content so a
 * deliverable that omits it still shows a sensible figure. The reading estimate
 * is a coarse words/200-wpm heuristic, floored at one minute.
 *
 * PRD: prd/0014-hud-piste-3d-nacre.md — Issue: issues/0088-overlay-reskin-typed-surfaces.md
 */
export function DocSurface({ props }: DocSurfaceProps) {
  const content = typeof props.content === "string" ? props.content : "";
  const file = typeof props.file === "string" && props.file.length > 0 ? props.file : null;
  const lines =
    typeof props.lines === "number" && Number.isFinite(props.lines)
      ? Math.max(0, Math.round(props.lines))
      : countLines(content);
  const minutes = readingMinutes(content);

  return (
    <article className="ov-md">
      {file !== null ? (
        <div className="ov-md-meta">
          <span className="ov-md-filename">{file}</span>
          <span className="ov-md-divider">·</span>
          <span className="ov-md-stat">{lines} lignes</span>
          <span className="ov-md-divider">·</span>
          <span className="ov-md-stat">lecture ≈ {minutes} min</span>
        </div>
      ) : null}
      {/* Reuse the existing react-markdown renderer for the body. It already
       * emits the `.md-*` classes the `.ov-md` doc styling targets, so the doc
       * picks up the mockup typography without a second Markdown engine. */}
      <MarkdownSection props={props} bare />
    </article>
  );
}

/** Count non-empty lines in the source — a sensible default when the descriptor
 * omits an explicit `lines` count. Empty input is one line (matches an empty
 * editor showing line 1). */
function countLines(content: string): number {
  if (content.length === 0) return 1;
  return content.split("\n").length;
}

/** Coarse reading-time estimate: words / 200 wpm, floored at one minute. Purely
 * cosmetic (the mono meta strip), so precision doesn't matter. */
function readingMinutes(content: string): number {
  const words = content.trim().split(/\s+/).filter(Boolean).length;
  return Math.max(1, Math.round(words / 200));
}
