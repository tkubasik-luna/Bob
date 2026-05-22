import { useMemo } from "react";
import { useChatStore } from "../store/chatStore";
import type { ClientMessage, Task } from "../types/ws";
import { TaskCard } from "./TaskCard";

type Props = {
  /** Send a WS event upstream — `ChatView` plumbs this from `useWebSocket`. */
  onSend: (msg: ClientMessage) => void;
};

/**
 * Right-hand panel showing every live sub-task. Reads `tasks` from the
 * shared zustand store — events come in via the WS handler in `ChatView`.
 *
 * Ordering: chronological (oldest first) so a new spawn slides under the
 * existing ones rather than re-ordering everything. Sticky header so the
 * title stays anchored while the list scrolls.
 *
 * Three actions per card:
 *  - Click body → `openTask(id)` opens the drawer on this task.
 *  - Click × on non-terminal cards (slice #0023) → `cancel_task` WS event.
 *    The backend interrupts the runner and emits `task_updated(failed)` +
 *    `task_result(reason)`; the card repopulates from those events.
 *  - Click "hide" on terminal cards (slice #0024) → `dismiss_task` WS
 *    event + in-memory drop.
 */
export function TaskSidebar({ onSend }: Props) {
  const tasks = useChatStore((s) => s.tasks);
  const openTask = useChatStore((s) => s.openTask);
  const dismissTask = useChatStore((s) => s.dismissTask);

  const ordered = useMemo<Task[]>(
    () => Object.values(tasks).sort((a, b) => a.createdAt.localeCompare(b.createdAt)),
    [tasks],
  );

  const handleDismiss = (task: Task) => {
    // Optimistic update: drop locally first so the UI is snappy, then tell
    // the backend to persist the flag. A no-op when the WS is closed —
    // useWebSocket queues the event for the next reconnect.
    dismissTask(task.id);
    onSend({ type: "dismiss_task", task_id: task.id });
  };

  const handleCancel = (task: Task) => {
    // No optimistic store mutation here — the card dims itself locally
    // (`optimisticCancel` inside TaskCard) and we wait for the backend's
    // `task_updated(failed)` to flip the persistent state. Keeping the row
    // until then preserves the title/goal/transcript while the runner
    // unwinds; an immediate drop would yank the card mid-action.
    onSend({ type: "cancel_task", task_id: task.id });
  };

  return (
    <aside className="flex h-full w-[320px] flex-none flex-col border-l border-neutral-800 bg-neutral-950">
      <header className="sticky top-0 z-10 border-b border-neutral-800 bg-neutral-950 px-4 py-3">
        <h2 className="text-sm font-semibold tracking-tight text-neutral-200">Tâches en cours</h2>
      </header>
      <div className="flex-1 overflow-y-auto px-3 py-3">
        {ordered.length === 0 ? (
          <p className="px-1 py-2 text-xs text-neutral-500">Aucune tâche en cours</p>
        ) : (
          <ul className="flex flex-col gap-2">
            {ordered.map((task) => (
              <li key={task.id}>
                <TaskCard
                  task={task}
                  onOpen={(t) => openTask(t.id)}
                  onDismiss={handleDismiss}
                  onCancel={handleCancel}
                />
              </li>
            ))}
          </ul>
        )}
      </div>
    </aside>
  );
}
