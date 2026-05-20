type ChatMessageBlockProps = {
  props: Record<string, unknown>;
};

/**
 * Renders the `ChatMessage` component descriptor: a simple chat bubble styled
 * by role.
 *
 * Defensive: unknown roles fall back to assistant styling and missing content
 * renders an empty bubble.
 */
export function ChatMessageBlock({ props }: ChatMessageBlockProps) {
  const rawRole = props.role;
  const isUser = rawRole === "user";
  const rawContent = props.content;
  const content = typeof rawContent === "string" ? rawContent : "";

  return (
    <div className={`flex ${isUser ? "justify-end" : "justify-start"}`}>
      <div
        className={`max-w-[80%] whitespace-pre-wrap rounded-2xl px-3 py-2 text-sm ${
          isUser
            ? "rounded-br-sm bg-blue-600 text-white"
            : "rounded-bl-sm bg-neutral-800 text-neutral-100"
        }`}
      >
        {content}
      </div>
    </div>
  );
}
