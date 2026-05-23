import { useEffect, useState } from "react";
import { useChatWsBridge } from "../hooks/useChatWsBridge";
import { shouldOverlayResponse } from "../lib/overlayHeuristic";
import { SphereCanvas } from "../sphere/SphereCanvas";
import { type SphereDerivedState, useSphereState } from "../sphere/useSphereState";
import { useDevTweaksStore } from "../state/devTweaksStore";
import { useChatStore } from "../store/chatStore";
import { DevControls } from "./sphere/DevControls";
import { HudTasks } from "./sphere/HudTasks";
import { InputField } from "./sphere/InputField";
import { MarkdownOverlay } from "./sphere/MarkdownOverlay";
import { TranscriptLine } from "./sphere/TranscriptLine";
import { SphereWsContext } from "./sphere/sphereWsContext";

// V1 locked props per PRD 0004: warm + calm + liquid mercury. Those locked
// defaults now live in `devTweaksStore` so dev mode (`?dev=1`) can flip
// motion / glow / variant / mood / theme at runtime via `<DevControls />`
// without conditional branches in this render path: even in prod we read
// from the dev store, which simply holds the defaults.
//
// The high-level sphere state is derived from the chat store via
// `useSphereState` (issue #0029). Dev mode can override it via
// `devTweaksStore.forcedState` (state pills + keyboard shortcuts in
// `DevControls`); the production derivation kicks back in the moment that
// override is cleared.
//
// The WS connection lives at the top of the `?ui=new` tree (issue #0030
// follow-up): `useChatWsBridge` owns the single socket, dispatches every
// incoming `ServerMessage` into the store, and exposes `send` to the input
// field via React Context so the leaf doesn't open a second connection.
export function SphereUI() {
  const derivedState = useSphereState();
  const forcedState = useDevTweaksStore((s) => s.forcedState);
  const motion = useDevTweaksStore((s) => s.motion);
  const glow = useDevTweaksStore((s) => s.glow);
  const variant = useDevTweaksStore((s) => s.variant);
  const mood = useDevTweaksStore((s) => s.mood);
  const theme = useDevTweaksStore((s) => s.theme);
  const effectiveState = forcedState ?? derivedState;
  // `TranscriptLine` only knows the 4 production states; the dev override
  // can widen to `listen` / `alert`, both of which fall through to the
  // default branch (assistant snippet or hint). Narrow back here so we don't
  // change the leaf signature.
  const transcriptState = forcedStateForTranscript(effectiveState);
  const { send } = useChatWsBridge();
  // Overlay state — owned here so the transcript line can hide while the
  // overlay carries the visual context. The opening trigger is driven by the
  // last assistant message (`shouldOverlayResponse` heuristic from #0026).
  // Closing it stays a user gesture: a `false` heuristic on a later message
  // never auto-dismisses the open card — only Esc / X / backdrop / DISMISS do.
  const messages = useChatStore((s) => s.messages);
  const [overlayContent, setOverlayContent] = useState<string | null>(null);
  useEffect(() => {
    // Walk back to the most recent non-empty assistant message; older entries
    // are uninteresting because the heuristic is evaluated per *latest* turn.
    let lastAssistant: string | null = null;
    for (let i = messages.length - 1; i >= 0; i--) {
      const m = messages[i];
      if (m.role === "assistant" && m.content.length > 0) {
        lastAssistant = m.content;
        break;
      }
    }
    if (lastAssistant === null) return;
    if (!shouldOverlayResponse(lastAssistant)) return;
    if (lastAssistant === overlayContent) return;
    setOverlayContent(lastAssistant);
  }, [messages, overlayContent]);

  const overlayOpen = overlayContent !== null;
  return (
    <SphereWsContext.Provider value={send}>
      <div
        className={`app theme-${theme} mood-${mood} state-${effectiveState} ${overlayOpen ? "has-surface surface-notes" : "surface-none"}`}
      >
        <SphereCanvas
          state={effectiveState}
          variant={variant}
          motion={motion}
          glow={glow}
          theme={theme}
          mood={mood}
        />
        <div className="hud-zone tr">
          <HudTasks />
        </div>
        <div className="hud-zone b">
          <TranscriptLine state={transcriptState} hidden={overlayOpen} />
          <InputField />
        </div>
        <MarkdownOverlay content={overlayContent} onClose={() => setOverlayContent(null)} />
        <DevControls />
      </div>
    </SphereWsContext.Provider>
  );
}

/** Map the wider dev-override state union back onto the four states
 * `TranscriptLine` understands. `listen` and `alert` lack first-class slots
 * in the transcript; collapse them onto `idle` so the snippet/hint path is
 * the one selected (matches what the user would see anyway). */
function forcedStateForTranscript(state: string): SphereDerivedState {
  if (state === "think" || state === "speak" || state === "error" || state === "idle") {
    return state;
  }
  return "idle";
}
