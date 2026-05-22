import { useMemo } from "react";
import { useChatStore } from "../store/chatStore";
import type { Task } from "../types/ws";
import { TaskCard } from "./TaskCard";

/**
 * Right-hand panel showing every live sub-task. Reads `tasks` from the
 * shared zustand store — events come in via the WS handler in `ChatView`.
 *
 * Ordering: chronological (oldest first) so a new spawn slides under the
 * existing ones rather than re-ordering everything. Sticky header so the
 * title stays anchored while the list scrolls.
 */
export function TaskSidebar() {
  const tasks = useChatStore((s) => s.tasks);
  const ordered = useMemo<Task[]>(
    () => Object.values(tasks).sort((a, b) => a.createdAt.localeCompare(b.createdAt)),
    [tasks],
  );

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
                <TaskCard task={task} />
              </li>
            ))}
          </ul>
        )}
      </div>
    </aside>
  );
}
