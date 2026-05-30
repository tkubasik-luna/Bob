import { useCallback, useEffect, useRef, useState } from "react";
import { useChatWsBridge } from "../hooks/useChatWsBridge";
import { shouldOverlayResponse } from "../lib/overlayHeuristic";
import { SphereCanvas } from "../sphere/SphereCanvas";
import { useAudioLevel } from "../sphere/useAudioLevel";
import { type SphereDerivedState, useSphereState } from "../sphere/useSphereState";
import { useDevTweaksStore } from "../state/devTweaksStore";
import { useChatStore } from "../store/chatStore";
import type { ComponentDescriptor, MailProps, Task } from "../types/ws";
import { DevControls } from "./sphere/DevControls";
import { HudTasks } from "./sphere/HudTasks";
import { InputField } from "./sphere/InputField";
import { MailOverlay } from "./sphere/MailOverlay";
import { MuteToggle } from "./sphere/MuteToggle";
import { SectionsOverlay } from "./sphere/SectionsOverlay";
import { TaskOverlay } from "./sphere/TaskOverlay";
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
  // PRD 0010 / issue 0066 — a single overlay state holding the list of section
  // descriptors the SectionsOverlay renders (Markdown today, more later). The
  // standalone MarkdownOverlay is gone; a text-only result now travels as a
  // list-of-one Markdown section.
  const [overlaySections, setOverlaySections] = useState<ComponentDescriptor[] | null>(null);
  // Issue 0053 — parallel state for the Mail overlay, kept until issue 0067
  // folds Mail into the SectionsOverlay registry. A Mail descriptor in a result
  // list is still routed here so the existing MailOverlay keeps working.
  const [overlayMail, setOverlayMail] = useState<MailProps | null>(null);
  // Issue 0052 — per-task overlay state. Clicking a task in `HudTasks`
  // sets this; the overlay subscribes to the task's live reflections
  // (running) or renders its markdown / empty state (finished).
  const [openTaskId, setOpenTaskId] = useState<string | null>(null);
  const openTask: Task | null = openTaskId !== null ? (tasks[openTaskId] ?? null) : null;
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
  // Mirror `openTaskId` into a ref so the task-result auto-open effect can
  // read the currently-open task without taking it as a dependency (which
  // would re-run that effect on unrelated opens). When a task is already open
  // in `TaskOverlay`, that overlay renders the result markdown itself on
  // completion — auto-opening the standalone `MarkdownOverlay` too would stack
  // two identical cards (the "two MD windows" bug).
  const openTaskIdRef = useRef<string | null>(null);
  useEffect(() => {
    openTaskIdRef.current = openTaskId;
  }, [openTaskId]);
  // PRD 0010 / issue 0066 — single dispatch point for a LIST of section
  // descriptors. The streaming `ui_payload` path and the final `assistant_msg.ui`
  // fallback both funnel a `ComponentDescriptor[]` through here.
  //
  // Routing:
  //   - a Mail descriptor still opens the standalone MailOverlay (kept working
  //     until issue 0067 folds Mail into the SectionsOverlay registry);
  //   - every other section (Markdown today, unknown → NotImplemented) opens
  //     the SectionsOverlay as an ordered list.
  //
  // Auto-open weight: a list with ≥1 `structured` section (per the section
  // registry, or a Mail card) opens unconditionally; a text-only list (Markdown
  // only) defers to the caller's `shouldOverlayResponse` heuristic on the text
  // (the caller passes `applyTextHeuristic`). Returns `true` when it opened an
  // overlay so the caller can record the source as evaluated.
  //
  // Wrapped in `useCallback` with an empty dep array — the setters are stable
  // across renders, so the dispatcher identity stays stable too and the effects
  // below can list it as a dep without re-running each render.
  const openOverlayFromSections = useCallback(
    (sections: ComponentDescriptor[] | null, applyTextHeuristic = false): boolean => {
      if (!sections || sections.length === 0) return false;

      // A single Mail descriptor routes to the legacy MailOverlay (issue 0067
      // removes this branch and renders Mail through the SectionsOverlay
      // registry instead). Guard `messageId` defensively so a malformed payload
      // never crashes the render.
      if (sections.length === 1 && sections[0].component === "Mail") {
        const props = sections[0].props as Partial<MailProps> | undefined;
        if (!props || typeof props.messageId !== "string") return false;
        setOverlayMail(props as MailProps);
        return true;
      }

      // Otherwise open the SectionsOverlay. Decide auto-open: any structured
      // section (or a Mail mixed into a larger list) opens unconditionally; a
      // text-only list defers to the text heuristic on the concatenated
      // Markdown content when the caller asked for it.
      const hasStructured = sections.some(
        (s) => s.component === "Mail" || sectionRegistry[s.component]?.structured === true,
      );
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
    // Already showing this task in the per-task overlay? It renders the result
    // itself on completion — don't also pop the standalone overlay.
    if (openTaskIdRef.current === latest.id) return;
    // PRD 0010 / issue 0066 — when the sub-agent produced a STRUCTURED
    // deliverable, `resultPayload` carries the ordered list of section
    // descriptors. Open it through the single dispatcher so the SectionsOverlay
    // rebuilds itself (a Mail section still routes to MailOverlay until 0067).
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

  const overlayOpen =
    (overlaySections !== null && overlaySections.length > 0) || overlayMail !== null;
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
          <HudTasks
            onOpenResult={(content) =>
              setOverlaySections([{ component: "Markdown", props: { content } }])
            }
            onOpenTask={(t) => setOpenTaskId(t.id)}
          />
        </div>
        <div className="hud-zone b">
          <TranscriptLine state={transcriptState} hidden={overlayOpen} />
          <InputField />
        </div>
        {/* PRD 0010 / issue 0066 — the single sections overlay shell. Replaces
         * the standalone MarkdownOverlay; a text-only result is a list-of-one
         * Markdown section here. */}
        <SectionsOverlay sections={overlaySections} onClose={() => setOverlaySections(null)} />
        {/* Mail overlay (issue 0053) — opens via the dispatcher above when a
         * single Mail descriptor lands. Kept independent until issue 0067 folds
         * Mail into the SectionsOverlay registry; the Esc / backdrop / DISMISS
         * paths are scoped to whichever card is mounted. */}
        <MailOverlay mail={overlayMail} onClose={() => setOverlayMail(null)} />
        {/* Per-task overlay (issue 0052) — opens on row click in HudTasks.
         * Kept mutually exclusive with the standalone MarkdownOverlay above:
         * the task-result auto-open effect skips a task that is already open
         * here (`openTaskIdRef` guard), so a finishing task doesn't stack two
         * identical result cards. Rendering both unconditionally keeps the
         * Esc/backdrop dismiss paths independent. */}
        <TaskOverlay task={openTask} onClose={() => setOpenTaskId(null)} />
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
