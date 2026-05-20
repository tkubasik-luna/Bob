import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";

type MarkdownViewProps = {
  props: Record<string, unknown>;
};

/**
 * Renders the `Markdown` component descriptor as rich Markdown (GFM).
 *
 * Expects `props.content` to be a string. Missing/invalid content is rendered
 * as an empty block rather than throwing, so a misbehaving LLM cannot crash
 * the UI.
 */
export function MarkdownView({ props }: MarkdownViewProps) {
  const raw = props.content;
  const content = typeof raw === "string" ? raw : "";
  return (
    <div className="prose prose-invert prose-sm max-w-none rounded-lg bg-neutral-900 px-3 py-2 text-sm text-neutral-100">
      <ReactMarkdown remarkPlugins={[remarkGfm]}>{content}</ReactMarkdown>
    </div>
  );
}
