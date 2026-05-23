import { useMemo } from "react";
import type { SphereDerivedState } from "../../sphere/useSphereState";
import { useChatStore } from "../../store/chatStore";

type TranscriptLineProps = {
  /** Current derived sphere state — drives which slot of the transcript is
   * rendered (hint / thinking dots / assistant snippet). */
  state: SphereDerivedState;
  /** When true the line is taken off the DOM entirely. Wired by the
   * `MarkdownOverlay` integration in a follow-up issue (0031) so the
   * overlay alone carries the visual context once it opens. */
  hidden?: boolean;
};

/** Max characters of the assistant message shown inline before truncation. */
const ASSISTANT_SNIPPET_MAX = 80;

/**
 * Single-line transcript above the input field. Port of the mockup
 * `HUDTranscript` adapted to read real messages from `chatStore`.
 *
 * Rendering rules (by sphere state):
 *   - `idle`  + no messages     → hint `"Tapez pour parler à Bob"`
 *   - `idle`  + last assistant  → snippet of last assistant message
 *   - `think`                   → animated `thinking · · ·` dots
 *   - `speak`                   → snippet of last assistant message
 *   - `error`                   → hint fallback (the sphere glitch carries the
 *                                  error signal; transcript stays low-noise)
 *
 * Fade in/out is delegated to the CSS `@keyframes transcript-in` declared in
 * `hud.css`. We force a re-mount whenever the rendered text or state shifts
 * by changing the `key` prop, which restarts the animation cleanly.
 *
 * PRD: prd/0004-sphere-hud-ui.md — Issue: issues/0030-input-field-transcript-line.md
 */
export function TranscriptLine({ state, hidden = false }: TranscriptLineProps) {
  const messages = useChatStore((s) => s.messages);

  const { lastUser, lastAssistant } = useMemo(() => {
    let user: string | null = null;
    let assistant: string | null = null;
    // Walk back from the end so we surface only the most recent of each
    // role — cheap on the order of one full scan per render but the
    // message list is bounded by user attention span.
    for (let i = messages.length - 1; i >= 0; i--) {
      const m = messages[i];
      if (assistant === null && m.role === "assistant") assistant = m.content;
      if (user === null && m.role === "user") user = m.content;
      if (assistant !== null && user !== null) break;
    }
    return { lastUser: user, lastAssistant: assistant };
  }, [messages]);

  if (hidden) return null;

  // Resolve the slot (`hint` | `thinking` | `text`) + raw text payload up
  // front so we can derive a stable `key` that re-mounts the inner span on
  // any change → CSS animation re-fires for the fade-in pattern.
  let slot: "hint" | "thinking" | "text";
  let text: string;
  switch (state) {
    case "think":
      slot = "thinking";
      text = lastUser ?? "";
      break;
    case "speak":
      slot = lastAssistant ? "text" : "hint";
      text = lastAssistant ? truncate(lastAssistant, ASSISTANT_SNIPPET_MAX) : "";
      break;
    default:
      // idle / error
      if (lastAssistant) {
        slot = "text";
        text = truncate(lastAssistant, ASSISTANT_SNIPPET_MAX);
      } else {
        slot = "hint";
        text = "";
      }
      break;
  }

  return (
    <div className={`hud-transcript ${text || slot !== "hint" ? "has-text" : ""}`}>
      <span key={`${state}_${text ? "on" : "off"}`}>
        {slot === "thinking" ? (
          <span className="hud-transcript-thinking">
            <span>thinking</span>
            <span className="dot d1">·</span>
            <span className="dot d2">·</span>
            <span className="dot d3">·</span>
          </span>
        ) : slot === "text" ? (
          <span className="hud-transcript-text">{text}</span>
        ) : (
          <span className="hud-transcript-hint">Tapez pour parler à Bob</span>
        )}
      </span>
    </div>
  );
}

function truncate(input: string, max: number): string {
  if (input.length <= max) return input;
  return `${input.slice(0, max)}…`;
}
