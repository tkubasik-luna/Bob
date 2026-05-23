import { useEffect, useRef, useState } from "react";
import { useChatWsBridge } from "../hooks/useChatWsBridge";
import { shouldOverlayResponse } from "../lib/overlayHeuristic";
import { SphereCanvas } from "../sphere/SphereCanvas";
import { useAudioLevel } from "../sphere/useAudioLevel";
import { type SphereDerivedState, useSphereState } from "../sphere/useSphereState";
import { useDevTweaksStore } from "../state/devTweaksStore";
import { useChatStore } from "../store/chatStore";
import { DevControls } from "./sphere/DevControls";
import { HudTasks } from "./sphere/HudTasks";
import { InputField } from "./sphere/InputField";
import { MarkdownOverlay } from "./sphere/MarkdownOverlay";
import { MuteToggle } from "./sphere/MuteToggle";
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
  // Tap the live TTS RMS so the sphere pulses with the actual voice. The
  // hook returns a stable ref — passing it down keeps SphereCanvas's rAF
  // loop reading the latest value without triggering a parent re-render.
  const audioLevelRef = useAudioLevel();
  // Overlay state — owned here so the transcript line can hide while the
  // overlay carries the visual context. The opening trigger is driven by the
  // last assistant message (`shouldOverlayResponse` heuristic from #0026).
  // Closing it stays a user gesture: a `false` heuristic on a later message
  // never auto-dismisses the open card — only Esc / X / backdrop / DISMISS do.
  const messages = useChatStore((s) => s.messages);
  const [overlayContent, setOverlayContent] = useState<string | null>(null);
  // Track the id of the last assistant message we evaluated against the
  // overlay heuristic. Keying on id (not content) means: once the user
  // dismisses the overlay via Esc / X / backdrop / DISMISS, the effect won't
  // reopen the same message even though `overlayContent` flips back to null.
  // A *new* assistant message (different id) re-triggers evaluation.
  const lastEvaluatedMsgIdRef = useRef<string | null>(null);
  useEffect(() => {
    // Walk back to the most recent non-empty assistant message; older entries
    // are uninteresting because the heuristic is evaluated per *latest* turn.
    let lastAssistant: { id: string; content: string } | null = null;
    for (let i = messages.length - 1; i >= 0; i--) {
      const m = messages[i];
      if (m.role === "assistant" && m.content.length > 0) {
        lastAssistant = { id: m.id, content: m.content };
        break;
      }
    }
    if (lastAssistant === null) return;
    if (lastEvaluatedMsgIdRef.current === lastAssistant.id) return;
    lastEvaluatedMsgIdRef.current = lastAssistant.id;
    if (!shouldOverlayResponse(lastAssistant.content)) return;
    setOverlayContent(lastAssistant.content);
  }, [messages]);

  const overlayOpen = overlayContent !== null;
  return (
    <SphereWsContext.Provider value={send}>
      <div
        className={`app theme-${theme} mood-${mood} state-${effectiveState} ${overlayOpen ? "has-surface surface-notes" : "surface-none"}`}
      >
        {/* Tauri v2 borderless drag region (#0036). The `?ui=new` window has
         * `decorations: false` so the OS chrome is gone; this transparent
         * 28px top strip carries `-webkit-app-region: drag` so the user can
         * still move the window. Inputs/buttons opt back out via no-drag in
         * hud.css. Rendered FIRST so it sits above the canvas but underneath
         * pointer-events:auto HUD zones in the stacking order. */}
        <div className="drag-region" />
        <SphereCanvas
          state={effectiveState}
          variant={variant}
          motion={motion}
          glow={glow}
          theme={theme}
          mood={mood}
          audioLevelRef={audioLevelRef}
        />
        <div className="hud-zone tr">
          <HudTasks />
        </div>
        <div className="hud-zone b">
          <TranscriptLine state={transcriptState} hidden={overlayOpen} />
          <InputField />
        </div>
        <MarkdownOverlay content={overlayContent} onClose={() => setOverlayContent(null)} />
        <MuteToggle />
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
