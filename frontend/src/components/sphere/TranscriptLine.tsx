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
 *   - live STT transcript        → WINS over every slot below: what Bob hears
 *                                  as the user speaks (stable prefix solid,
 *                                  tentative tail dimmed; the frozen final is
 *                                  fully solid until Bob's reply clears it)
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
  // PRD 0006 / issue 0049 — when a streamed Jarvis turn is in flight we
  // prefer its progressive buffer over the most-recent persisted bubble.
  // That's how the sphere shows the spoken phrase appearing word-by-word
  // before the closing `assistant_msg` lands.
  const streamingSpeech = useChatStore((s) => s.streamingAssistant?.speech ?? null);
  // PRD 0016 / issue 0099 — the live STT hypothesis (`stt_partial` /
  // `stt_final`): the user's words as Bob hears them, in real time. Written by
  // `useChatWsBridge`, cleared when Bob's reply lands / the turn aborts.
  const liveUser = useChatStore((s) => s.liveUserTranscript);

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

  // The streaming buffer is non-null only while a turn is mid-flight; it
  // wins over the persisted bubble so the user sees a smooth left-to-right
  // text reveal. A non-empty streamed buffer also flips `slot` from `hint`
  // → `text` on the very first delta, removing the "Tapez pour parler" line
  // the moment Jarvis starts talking.
  const effectiveAssistant =
    streamingSpeech !== null && streamingSpeech.length > 0 ? streamingSpeech : lastAssistant;

  if (hidden) return null;

  // The live voice transcript outranks every other slot: while the user
  // speaks (and until Bob replies) the line mirrors what the STT engine
  // hears — the settled prefix solid, the still-churning tail dimmed. The
  // frozen final (`stt_final`) renders fully solid so the user can see
  // exactly what Bob understood before the answer arrives.
  if (liveUser !== null && liveUser.text.length > 0) {
    const stableLen = liveUser.final
      ? liveUser.text.length
      : Math.max(0, Math.min(liveUser.stablePrefixLen, liveUser.text.length));
    const stable = liveUser.text.slice(0, stableLen);
    const tail = liveUser.text.slice(stableLen);
    return (
      <div className="hud-transcript has-text">
        <span key={`user_${liveUser.turnId}_${liveUser.final ? "final" : "partial"}`}>
          <span
            className={`hud-transcript-user ${liveUser.final ? "is-final" : ""}`}
            data-testid="live-user-transcript"
          >
            <span className="hud-transcript-user-stable">{stable}</span>
            {tail.length > 0 && <span className="hud-transcript-user-tail">{tail}</span>}
          </span>
        </span>
      </div>
    );
  }

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
      slot = effectiveAssistant ? "text" : "hint";
      text = effectiveAssistant ? truncate(effectiveAssistant, ASSISTANT_SNIPPET_MAX) : "";
      break;
    default:
      // idle / error
      if (effectiveAssistant) {
        slot = "text";
        text = truncate(effectiveAssistant, ASSISTANT_SNIPPET_MAX);
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
