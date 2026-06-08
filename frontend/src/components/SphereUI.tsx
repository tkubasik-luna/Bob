import { useCallback, useEffect, useState } from "react";
import { useMicCapture } from "../audio/useMicCapture";
import { useChatWsBridge } from "../hooks/useChatWsBridge";
import { useTurnState } from "../hooks/useTurnState";
import { useVoiceMode } from "../hooks/useVoiceMode";
import { useAudioLevel } from "../sphere/useAudioLevel";
import { type SphereDerivedState, useSphereState } from "../sphere/useSphereState";
import { useDevTweaksStore } from "../state/devTweaksStore";
import { useChatStore } from "../store/chatStore";
import { useDeliverableStore } from "../store/deliverableStore";
import type { ComponentDescriptor } from "../types/ws";
import "./piste/idleState.css";
import { BackgroundGrain } from "./piste/BackgroundGrain";
import { BottomBar } from "./piste/BottomBar";
import { CoreSlot } from "./piste/CoreSlot";
import { DataSlot } from "./piste/DataSlot";
import { FloorIndicatorView } from "./piste/FloorIndicator";
import { Identity } from "./piste/Identity";
import { SettingsControl } from "./piste/SettingsControl";
import { TaskSlot } from "./piste/TaskSlot";
import { DevControls } from "./sphere/DevControls";
import { SectionsOverlay } from "./sphere/SectionsOverlay";
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

// PRD 0014 — the HUD `new` window is the « Piste 3D · Nacre » scene. SphereUI
// is the SHELL: it owns the WS bridge, the sphere-state derivation, the audio
// ref, the Cmd+Shift+D debug-window listener, and the single overlay state;
// everything visual is delegated to the `components/piste/*` slot components so
// the five downstream issues (0084–0091) can each own a disjoint file without
// editing the shell. The scoped styling lives in `styles/p3d.css`.
//
// V1 locked aesthetics still flow through `devTweaksStore` (warm + calm +
// liquid defaults; `?dev=1` flips them at runtime via `<DevControls/>`) so the
// orb mount stays unconditional. The high-level sphere state is derived from
// the chat store via `useSphereState` and can be overridden by
// `devTweaksStore.forcedState`.
//
// The WS connection lives at the top of the tree: `useChatWsBridge` owns the
// single socket, dispatches every incoming `ServerMessage` into the store, and
// exposes `send` to the input field via React Context so the leaf doesn't open
// a second connection.
export function SphereUI() {
  const derivedState = useSphereState();
  const forcedState = useDevTweaksStore((s) => s.forcedState);
  const motion = useDevTweaksStore((s) => s.motion);
  const glow = useDevTweaksStore((s) => s.glow);
  const variant = useDevTweaksStore((s) => s.variant);
  const mood = useDevTweaksStore((s) => s.mood);
  const theme = useDevTweaksStore((s) => s.theme);
  const effectiveState = forcedState ?? derivedState;
  // `TranscriptLine` only knows the 4 production states; the dev override can
  // widen to `listen` / `alert`, both of which fall through to the default
  // branch (assistant snippet or hint). Narrow back here so we don't change
  // the leaf signature.
  const transcriptState = forcedStateForTranscript(effectiveState);
  const { status: wsStatus, send, sendBinary } = useChatWsBridge();
  // Tap the live TTS RMS so the orb pulses with the actual voice. The hook
  // returns a stable ref — passing it down keeps SphereCanvas's rAF loop
  // reading the latest value without triggering a parent re-render.
  const audioLevelRef = useAudioLevel();

  // PRD 0016 / issue 0099 — the « Listen » mic path. The HUD `new` window owns
  // the mic; it is armed only while the voice toggle is ON *and* the socket is
  // open (so `voice_start` + binary frames never fire on a dead connection).
  // `useMicCapture` handles getUserMedia + the AudioWorklet + the
  // voice_start/voice_stop framing; mounting it here is the whole wiring.
  const { voiceEnabled } = useVoiceMode();
  // PRD 0016 Annexe G / issue 0101 — the half-duplex mute gate. The live voice
  // floor is lifted HERE (one `/ws/debug` socket) and shared with the
  // FloorIndicator pill below. Browser AEC is enabled in getUserMedia but is
  // imperfect, so without this gate Bob's own TTS leaks into the mic during
  // `bob_speaking` and the 200 ms barge-in window cuts him off mid-reply — the
  // single biggest perceived voice-reliability bug. Muting outbound PCM while
  // Bob speaks keeps the capture graph armed (no mic re-prompt) and resumes the
  // instant the floor leaves `bob_speaking`.
  const floor = useTurnState();
  useMicCapture({
    enabled: voiceEnabled && wsStatus === "open",
    send,
    sendBinary,
    windowName: "new",
    muteOutbound: floor === "bob_speaking",
  });

  // PRD 0014 / issue 0091 — cold-start ↔ rest orchestration. `hasActivity` is
  // TRUE once any REAL datum exists in the session and FALSE again when it all
  // clears. The data surfaces, read straight from the existing stores (no
  // duplicated state):
  //   • a message (the user's first prompt OR Bob's reply) — `messages.length`;
  //   • an in-flight streamed turn, before its closing `assistant_msg` lands —
  //     `streamingAssistant` (so the deck/dock reveal the instant Bob starts,
  //     not only once the bubble is committed);
  //   • a sub-task — `tasks` (left deck content);
  //   • a generated deliverable — `deliverableStore.byId` (right dock content).
  // Each selector is a narrow boolean, so the shell only re-renders when the
  // EMPTY ↔ NON-EMPTY edge flips — not on every message/task mutation. The fade
  // is a CSS transition keyed on the derived `.is-idle` class, so the motion
  // follows the real event timing with no setTimeout choreography. The store
  // map empties on `chatStore` reset / `deliverableStore.reset()` (new session),
  // which flips `hasActivity` back to false → the shell returns to rest.
  const hasMessages = useChatStore((s) => s.messages.length > 0);
  const isStreaming = useChatStore((s) => s.streamingAssistant !== null);
  const hasTasks = useChatStore((s) => Object.keys(s.tasks).length > 0);
  const hasDeliverables = useDeliverableStore((s) => Object.keys(s.byId).length > 0);
  const hasActivity = hasMessages || isStreaming || hasTasks || hasDeliverables;

  // PRD 0014 — a single overlay state holding the list of section descriptors
  // the SectionsOverlay renders. Per the PRD the overlay is now CLICK-ONLY: the
  // legacy auto-open effects (streamed `ui_payload`, last `assistant_msg`, text
  // heuristic, task result) are gone — issue 0088 owns the click-to-open story.
  // Slots open it via `openOverlay(...)`; the close paths (Esc / X / backdrop /
  // DISMISS) clear it through `SectionsOverlay`'s `onClose`.
  const [overlaySections, setOverlaySections] = useState<ComponentDescriptor[] | null>(null);
  // Stable opener handed to slots (DataSlot today; more in 0087+). Empty dep
  // array — the setter is stable, so the callback identity stays stable and
  // slots can list it as a dep without re-binding each render.
  const openOverlay = useCallback(
    (sections: ComponentDescriptor[]) => setOverlaySections(sections),
    [],
  );

  // PRD 0005 — Cmd+Shift+D toggles the dedicated debug window. The listener
  // attaches at mount and unattaches on unmount; the guard rejects key events
  // whose target is a text input so the user can still type `D` while composing
  // a message. `event.code === "KeyD"` is keyboard-layout agnostic (matches the
  // physical key, not the produced character).
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

  const overlayOpen = overlaySections !== null && overlaySections.length > 0;
  // Orb props forwarded straight through to the CoreSlot placeholder (issue
  // 0084 keeps the same binding when it swaps the internals).
  const orbProps = {
    state: effectiveState,
    variant,
    motion,
    glow,
    theme,
    mood,
    audioLevelRef,
  };
  return (
    <SphereWsContext.Provider value={send}>
      {/* The piste root. Carries the foundation layout/camera/panel modifiers
       * (`layout-depth cam-deep panel-frost`) plus the runtime state/theme/mood
       * classes, and toggles `has-surface` while the overlay is open so the
       * stage recedes (see `styles/p3d.css`). Issue 0091 adds `is-idle` while the
       * session is empty (cold start / rest): the deck + dock recede and the
       * centred invitation shows; the fade-in on first real data is a pure CSS
       * transition keyed on this class (see `piste/idleState.css`). */}
      <div
        className={`piste layout-depth cam-deep panel-frost theme-${theme} mood-${mood} state-${effectiveState} ${overlayOpen ? "has-surface" : ""} ${hasActivity ? "" : "is-idle"}`}
      >
        {/* Tauri v2 borderless drag region (#0036). The `?ui=new` window has
         * `decorations: false` so the OS chrome is gone; this transparent 28px
         * top strip carries `-webkit-app-region: drag` so the user can still
         * move the window. Inputs/buttons opt back out via no-drag in hud.css.
         * Rendered FIRST so it sits above the canvas but underneath
         * pointer-events:auto HUD zones in the stacking order. */}
        <div className="drag-region" />
        <BackgroundGrain />
        <Identity />
        {/* PRD 0016 Annexe A.2 / issue 0108 — the voice-floor pill (top-left,
         * below the identity). Driven purely by `turn_state` voice events from
         * `/ws/debug` (via `useTurnState`); mounting it is the whole wiring. It
         * is orthogonal to the orb state and only animates during a real voice
         * turn. */}
        <FloorIndicatorView floor={floor} />
        {/* The 3D stage: perspective on `.stage-3d`, the slow camera drift +
         * preserve-3d context on `.stage-cam`, then the three depth-positioned
         * slots. The orb is the placeholder SphereCanvas (issue 0084 replaces
         * it); task/data are empty stubs (0085/0086/0087 fill them). */}
        <div className="stage-3d">
          <div className="stage-cam">
            <div className="slot-task">
              <TaskSlot />
            </div>
            <div className="slot-core">
              <CoreSlot {...orbProps} />
            </div>
            <div className="slot-data">
              <DataSlot onOpenDeliverable={openOverlay} />
            </div>
          </div>
        </div>
        {/* Cold-start invitation (issue 0091). A discreet line under the orb that
         * shows only while the session is empty (`.is-idle` reveals it via CSS);
         * it fades out the moment the first real datum arrives. `aria-hidden` +
         * `pointer-events:none` keep it purely decorative. Always mounted so the
         * fade is a CSS transition (no mount/unmount flash). */}
        <div className="piste-invite" aria-hidden="true">
          <span className="invite-glyph" />
          BOB · en attente
          <span className="invite-hint">Posez une question pour commencer</span>
        </div>
        <SettingsControl />
        <BottomBar transcriptState={transcriptState} overlayOpen={overlayOpen} />
        {/* The single sections overlay shell (PRD 0010). Still rendered + still
         * closes on Esc / X / backdrop / DISMISS; it now opens ONLY via
         * `openOverlay(...)` from a slot (click-only per PRD 0014 / issue 0088). */}
        <SectionsOverlay sections={overlaySections} onClose={() => setOverlaySections(null)} />
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
