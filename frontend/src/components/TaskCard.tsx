import type { Task, TaskState } from "../types/ws";

type Props = {
  task: Task;
  /** Click handler — stubbed for slice #0019; the drawer lands in #0024. */
  onClick?: (task: Task) => void;
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
 * + HH:MM timestamp. The whole card is the click target so the future drawer
 * (#0024) can be wired without restructuring the markup.
 */
export function TaskCard({ task, onClick }: Props) {
  const time = formatTime(task.createdAt);
  return (
    <button
      type="button"
      onClick={() => onClick?.(task)}
      className="group flex w-full items-center gap-3 rounded-md border border-neutral-800 bg-neutral-900 px-3 py-2 text-left transition-colors hover:border-neutral-700 hover:bg-neutral-800"
    >
      <span
        aria-label={STATE_LABEL[task.state]}
        title={STATE_LABEL[task.state]}
        className={`block h-2.5 w-2.5 flex-none rounded-full ${STATE_DOT_CLASSES[task.state]}`}
      />
      <span className="min-w-0 flex-1 truncate text-sm text-neutral-100">{task.title}</span>
      <span className="flex-none text-xs text-neutral-500 tabular-nums">{time}</span>
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
