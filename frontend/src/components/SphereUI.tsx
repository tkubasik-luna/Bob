import { useCallback, useEffect, useRef, useState } from "react";
import { useChatWsBridge } from "../hooks/useChatWsBridge";
import { shouldOverlayResponse } from "../lib/overlayHeuristic";
import { SphereCanvas } from "../sphere/SphereCanvas";
import { useAudioLevel } from "../sphere/useAudioLevel";
import { type SphereDerivedState, useSphereState } from "../sphere/useSphereState";
import { useDevTweaksStore } from "../state/devTweaksStore";
import { useChatStore } from "../store/chatStore";
import type { ComponentDescriptor } from "../types/ws";
import { AgentActivityPanel } from "./AgentActivityPanel";
import { DevControls } from "./sphere/DevControls";
import { InputField } from "./sphere/InputField";
import { MuteToggle } from "./sphere/MuteToggle";
import { ProviderPicker } from "./sphere/ProviderPicker";
import { SectionsOverlay } from "./sphere/SectionsOverlay";
import { TranscriptLine } from "./sphere/TranscriptLine";
import { sectionRegistry } from "./sphere/sectionRegistry";
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
  // PRD 0006 / issue 0049 — the streaming pipeline pushes the `ui_payload`
  // frame as soon as the LLM closes the argument object, well before the
  // closing `assistant_msg`. We watch the streaming buffer for a non-null
  // `ui` so the overlay opens "while Jarvis is still talking" rather than
  // at the very end of the turn.
  const streamingUi = useChatStore((s) => s.streamingAssistant?.ui ?? null);
  // PRD 0010 — a single overlay state holding the list of section descriptors
  // the SectionsOverlay renders (Markdown + Mail today, more later). The
  // standalone MarkdownOverlay (0066) and MailOverlay (0067) are both gone:
  // every result — text or mail — travels as a `ComponentDescriptor[]` and is
  // rendered through the unified registry. A text-only result is a list-of-one
  // Markdown section; a mail result is one Mail section per message.
  const [overlaySections, setOverlaySections] = useState<ComponentDescriptor[] | null>(null);
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
  // Issue 0049 — track which streamed msg_ids have already opened the
  // overlay so a late-arriving `assistant_msg` (carrying the same `ui`)
  // doesn't re-open it after the user dismissed it. The ref outlives the
  // streaming buffer lifecycle (the buffer is cleared when the
  // `assistant_msg` lands; we still need to remember we already acted).
  const evaluatedStreamUiRef = useRef<Set<string>>(new Set());
  // PRD 0010 / issue 0066 — single dispatch point for a LIST of section
  // descriptors. The streaming `ui_payload` path and the final `assistant_msg.ui`
  // fallback both funnel a `ComponentDescriptor[]` through here.
  //
  // Routing (PRD 0010 / issue 0067): every section — Markdown, Mail, or an
  // unknown name (→ NotImplemented) — opens the single SectionsOverlay as an
  // ordered list. The standalone MailOverlay is gone; a Mail descriptor is just
  // a `structured` section in the registry.
  //
  // Auto-open weight: a list with ≥1 `structured` section (per the section
  // registry — `Mail` is structured) opens unconditionally; a text-only list
  // (Markdown only) defers to the caller's `shouldOverlayResponse` heuristic on
  // the text (the caller passes `applyTextHeuristic`). Returns `true` when it
  // opened an overlay so the caller can record the source as evaluated.
  //
  // Wrapped in `useCallback` with an empty dep array — the setters are stable
  // across renders, so the dispatcher identity stays stable too and the effects
  // below can list it as a dep without re-running each render.
  const openOverlayFromSections = useCallback(
    (sections: ComponentDescriptor[] | null, applyTextHeuristic = false): boolean => {
      if (!sections || sections.length === 0) return false;

      // Open the SectionsOverlay. Decide auto-open: any structured section (per
      // the registry — `Mail` is structured) opens unconditionally; a text-only
      // list defers to the text heuristic on the concatenated Markdown content
      // when the caller asked for it.
      const hasStructured = sections.some((s) => sectionRegistry[s.component]?.structured === true);
      if (applyTextHeuristic && !hasStructured) {
        const text = sections
          .map((s) => (typeof s.props?.content === "string" ? (s.props.content as string) : ""))
          .join("\n\n")
          .trim();
        if (text.length === 0 || !shouldOverlayResponse(text)) return false;
      }
      setOverlaySections(sections);
      return true;
    },
    [],
  );

  // PRD 0006 / issue 0049 — open the overlay as soon as the streamed
  // `ui_payload` lands. Issue 0053 generalised the dispatch over the
  // component discriminator (Markdown vs Mail today, more later) via
  // `openOverlayFromDescriptor`. The heuristic-driven path below still
  // handles legacy / non-streamed bubbles (proactive pushes, degrade
  // paths) so we do NOT bypass it.
  useEffect(() => {
    if (streamingUi === null) return;
    const msgId = useChatStore.getState().streamingAssistant?.msgId ?? null;
    if (msgId === null) return;
    if (evaluatedStreamUiRef.current.has(msgId)) return;
    evaluatedStreamUiRef.current.add(msgId);
    // The streamed `ui` is a single descriptor — lift it onto a list-of-one.
    openOverlayFromSections([streamingUi]);
  }, [streamingUi, openOverlayFromSections]);
  // Fallback for the streamed `ui_payload` path: open the overlay from the
  // FINAL `assistant_msg`'s `ui` field. The streamed `ui_payload` frame is
  // routed through the single process-wide ws emitter (last-connected window
  // wins), so a window that asked the question can miss it entirely — but it
  // always receives the closing `assistant_msg`, which carries the same
  // descriptor. Dedup by msg id via the shared `evaluatedStreamUiRef` so the
  // streaming path and this one never double-open (or re-open after the user
  // dismissed the card). The dispatcher routes Markdown vs Mail.
  useEffect(() => {
    let lastAssistant: (typeof messages)[number] | null = null;
    for (let i = messages.length - 1; i >= 0; i--) {
      const m = messages[i];
      if (m.role === "assistant" && !m.proactive) {
        lastAssistant = m;
        break;
      }
    }
    if (lastAssistant === null) return;
    if (evaluatedStreamUiRef.current.has(lastAssistant.id)) return;
    // `assistant_msg.ui` is already a `ComponentDescriptor[]` (the Jarvis say.ui
    // contract) — open the whole list of sections at once. A text-only list
    // (Markdown) defers to the `shouldOverlayResponse` heuristic; a structured
    // section opens unconditionally.
    const sections = lastAssistant.ui ?? null;
    if (!openOverlayFromSections(sections, true)) return;
    evaluatedStreamUiRef.current.add(lastAssistant.id);
  }, [messages, openOverlayFromSections]);
  useEffect(() => {
    // Walk back to the most recent non-empty assistant message; older entries
    // are uninteresting because the heuristic is evaluated per *latest* turn.
    // Proactive pushes (sub-task done/ask_user synthesis) are spoken-only —
    // their text is a short TTS announcement, never an overlay card. The full
    // task result surfaces via the task-result effect below instead, so a long
    // synthesis must not trip `shouldOverlayResponse` and duplicate itself.
    let lastAssistant: { id: string; content: string } | null = null;
    for (let i = messages.length - 1; i >= 0; i--) {
      const m = messages[i];
      if (m.role === "assistant" && !m.proactive && m.content.length > 0) {
        lastAssistant = { id: m.id, content: m.content };
        break;
      }
    }
    if (lastAssistant === null) return;
    if (lastEvaluatedMsgIdRef.current === lastAssistant.id) return;
    lastEvaluatedMsgIdRef.current = lastAssistant.id;
    if (!shouldOverlayResponse(lastAssistant.content)) return;
    // A heuristic-triggered text bubble travels as a list-of-one Markdown
    // section through the same SectionsOverlay registry.
    setOverlaySections([{ component: "Markdown", props: { content: lastAssistant.content } }]);
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
    // PRD 0010 / issue 0067 — when the sub-agent produced a STRUCTURED
    // deliverable, `resultPayload` carries the ordered list of section
    // descriptors. Open it through the single dispatcher so the SectionsOverlay
    // rebuilds itself (Mail sections render as stacked MailCards in the registry).
    // The dispatcher returns `false` for an empty / malformed list; in that
    // case we fall through to the legacy Markdown path on the `result` text,
    // wrapped as a list-of-one Markdown section.
    if (
      latest.resultPayload &&
      latest.resultPayload.length > 0 &&
      openOverlayFromSections(latest.resultPayload, true)
    ) {
      return;
    }
    const result = latest.result;
    if (typeof result !== "string") return;
    if (!shouldOverlayResponse(result)) return;
    setOverlaySections([{ component: "Markdown", props: { content: result } }]);
  }, [tasks, openOverlayFromSections]);

  const overlayOpen = overlaySections !== null && overlaySections.length > 0;
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
        {/* PRD 0011 / issue 0076 — right-edge agent-activity panel replaces the
         * top-right HudTasks zone. Collapsed = a narrow rail (active-agent
         * badges + count); expanded = the full multi-agent lanes feed. The
         * "résultat" button on a finished lane opens the SAME SectionsOverlay
         * the streamed-ui / task-result paths use, via the shared dispatcher. */}
        <AgentActivityPanel onOpenResult={(sections) => setOverlaySections(sections)} />
        {/* PRD 0012 / issue 0079 — LLM engine picker in the top-left HUD zone.
         * Read-only this slice: it fetches `GET /api/llm/models` on dropdown
         * open and highlights the current selection. Only the Sphere HUD gets
         * the picker; legacy ChatView does not. */}
        <div className="hud-zone tl">
          <ProviderPicker />
        </div>
        <div className="hud-zone b">
          <TranscriptLine state={transcriptState} hidden={overlayOpen} />
          <InputField />
        </div>
        {/* PRD 0010 / issue 0067 — the single sections overlay shell. Replaces
         * BOTH the standalone MarkdownOverlay (0066) and MailOverlay (0067): a
         * text-only result is a list-of-one Markdown section, a mail result is
         * a stack of Mail cards, all rendered through the section registry. */}
        <SectionsOverlay sections={overlaySections} onClose={() => setOverlaySections(null)} />
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
