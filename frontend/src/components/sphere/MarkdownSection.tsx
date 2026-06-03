import ReactMarkdown, { type Components } from "react-markdown";
import remarkGfm from "remark-gfm";

type MarkdownSectionProps = {
  /** Section props bag (validated server-side). The renderable text lives under
   * `content`; a non-string / missing value renders as an empty article rather
   * than crashing the overlay (PRD 0010 robustness bar). */
  props: Record<string, unknown>;
  /** When true, render the `react-markdown` output WITHOUT the wrapping
   * `.ov-md` article — the caller (e.g. `DocSurface`) supplies its own `.ov-md`
   * wrapper plus a meta strip. Defaults to false so standalone use keeps the
   * self-contained article. */
  bare?: boolean;
};

/**
 * Markdown body of a `Markdown` section. Now wrapped by `DocSurface` (PRD 0014
 * / issue 0088) so it renders inside the mockup's Document chrome, but still
 * usable standalone: the same `react-markdown` + `remark-gfm` setup wired to
 * the mockup `.md-*` classes (styled in `SectionsOverlay.css` under `.p3d-ov`).
 * The corner-bracket frame / header / footer / dismiss paths live ONCE in
 * `SectionsOverlay`, so a list of sections shares a single shell.
 *
 * When `bare`, the outer `.ov-md` article is omitted so `DocSurface` can own
 * the wrapper (one `.ov-md` element per Document, with its meta strip).
 *
 * PRD: prd/0010-adaptive-composite-ui.md — Issue: issues/0066-sections-list-pipeline-markdown.md
 */
export function MarkdownSection({ props, bare = false }: MarkdownSectionProps) {
  const raw = props.content;
  const content = typeof raw === "string" ? raw : "";
  const md = (
    <ReactMarkdown remarkPlugins={[remarkGfm]} components={MD_COMPONENTS}>
      {content}
    </ReactMarkdown>
  );
  if (bare) return md;
  return <article className="ov-md">{md}</article>;
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
