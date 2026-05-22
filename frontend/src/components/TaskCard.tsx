import { type MouseEvent, useState } from "react";
import type { Task, TaskState } from "../types/ws";

type Props = {
  task: Task;
  /** Click anywhere on the card body (outside action buttons) → opens the
   * drawer. Wired by `TaskSidebar` to the zustand `openTask` action. */
  onOpen?: (task: Task) => void;
  /** Slice #0024 — click on the "hide" eye-off button hides the card from
   * the sidebar (terminal states only). The handler is responsible for
   * both the WS dismiss event AND the in-memory drop via `dismissTask`. */
  onDismiss?: (task: Task) => void;
  /** Slice #0023 — click on the cancel × button on a non-terminal card
   * dispatches a `cancel_task` WS event upstream. The card optimistically
   * dims while we wait for the backend `task_updated(failed)` to land. */
  onCancel?: (task: Task) => void;
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
 * Two action buttons per card, mutually exclusive depending on state:
 *
 * - Non-terminal (`pending` / `running` / `waiting_input`): a cancel × at the
 *   top-right (slice #0023) — clicking it fires a `cancel_task` WS event.
 *   The card is dimmed locally until the backend's `task_updated(failed)`
 *   lands and the eye-off button replaces the ×.
 * - Terminal (`done` / `failed`): the slice #0024 eye-off "hide" button.
 *   Dismissing the card removes it from the sidebar without dropping the
 *   row from SQLite (the row is only flagged `dismissed=true`).
 *
 * Pending tasks (cap saturated, awaiting a free slot — slice #0020) carry
 * a small "En attente" sub-label so the user knows the task hasn't started
 * yet. Failed cards (notably user-cancelled) surface the reason as a
 * `title=` tooltip so the user can hover to remember why it stopped.
 */
export function TaskCard({ task, onOpen, onDismiss, onCancel }: Props) {
  const time = formatTime(task.createdAt);
  const isTerminal = task.state === "done" || task.state === "failed";
  // Local optimistic flag: once the user clicks ×, dim the card and disable
  // further clicks. The backend ack arrives via `task_updated` and flips
  // `task.state` to `failed`, at which point `isTerminal` becomes true and
  // the cancel button vanishes — `optimisticCancel` then loses meaning.
  const [optimisticCancel, setOptimisticCancel] = useState(false);

  const handleDismiss = (event: MouseEvent<HTMLButtonElement>) => {
    event.stopPropagation();
    onDismiss?.(task);
  };

  const handleCancel = (event: MouseEvent<HTMLButtonElement>) => {
    event.stopPropagation();
    if (optimisticCancel) return;
    setOptimisticCancel(true);
    onCancel?.(task);
  };

  const failureTooltip =
    task.state === "failed" && task.result
      ? task.result === "user_cancelled"
        ? "Annulée par l'utilisateur"
        : `Échec : ${task.result}`
      : undefined;

  return (
    <button
      type="button"
      onClick={() => onOpen?.(task)}
      disabled={optimisticCancel}
      title={failureTooltip}
      className={`group flex w-full items-center gap-3 rounded-md border border-neutral-800 bg-neutral-900 px-3 py-2 text-left transition-colors hover:border-neutral-700 hover:bg-neutral-800 ${
        optimisticCancel ? "cursor-wait opacity-60" : ""
      }`}
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
        {/* Slice #0022 — live progress status from the sub-agent. Only
            rendered while the task is actually running; the store clears
            the field on any other transition so we never show a stale
            line under a `done` / `failed` card. */}
        {task.state === "running" && task.progressStatus && (
          <span className="truncate text-xs text-zinc-500 italic">{task.progressStatus}</span>
        )}
      </span>
      <span className="flex-none text-xs text-neutral-500 tabular-nums">{time}</span>
      {!isTerminal && onCancel && (
        <button
          type="button"
          onClick={handleCancel}
          disabled={optimisticCancel}
          aria-label={`Annuler la tâche ${task.title}`}
          title="Annuler la tâche"
          className="flex h-6 w-6 flex-none items-center justify-center rounded text-neutral-500 opacity-0 transition-opacity hover:bg-red-700/40 hover:text-red-200 group-hover:opacity-100 focus:opacity-100 disabled:cursor-wait disabled:opacity-50"
        >
          <CancelIcon />
        </button>
      )}
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

/** Eye-off icon (heroicons-ish) — visually distinct from the cancel × so
 * the user can read "hide" vs "stop" at a glance. Used on terminal cards
 * (slice #0024). */
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

/** Cancel "×" icon for non-terminal cards (slice #0023). Stroke-only so it
 * stays crisp at small sizes and pairs visually with the eye-off icon
 * without looking like the same control. */
function CancelIcon() {
  return (
    <svg
      xmlns="http://www.w3.org/2000/svg"
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth="2.5"
      strokeLinecap="round"
      strokeLinejoin="round"
      className="h-3.5 w-3.5"
      aria-hidden="true"
    >
      <title>Annuler</title>
      <line x1="6" y1="6" x2="18" y2="18" />
      <line x1="18" y1="6" x2="6" y2="18" />
    </svg>
  );
}
