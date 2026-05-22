import { useEffect } from "react";
import { useChatStore } from "../store/chatStore";
import type { ClientMessage, Task, TaskMessage, TaskState } from "../types/ws";

type Props = {
  /** Send a WS event upstream — `ChatView` plumbs this from `useWebSocket`. */
  onSend: (msg: ClientMessage) => void;
};

const STATE_LABEL: Record<TaskState, string> = {
  pending: "En attente",
  running: "En cours",
  waiting_input: "Question en attente",
  done: "Terminée",
  failed: "Échec",
};

const STATE_BADGE_CLASSES: Record<TaskState, string> = {
  pending: "bg-neutral-700 text-neutral-200",
  running: "bg-blue-600/30 text-blue-200",
  waiting_input: "bg-yellow-500/30 text-yellow-100",
  done: "bg-green-600/30 text-green-200",
  failed: "bg-red-600/30 text-red-200",
};

/**
 * Slice #0024 — slide-in panel that surfaces a single sub-task in detail.
 * Renders goal, full transcript (task_messages), and the final result /
 * failure reason. Reads from the zustand store so any live `task_message`
 * or `task_updated` event reactively refreshes the panel while it's open.
 *
 * Layout: full-height fixed overlay anchored to the right edge. Width is
 * half-screen on `md+` viewports and full-width on mobile, with a dimmed
 * backdrop that closes the drawer on click. The close button is also
 * available in the top-right corner.
 *
 * Transcript fetch: when the drawer opens we send a
 * `request_task_messages` WS event and the backend replies with a
 * `task_messages_snapshot` that hydrates the cache. Live appends after
 * open arrive via `task_message` events and dedupe by message id.
 */
export function TaskDrawer({ onSend }: Props) {
  const openTaskId = useChatStore((s) => s.openTaskId);
  const task = useChatStore((s) => (s.openTaskId ? s.tasks[s.openTaskId] : null));
  const messages = useChatStore((s) =>
    s.openTaskId ? (s.taskMessages[s.openTaskId] ?? null) : null,
  );
  const openTask = useChatStore((s) => s.openTask);

  // Fetch the transcript snapshot every time the drawer opens on a new id.
  // The store dedupes by message_id when live events arrive in parallel.
  useEffect(() => {
    if (!openTaskId) return;
    onSend({ type: "request_task_messages", task_id: openTaskId });
  }, [openTaskId, onSend]);

  // ESC closes the drawer.
  useEffect(() => {
    if (!openTaskId) return;
    const onKey = (event: KeyboardEvent) => {
      if (event.key === "Escape") openTask(null);
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [openTaskId, openTask]);

  if (!openTaskId) return null;

  const handleBackdropClick = () => openTask(null);
  const handleClose = () => openTask(null);

  // If the task is not in the in-memory map (e.g. dismissed elsewhere), we
  // still render the drawer shell with whatever we have. In practice
  // `dismissTask` closes the drawer automatically, so this is rare.
  return (
    <dialog
      open
      aria-modal="true"
      className="fixed inset-0 z-30 m-0 flex h-full max-h-none w-full max-w-none bg-transparent p-0"
    >
      <button
        type="button"
        aria-label="Fermer le panneau"
        onClick={handleBackdropClick}
        className="flex-1 cursor-default bg-black/50"
      />
      <aside className="flex h-full w-full flex-col border-l border-neutral-800 bg-neutral-950 shadow-xl md:w-1/2">
        <DrawerHeader task={task} onClose={handleClose} />
        <div className="flex-1 overflow-y-auto px-5 py-4">
          {task ? (
            <DrawerBody task={task} messages={messages} />
          ) : (
            <p className="text-sm text-neutral-400">
              Tâche introuvable. Elle a peut-être été masquée.
            </p>
          )}
        </div>
      </aside>
    </dialog>
  );
}

function DrawerHeader({ task, onClose }: { task: Task | null; onClose: () => void }) {
  return (
    <header className="flex items-start justify-between gap-3 border-b border-neutral-800 px-5 py-4">
      <div className="flex min-w-0 flex-1 flex-col gap-1">
        <h2 className="truncate text-lg font-semibold text-neutral-100">
          {task?.title ?? "Tâche"}
        </h2>
        {task && (
          <span
            className={`inline-flex w-fit items-center rounded-full px-2 py-0.5 text-xs ${STATE_BADGE_CLASSES[task.state]}`}
          >
            {STATE_LABEL[task.state]}
          </span>
        )}
      </div>
      <button
        type="button"
        onClick={onClose}
        aria-label="Fermer"
        className="flex h-8 w-8 flex-none items-center justify-center rounded-md border border-neutral-800 bg-neutral-900 text-neutral-300 transition-colors hover:bg-neutral-800"
      >
        <CloseIcon />
      </button>
    </header>
  );
}

function DrawerBody({ task, messages }: { task: Task; messages: TaskMessage[] | null }) {
  return (
    <div className="flex flex-col gap-5">
      <Section title="Objectif">
        <p className="whitespace-pre-wrap text-sm text-neutral-200">{task.goal}</p>
      </Section>

      {task.result && (
        <Section title={task.state === "failed" ? "Raison" : "Résultat"}>
          <p className="whitespace-pre-wrap text-sm text-neutral-200">{task.result}</p>
        </Section>
      )}

      <Section title="Historique">
        {messages === null ? (
          <p className="text-xs text-neutral-500">Chargement…</p>
        ) : messages.length === 0 ? (
          <p className="text-xs text-neutral-500">Aucun message pour cette tâche.</p>
        ) : (
          <ul className="flex flex-col gap-3">
            {messages.map((msg) => (
              <li key={msg.id}>
                <TranscriptRow message={msg} />
              </li>
            ))}
          </ul>
        )}
      </Section>
    </div>
  );
}

function Section({ title, children }: { title: string; children: React.ReactNode }) {
  return (
    <section className="flex flex-col gap-2">
      <h3 className="text-xs font-semibold uppercase tracking-wide text-neutral-500">{title}</h3>
      {children}
    </section>
  );
}

const ROLE_LABEL: Record<TaskMessage["role"], string> = {
  user: "Utilisateur",
  assistant: "Sub-agent",
  system: "Système",
  tool: "Outil",
};

const ACTION_BADGE_CLASSES: Record<NonNullable<TaskMessage["action"]>, string> = {
  done: "bg-green-600/30 text-green-200",
  ask_user: "bg-yellow-500/30 text-yellow-100",
  progress: "bg-neutral-700 text-neutral-300",
};

function TranscriptRow({ message }: { message: TaskMessage }) {
  return (
    <div className="flex flex-col gap-1 rounded-md border border-neutral-800 bg-neutral-900 px-3 py-2">
      <div className="flex items-center gap-2 text-[11px] text-neutral-500">
        <span className="font-medium uppercase tracking-wide">{ROLE_LABEL[message.role]}</span>
        {message.action && (
          <span
            className={`inline-flex items-center rounded px-1.5 py-0.5 text-[10px] font-medium ${ACTION_BADGE_CLASSES[message.action]}`}
          >
            {message.action}
          </span>
        )}
        <span className="ml-auto tabular-nums">{formatTimestamp(message.created_at)}</span>
      </div>
      <p className="whitespace-pre-wrap text-sm text-neutral-200">{message.content}</p>
    </div>
  );
}

function formatTimestamp(raw: string): string {
  const isoish = raw.includes("T") ? raw : raw.replace(" ", "T");
  const date = new Date(`${isoish}Z`);
  if (Number.isNaN(date.getTime())) return raw;
  const hh = String(date.getHours()).padStart(2, "0");
  const mm = String(date.getMinutes()).padStart(2, "0");
  const ss = String(date.getSeconds()).padStart(2, "0");
  return `${hh}:${mm}:${ss}`;
}

function CloseIcon() {
  return (
    <svg
      xmlns="http://www.w3.org/2000/svg"
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth="2"
      strokeLinecap="round"
      strokeLinejoin="round"
      className="h-4 w-4"
      aria-hidden="true"
    >
      <title>Fermer</title>
      <line x1="18" y1="6" x2="6" y2="18" />
      <line x1="6" y1="6" x2="18" y2="18" />
    </svg>
  );
}
