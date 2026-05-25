import { useEffect } from "react";
import { useTaskEvents } from "../../hooks/useTaskEvents";
import type { Task } from "../../types/ws";
import type { DebugEvent } from "../../types/ws-debug";
import { MarkdownOverlay } from "./MarkdownOverlay";

type TaskOverlayProps = {
  /** Task to render. `null` keeps the overlay closed. */
  task: Task | null;
  /** Called on Esc, X, backdrop click, footer DISMISS. The parent owns the
   * open/closed state; this component only signals intent. */
  onClose: () => void;
};

/**
 * Per-task overlay (PRD 0006 / issue 0052).
 *
 * Three render modes driven by the task state:
 *
 * - **Running** (`state in {pending, running, waiting_input}`): subscribes to
 *   `/ws/task/{task.id}` via `useTaskEvents` and renders the reflection
 *   timeline live. Snapshot-then-tail is handled inside the hook.
 * - **Finished with result** (`state in {done, failed}` and `task.result`
 *   non-empty): renders the result as Markdown via the existing
 *   `MarkdownOverlay` — the same component the sphere auto-opens for
 *   surfacing-class assistant responses.
 * - **Finished without result**: renders a clear empty-state overlay so the
 *   user is not confused by a blank card. Per the PRD: "clicking a finished
 *   task with no `ui_payload` opens an empty-state overlay (clear, not
 *   blank)."
 *
 * The component is a leaf — no global state mutation, no WS routing. The
 * parent decides which task is open and passes it in; closing is a single
 * `onClose` callback so the same close logic (Esc / backdrop / button) works
 * across all three modes.
 */
export function TaskOverlay({ task, onClose }: TaskOverlayProps) {
  // Always call the hook; passing `null` keeps it idle (no socket opens).
  // This satisfies the rules-of-hooks contract without conditional calls.
  const isRunning =
    task !== null &&
    (task.state === "running" || task.state === "pending" || task.state === "waiting_input");
  const { events, ready } = useTaskEvents(isRunning && task ? task.id : null);

  // Global Esc listener — symmetric with MarkdownOverlay's behaviour so the
  // user has a consistent dismiss path across all overlay modes.
  useEffect(() => {
    if (task === null) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") onClose();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [task, onClose]);

  if (task === null) return null;

  if (isRunning) {
    return <TaskOverlayRunning task={task} events={events} ready={ready} onClose={onClose} />;
  }

  // Finished — done or failed.
  const result = typeof task.result === "string" && task.result.length > 0 ? task.result : null;
  if (result !== null) {
    return <MarkdownOverlay content={result} onClose={onClose} />;
  }
  return <TaskOverlayEmptyState task={task} onClose={onClose} />;
}

/**
 * Live reflection timeline for a running task.
 *
 * Renders one row per event with a coloured pill keyed off the
 * `payload.kind` field set by the sub-agent runner (issue 0052):
 *
 * - `thought` — internal reasoning step.
 * - `tool_invoke` — sub-agent is about to call a tool.
 * - `tool_result` — paired result frame for the preceding invoke.
 * - `addendum_received` — user enrichment landed at the iteration boundary.
 * - `status_change` — task state transition (terminal or `waiting_input`).
 *
 * Events without a `kind` payload still render but with a neutral pill —
 * fallback for legacy emits.
 */
function TaskOverlayRunning({
  task,
  events,
  ready,
  onClose,
}: {
  task: Task;
  events: DebugEvent[];
  ready: boolean;
  onClose: () => void;
}) {
  const onBackdrop = (e: React.MouseEvent<HTMLDivElement>) => {
    if (e.target === e.currentTarget) onClose();
  };
  const stopProp = (e: React.MouseEvent<HTMLDivElement>) => {
    e.stopPropagation();
  };

  return (
    // biome-ignore lint/a11y/useKeyWithClickEvents: Esc listener installed in parent useEffect.
    <div className="overlay-stage" onClick={onBackdrop}>
      <div className="overlay-beam" />
      {/* biome-ignore lint/a11y/useKeyWithClickEvents: stop-prop only; focused buttons handle their own keys. */}
      <div
        className="overlay-card surface-notes"
        // biome-ignore lint/a11y/useSemanticElements: matches MarkdownOverlay chrome.
        role="dialog"
        aria-label="TASK REFLECTIONS"
        onClick={stopProp}
      >
        <span className="ov-corner tl" />
        <span className="ov-corner tr" />
        <span className="ov-corner bl" />
        <span className="ov-corner br" />

        <header className="ov-header">
          <div className="ov-header-left">
            <span className="ov-source-tag">BOB · TASK</span>
            <span className="ov-divider">/</span>
            <span className="ov-type-chip">REFLEXIONS</span>
          </div>
          <div className="ov-header-right">
            <span className="ov-id-tag">{task.title}</span>
            <button type="button" className="ov-close" onClick={onClose} aria-label="dismiss">
              <span className="ov-close-glyph">✕</span>
            </button>
          </div>
        </header>

        <div className="ov-body">
          <div className="task-overlay-timeline">
            {!ready && <p className="task-overlay-loading">Chargement du flux de réflexions…</p>}
            {ready && events.length === 0 && (
              <p className="task-overlay-empty">
                Aucune réflexion encore. La tâche vient de démarrer.
              </p>
            )}
            {events.map((event) => (
              <ReflectionRow key={`${event.ts}-${event.source}`} event={event} />
            ))}
          </div>
        </div>

        <footer className="ov-footer">
          <button type="button" className="ov-action" aria-label="dismiss" onClick={onClose}>
            <span className="ov-action-key">ESC</span>
            <span>DISMISS</span>
          </button>
        </footer>
      </div>
    </div>
  );
}

/**
 * One row in the reflection timeline. The pill colour / label is derived
 * from `payload.kind`; the body shows the event's `summary` plus a
 * sub-line with the event timestamp.
 */
function ReflectionRow({ event }: { event: DebugEvent }) {
  const kind = reflectionKind(event);
  return (
    <div className={`task-overlay-row kind-${kind}`} data-kind={kind}>
      <span className="task-overlay-pill" data-kind={kind}>
        {pillLabel(kind)}
      </span>
      <div className="task-overlay-row-body">
        <div className="task-overlay-summary">{event.summary}</div>
        <div className="task-overlay-ts">{formatTime(event.ts)}</div>
      </div>
    </div>
  );
}

type ReflectionKind =
  | "thought"
  | "tool_invoke"
  | "tool_result"
  | "addendum_received"
  | "status_change"
  | "other";

function reflectionKind(event: DebugEvent): ReflectionKind {
  const k = event.payload?.kind;
  if (
    k === "thought" ||
    k === "tool_invoke" ||
    k === "tool_result" ||
    k === "addendum_received" ||
    k === "status_change"
  ) {
    return k;
  }
  return "other";
}

function pillLabel(kind: ReflectionKind): string {
  switch (kind) {
    case "thought":
      return "PENSÉE";
    case "tool_invoke":
      return "OUTIL";
    case "tool_result":
      return "RÉSULTAT";
    case "addendum_received":
      return "ADDENDUM";
    case "status_change":
      return "ÉTAT";
    case "other":
      return "•";
  }
}

function formatTime(ts: string): string {
  // ISO 8601 → HH:MM:SS (drops the date, which is implicit since the
  // session is live).
  const match = /T(\d{2}:\d{2}:\d{2})/.exec(ts);
  return match?.[1] ?? ts;
}

/**
 * Empty-state overlay for a finished task with no `ui_payload`. Per the
 * PRD: "opens an empty-state overlay (clear, not blank)". We render the
 * same chrome as the running overlay so the visual transition between
 * the two modes is seamless.
 */
function TaskOverlayEmptyState({ task, onClose }: { task: Task; onClose: () => void }) {
  const onBackdrop = (e: React.MouseEvent<HTMLDivElement>) => {
    if (e.target === e.currentTarget) onClose();
  };
  const stopProp = (e: React.MouseEvent<HTMLDivElement>) => {
    e.stopPropagation();
  };

  return (
    // biome-ignore lint/a11y/useKeyWithClickEvents: Esc listener installed in parent useEffect.
    <div className="overlay-stage" onClick={onBackdrop}>
      <div className="overlay-beam" />
      {/* biome-ignore lint/a11y/useKeyWithClickEvents: stop-prop only; focused buttons handle their own keys. */}
      <div
        className="overlay-card surface-notes"
        // biome-ignore lint/a11y/useSemanticElements: matches MarkdownOverlay chrome.
        role="dialog"
        aria-label="TASK EMPTY"
        onClick={stopProp}
      >
        <span className="ov-corner tl" />
        <span className="ov-corner tr" />
        <span className="ov-corner bl" />
        <span className="ov-corner br" />

        <header className="ov-header">
          <div className="ov-header-left">
            <span className="ov-source-tag">BOB · TASK</span>
            <span className="ov-divider">/</span>
            <span className="ov-type-chip">VIDE</span>
          </div>
          <div className="ov-header-right">
            <span className="ov-id-tag">{task.title}</span>
            <button type="button" className="ov-close" onClick={onClose} aria-label="dismiss">
              <span className="ov-close-glyph">✕</span>
            </button>
          </div>
        </header>

        <div className="ov-body">
          <p className="task-overlay-empty-state">Aucune synthèse pour cette tâche.</p>
        </div>

        <footer className="ov-footer">
          <button type="button" className="ov-action" aria-label="dismiss" onClick={onClose}>
            <span className="ov-action-key">ESC</span>
            <span>DISMISS</span>
          </button>
        </footer>
      </div>
    </div>
  );
}
