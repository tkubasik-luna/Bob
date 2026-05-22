import type { MouseEvent } from "react";
import type { Task, TaskState } from "../types/ws";

type Props = {
  task: Task;
  /** Click anywhere on the card body (outside action buttons) → opens the
   * drawer. Wired by `TaskSidebar` to the zustand `openTask` action. */
  onOpen?: (task: Task) => void;
  /** Slice #0024 — click on the secondary × button hides the card from the
   * sidebar (terminal states only). The handler is responsible for both the
   * WS dismiss event AND the in-memory drop via `dismissTask`. */
  onDismiss?: (task: Task) => void;
};

const STATE_DOT_CLASSES: Record<TaskState, string> = {
  pending: "bg-neutral-500",
  running: "bg-blue-500",
  waiting_input: "bg-yellow-400",
  done: "bg-green-500",
  failed: "bg-red-500",
};

const STATE_LABEL: Record<TaskState, string> = {
  pending: "En attente",
  running: "En cours",
  waiting_input: "Question en attente",
  done: "Terminée",
  failed: "Échec",
};

/**
 * One row in the sidebar. Minimalist by design: state dot + truncated title
 * + HH:MM timestamp. Click the card body to open the drawer (slice #0024).
 *
 * Terminal states (`done` / `failed`) show a secondary "hide" × button:
 * dismissing the card removes it from the sidebar without dropping the row
 * from SQLite (the row is only flagged `dismissed=true`). Pending tasks
 * (cap saturated, awaiting a free slot — slice #0020) carry a small
 * "En attente" sub-label so the user knows the task hasn't started yet.
 */
export function TaskCard({ task, onOpen, onDismiss }: Props) {
  const time = formatTime(task.createdAt);
  const isTerminal = task.state === "done" || task.state === "failed";

  const handleDismiss = (event: MouseEvent<HTMLButtonElement>) => {
    // Prevent the parent button's onClick from firing.
    event.stopPropagation();
    onDismiss?.(task);
  };

  return (
    <button
      type="button"
      onClick={() => onOpen?.(task)}
      className="group flex w-full items-center gap-3 rounded-md border border-neutral-800 bg-neutral-900 px-3 py-2 text-left transition-colors hover:border-neutral-700 hover:bg-neutral-800"
    >
      <span
        aria-label={STATE_LABEL[task.state]}
        title={STATE_LABEL[task.state]}
        className={`block h-2.5 w-2.5 flex-none rounded-full ${STATE_DOT_CLASSES[task.state]}`}
      />
      <span className="flex min-w-0 flex-1 flex-col">
        <span className="truncate text-sm text-neutral-100">{task.title}</span>
        {task.state === "pending" && (
          <span className="truncate text-xs text-neutral-500">En attente</span>
        )}
      </span>
      <span className="flex-none text-xs text-neutral-500 tabular-nums">{time}</span>
      {isTerminal && onDismiss && (
        <button
          type="button"
          onClick={handleDismiss}
          aria-label={`Masquer la tâche ${task.title}`}
          title="Masquer (sans supprimer l'historique)"
          className="flex h-6 w-6 flex-none items-center justify-center rounded text-neutral-500 opacity-0 transition-opacity hover:bg-neutral-700 hover:text-neutral-200 group-hover:opacity-100 focus:opacity-100"
        >
          <EyeOffIcon />
        </button>
      )}
    </button>
  );
}

/** Best-effort HH:MM extraction from the SQLite `datetime('now')` shape
 * (`YYYY-MM-DD HH:MM:SS`) or any ISO-8601 string. Falls back to the raw
 * value when parsing fails — better to show *something* than blank. */
function formatTime(raw: string): string {
  // SQLite returns `YYYY-MM-DD HH:MM:SS` without a `T`; normalise then parse.
  const isoish = raw.includes("T") ? raw : raw.replace(" ", "T");
  const date = new Date(`${isoish}Z`);
  if (Number.isNaN(date.getTime())) {
    return raw;
  }
  const hh = String(date.getHours()).padStart(2, "0");
  const mm = String(date.getMinutes()).padStart(2, "0");
  return `${hh}:${mm}`;
}

/** Eye-off icon (heroicons-ish) — visually distinct from the cancel × that
 * slice #0023 will add: "hide", not "stop". */
function EyeOffIcon() {
  return (
    <svg
      xmlns="http://www.w3.org/2000/svg"
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth="2"
      strokeLinecap="round"
      strokeLinejoin="round"
      className="h-3.5 w-3.5"
      aria-hidden="true"
    >
      <title>Masquer</title>
      <path d="M9.88 9.88a3 3 0 1 0 4.24 4.24" />
      <path d="M10.73 5.08A10.43 10.43 0 0 1 12 5c7 0 10 7 10 7a13.16 13.16 0 0 1-1.67 2.68" />
      <path d="M6.61 6.61A13.526 13.526 0 0 0 2 12s3 7 10 7a9.74 9.74 0 0 0 5.39-1.61" />
      <line x1="2" y1="2" x2="22" y2="22" />
    </svg>
  );
}
