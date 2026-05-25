import { useMemo } from "react";
import { useChatStore } from "../../store/chatStore";
import type { Task, TaskState } from "../../types/ws";

type HudTasksProps = {
  /** Called when the user clicks a task row that carries a result payload
   * (typically state=done with a non-empty `result`). The parent decides what
   * to do with the markdown — usually re-opening the `MarkdownOverlay`. When
   * omitted, rows stay non-interactive. */
  onOpenResult?: (content: string) => void;
};

/**
 * Top-right "tâches en cours" panel — port of `Design Mockup/hud.jsx`
 * `HUDTasks` adapted to read the real Jarvis sub-tasks from `chatStore`.
 *
 * Mapping store → mockup format:
 * - `pending` / queued    → `.is-queued`, sub `EN FILE`
 * - `running`             → `.is-running`, spinner, sub `${progressStatus ?? "EN COURS"}`
 * - `done`                → `.is-done`, check icon, sub `OK`
 * - `failed`              → `.is-error`, cross, sub `ÉCHEC`
 * - `waiting_input`       → `.is-queued`, sub `ATTENTE INPUT`, `needsAttention` border accent
 *
 * Display: most recent 4 tasks (chronological by `createdAt`), FIFO trim —
 * oldest falls off when a fifth arrives. Done tasks remain visible until they
 * fall off the FIFO window so the user can click one to re-open its result
 * in the markdown overlay (`onOpenResult` callback).
 */
export function HudTasks({ onOpenResult }: HudTasksProps) {
  const tasksMap = useChatStore((s) => s.tasks);

  const ordered = useMemo<Task[]>(
    () => Object.values(tasksMap).sort((a, b) => a.createdAt.localeCompare(b.createdAt)),
    [tasksMap],
  );

  const visible = useMemo(() => ordered.slice(-4), [ordered]);

  const liveCount = ordered.filter(
    (t) => t.state === "running" || t.state === "pending" || t.state === "waiting_input",
  ).length;
  const runningCount = ordered.filter((t) => t.state === "running").length;
  const total = ordered.length;

  return (
    <div className="hud-tasks">
      <div className="hud-tasks-head">
        <span className="hud-tasks-title">TÂCHES · ARRIÈRE-PLAN</span>
        <span className="hud-tasks-count">
          <span className={runningCount > 0 ? "is-live" : ""}>
            {String(liveCount).padStart(2, "0")}
          </span>
          <span className="hud-tasks-sep">/</span>
          <span>{String(total).padStart(2, "0")}</span>
        </span>
      </div>
      <div className="hud-tasks-list">
        {visible.map((task) => (
          <HudTaskRow key={task.id} task={task} onOpenResult={onOpenResult} />
        ))}
      </div>
    </div>
  );
}

function HudTaskRow({
  task,
  onOpenResult,
}: {
  task: Task;
  onOpenResult?: (content: string) => void;
}) {
  const variant = stateToVariant(task.state);
  const sub = formatTaskSub(task);
  const fillWidth = progressFillWidth(task.state);

  // Slice #0024 — `needs_attention` on `waiting_input` tasks gets a subtle
  // accent border so the user notices Jarvis is blocked on them.
  const needsAccent = task.needsAttention === true && task.state === "waiting_input";

  // Interactive once a result payload is attached — typically state=done with
  // a non-empty result. The auto-trigger in `SphereUI` only fires once per
  // task id; this manual path lets the user re-open after dismissing.
  const result = typeof task.result === "string" && task.result.length > 0 ? task.result : null;
  const interactive = result !== null && onOpenResult !== undefined;
  const handleOpen = () => {
    if (interactive && result !== null) onOpenResult(result);
  };

  return (
    <div
      className={`hud-task is-${variant}${needsAccent ? " needs-attention" : ""}${interactive ? " is-clickable" : ""}`}
      data-task-id={task.id}
      role={interactive ? "button" : undefined}
      tabIndex={interactive ? 0 : undefined}
      onClick={interactive ? handleOpen : undefined}
      onKeyDown={
        interactive
          ? (e) => {
              if (e.key === "Enter" || e.key === " ") {
                e.preventDefault();
                handleOpen();
              }
            }
          : undefined
      }
    >
      <span className="hud-task-status" aria-hidden="true" />
      <span className="hud-task-name">{task.title}</span>
      <span className="hud-task-sub">{sub}</span>
      <span className="hud-task-prog">
        <span className="hud-task-prog-fill" style={{ width: `${fillWidth}%` }} />
      </span>
    </div>
  );
}

/** Map the real `TaskState` to the mockup CSS variant slug. */
function stateToVariant(state: TaskState): "queued" | "running" | "done" | "error" {
  switch (state) {
    case "running":
      return "running";
    case "done":
      return "done";
    case "failed":
      return "error";
    case "pending":
    case "waiting_input":
      return "queued";
  }
}

/** Sub-text per state, mirroring the mockup glyph language. */
function formatTaskSub(task: Task): string {
  switch (task.state) {
    case "pending":
      return "EN FILE";
    case "waiting_input":
      return "ATTENTE INPUT";
    case "running":
      // Store has no numeric `progress` (V1) — surface the agent's free-form
      // `progressStatus` if any, otherwise a neutral "en cours" placeholder.
      return task.progressStatus ?? "EN COURS";
    case "done":
      return "OK";
    case "failed":
      return "ÉCHEC";
  }
}

/** Progress bar fill width, in %. Running with no numeric progress shows
 * the bar empty (the spinning arc carries the activity signal). Done /
 * failed fill to 100%, queued / pending leave it 0 (CSS hides it). */
function progressFillWidth(state: TaskState): number {
  if (state === "done" || state === "failed") return 100;
  return 0;
}
