import { useEffect, useMemo, useRef, useState } from "react";
import { useChatStore } from "../../store/chatStore";
import type { Task, TaskState } from "../../types/ws";

/**
 * Top-right "tâches en cours" panel — port of `Design Mockup/hud.jsx`
 * `HUDTasks` adapted to read the real Jarvis sub-tasks from `chatStore`.
 *
 * Mapping store → mockup format:
 * - `pending` / queued    → `.is-queued`, sub `EN FILE`
 * - `running`             → `.is-running`, spinner, sub `${progressStatus ?? "EN COURS"}`
 * - `done`                → `.is-done`, check icon, sub `OK`, fade-out after ~3s
 * - `failed`              → `.is-error`, cross, sub `ÉCHEC`
 * - `waiting_input`       → `.is-queued`, sub `ATTENTE INPUT`, `needsAttention` border accent
 *
 * Display limit: last 4 tasks (chronological by `createdAt`). Empty store →
 * header alone with `00/00`. Done tasks fade out after 3s and are hidden
 * thereafter, matching the mockup's `fadeAt` behaviour.
 */
export function HudTasks() {
  const tasksMap = useChatStore((s) => s.tasks);

  const ordered = useMemo<Task[]>(
    () => Object.values(tasksMap).sort((a, b) => a.createdAt.localeCompare(b.createdAt)),
    [tasksMap],
  );

  // Track per-task "done at" timestamps so we can fade out done tasks ~3s
  // after they enter their terminal state. We keep the ref keyed by task id;
  // the `hiddenIds` state mirrors which ones have crossed the fade threshold
  // so React re-renders when the timer fires.
  const doneAtRef = useRef<Map<string, number>>(new Map());
  const [hiddenIds, setHiddenIds] = useState<Set<string>>(() => new Set());

  useEffect(() => {
    const FADE_MS = 3000;
    const now = performance.now();
    const orderedIds = new Set(ordered.map((t) => t.id));

    // Record `doneAt` for any newly-done task; clear stale entries.
    for (const task of ordered) {
      if (task.state === "done") {
        if (!doneAtRef.current.has(task.id)) {
          doneAtRef.current.set(task.id, now);
        }
      } else if (doneAtRef.current.has(task.id)) {
        doneAtRef.current.delete(task.id);
      }
    }
    for (const id of [...doneAtRef.current.keys()]) {
      if (!orderedIds.has(id)) doneAtRef.current.delete(id);
    }

    // Compute the set of currently-hidden ids and dedupe against state to
    // avoid spurious re-renders.
    const computeHidden = (t: number): Set<string> => {
      const next = new Set<string>();
      for (const [id, doneAt] of doneAtRef.current) {
        if (t - doneAt >= FADE_MS) next.add(id);
      }
      return next;
    };
    const isSameSet = (a: Set<string>, b: Set<string>) => {
      if (a.size !== b.size) return false;
      for (const id of a) if (!b.has(id)) return false;
      return true;
    };

    setHiddenIds((prev) => {
      const next = computeHidden(now);
      return isSameSet(prev, next) ? prev : next;
    });

    // Schedule the next re-evaluation for the soonest pending fade-out.
    let nextHide: number | null = null;
    for (const [, doneAt] of doneAtRef.current) {
      const remaining = FADE_MS - (now - doneAt);
      if (remaining <= 0) continue;
      nextHide = nextHide === null ? remaining : Math.min(nextHide, remaining);
    }
    if (nextHide === null) return;
    const timer = window.setTimeout(() => {
      const t = performance.now();
      setHiddenIds((prev) => {
        const next = computeHidden(t);
        return isSameSet(prev, next) ? prev : next;
      });
    }, nextHide + 16);
    return () => window.clearTimeout(timer);
  }, [ordered]);

  const visible = useMemo(
    () => ordered.filter((t) => !hiddenIds.has(t.id)).slice(-4),
    [ordered, hiddenIds],
  );

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
          <HudTaskRow key={task.id} task={task} />
        ))}
      </div>
    </div>
  );
}

function HudTaskRow({ task }: { task: Task }) {
  const variant = stateToVariant(task.state);
  const sub = formatTaskSub(task);
  const fillWidth = progressFillWidth(task.state);

  // Slice #0024 — `needs_attention` on `waiting_input` tasks gets a subtle
  // accent border so the user notices Jarvis is blocked on them.
  const needsAccent = task.needsAttention === true && task.state === "waiting_input";

  return (
    <div
      className={`hud-task is-${variant}${needsAccent ? " needs-attention" : ""}`}
      data-task-id={task.id}
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
