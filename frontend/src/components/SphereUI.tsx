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

/** Cmd+Shift+D toggles the dedicated debug window (PRD 0005). The listener
 * is window-scoped (only fires while the Sphere window has focus) and
 * ignores key events that originated inside text inputs so the user can
 * type a literal `D` in the InputField without opening the debug view. The
 * Tauri command lives in `src-tauri/src/lib.rs` and is registered with the
 * builder. Importing `@tauri-apps/api/core` works fine in browser-only dev
 * (`pnpm dev`); the dynamic import lets us no-op the invocation if Tauri
 * isn't available so the web preview doesn't crash. */
async function invokeToggleDebugWindow(): Promise<void> {
  try {
    const tauri = await import("@tauri-apps/api/core");
    await tauri.invoke("toggle_debug_window");
  } catch {
    // Not running inside Tauri (e.g. `pnpm dev` web preview) — silently
    // swallow. The multi-window plumbing only exists in `pnpm tauri dev`.
  }
}

function isEditableTarget(target: EventTarget | null): boolean {
  if (!(target instanceof HTMLElement)) return false;
  const tag = target.tagName;
  if (tag === "INPUT" || tag === "TEXTAREA") return true;
  if (target.isContentEditable) return true;
  return false;
}

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
  const tasks = useChatStore((s) => s.tasks);
  const [overlayContent, setOverlayContent] = useState<string | null>(null);
  // PRD 0005 — Cmd+Shift+D toggles the dedicated debug window. The listener
  // attaches at mount and unattaches on unmount; the guard rejects key
  // events whose target is a text input so the user can still type `D`
  // while composing a message. `event.code === "KeyD"` is keyboard-layout
  // agnostic (matches the physical key, not the produced character).
  useEffect(() => {
    const onKeyDown = (event: KeyboardEvent) => {
      if (!event.metaKey || !event.shiftKey) return;
      if (event.code !== "KeyD") return;
      if (isEditableTarget(event.target)) return;
      event.preventDefault();
      void invokeToggleDebugWindow();
    };
    window.addEventListener("keydown", onKeyDown);
    return () => window.removeEventListener("keydown", onKeyDown);
  }, []);
  // Dedup keys for already-evaluated sources. Keying by id (not content) means:
  // once the user dismisses the overlay via Esc / X / backdrop / DISMISS, the
  // effect won't reopen the same source even though `overlayContent` flips
  // back to null. A *new* assistant message id, or a *new* task result, is
  // re-evaluated.
  const lastEvaluatedMsgIdRef = useRef<string | null>(null);
  const evaluatedTaskIdsRef = useRef<Set<string>>(new Set());
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
  useEffect(() => {
    // Sub-task results land on `tasks[id].result` (not on the main `messages`
    // stream). The orchestrator follows up with a short synth assistant_msg
    // ("Résultat de la veille UK revenu…") which is too short to trigger the
    // overlay on its own — the long markdown lives only on the task. Surface
    // it here when a task transitions to done with a non-empty result.
    const candidates = Object.values(tasks)
      .filter((t) => t.state === "done" && typeof t.result === "string" && t.result.length > 0)
      .sort((a, b) => (a.updatedAt ?? a.createdAt).localeCompare(b.updatedAt ?? b.createdAt));
    const latest = candidates[candidates.length - 1];
    if (!latest) return;
    if (evaluatedTaskIdsRef.current.has(latest.id)) return;
    evaluatedTaskIdsRef.current.add(latest.id);
    const result = latest.result;
    if (typeof result !== "string") return;
    if (!shouldOverlayResponse(result)) return;
    setOverlayContent(result);
  }, [tasks]);

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
          <HudTasks onOpenResult={setOverlayContent} />
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
