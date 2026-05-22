import { create } from "zustand";
import type {
  ChatMessage,
  ComponentDescriptor,
  ConnectionStatus,
  Task,
  TaskCreatedMsg,
  TaskResultMsg,
  TaskUpdatedMsg,
} from "../types/ws";

export type ToastKind = "error" | "info";

export type Toast = {
  id: string;
  message: string;
  code?: string;
  kind: ToastKind;
  createdAt: number;
};

export type PushToastOptions = {
  code?: string;
  kind?: ToastKind;
  /** If provided, the toast uses this id (idempotent push) and is NOT
   * auto-dismissed — it must be removed explicitly via `dismissToast`. */
  id?: string;
};

const TOAST_AUTO_DISMISS_MS = 5_000;

type ChatState = {
  messages: ChatMessage[];
  connectionStatus: ConnectionStatus;
  isWaitingResponse: boolean;
  sessionId: string | null;
  toasts: Toast[];
  /** msg_id of the assistant bubble currently being voiced by audioPlayer,
   * or null when nothing is playing. Driven by `subscribeSpeaking`. */
  speakingMsgId: string | null;
  /** Sub-tasks driven by `task_*` WS events (slice #0019). Keyed by id so
   * each event is an idempotent upsert. */
  tasks: Record<string, Task>;
  addUserMessage: (content: string) => void;
  /** Add an assistant message. When `msgId` is provided (server-issued
   * id from the `assistant_msg` frame) it is used as the React key AND as
   * the correlation id for audio playback / `speakingMsgId`. Falls back to
   * a generated id for older code paths / tests. */
  addAssistantMessage: (content: string, ui?: ComponentDescriptor[], msgId?: string) => void;
  setStatus: (status: ConnectionStatus) => void;
  setWaiting: (waiting: boolean) => void;
  setSessionId: (id: string | null) => void;
  setSpeakingMsgId: (msgId: string | null) => void;
  pushToast: (message: string, codeOrOptions?: string | PushToastOptions) => string;
  dismissToast: (id: string) => void;
  /** Insert a freshly-spawned task (or refresh one already present at
   * reconnect). Live events arrive with `state=pending`; replayed events
   * carry the current state. */
  upsertTaskCreated: (msg: TaskCreatedMsg) => void;
  /** Merge state / attention / updatedAt onto an existing task; create the
   * task on the fly when the event lands before the matching `task_created`
   * (defensive against races / replay reordering). */
  upsertTaskUpdated: (msg: TaskUpdatedMsg) => void;
  /** Persist the final result payload on a task; never changes state. */
  setTaskResult: (msg: TaskResultMsg) => void;
};

function randomId(): string {
  // Prefer crypto.randomUUID where available; fall back to a simple random string.
  if (typeof crypto !== "undefined" && typeof crypto.randomUUID === "function") {
    return crypto.randomUUID();
  }
  return `${Date.now()}-${Math.random().toString(36).slice(2, 10)}`;
}

export const useChatStore = create<ChatState>((set, get) => ({
  messages: [],
  connectionStatus: "connecting",
  isWaitingResponse: false,
  sessionId: null,
  toasts: [],
  speakingMsgId: null,
  tasks: {},
  addUserMessage: (content) =>
    set((state) => ({
      messages: [...state.messages, { id: randomId(), role: "user", content }],
    })),
  addAssistantMessage: (content, ui, msgId) =>
    set((state) => ({
      messages: [...state.messages, { id: msgId ?? randomId(), role: "assistant", content, ui }],
    })),
  setStatus: (connectionStatus) => set({ connectionStatus }),
  setWaiting: (isWaitingResponse) => set({ isWaitingResponse }),
  setSessionId: (sessionId) => set({ sessionId }),
  setSpeakingMsgId: (speakingMsgId) => set({ speakingMsgId }),
  upsertTaskCreated: (msg) =>
    set((state) => {
      const existing = state.tasks[msg.task_id];
      const next: Task = {
        // Preserve any prior fields (result, updatedAt) so a late-arriving
        // task_created (e.g. on reconnect) never erases progress.
        ...(existing ?? {}),
        id: msg.task_id,
        title: msg.title,
        goal: msg.goal,
        state: msg.state,
        createdAt: msg.created_at,
      };
      return { tasks: { ...state.tasks, [msg.task_id]: next } };
    }),
  upsertTaskUpdated: (msg) =>
    set((state) => {
      const existing = state.tasks[msg.task_id];
      const base: Task = existing ?? {
        id: msg.task_id,
        title: msg.task_id,
        goal: "",
        state: msg.state,
        createdAt: msg.updated_at,
      };
      const next: Task = {
        ...base,
        state: msg.state,
        updatedAt: msg.updated_at,
        ...(msg.needs_attention !== undefined ? { needsAttention: msg.needs_attention } : {}),
      };
      return { tasks: { ...state.tasks, [msg.task_id]: next } };
    }),
  setTaskResult: (msg) =>
    set((state) => {
      const existing = state.tasks[msg.task_id];
      if (!existing) {
        // task_result before task_created: keep the result so the next
        // task_created upsert preserves it via the spread.
        return {
          tasks: {
            ...state.tasks,
            [msg.task_id]: {
              id: msg.task_id,
              title: msg.task_id,
              goal: "",
              state: "done",
              createdAt: new Date().toISOString(),
              result: msg.result,
            },
          },
        };
      }
      return {
        tasks: {
          ...state.tasks,
          [msg.task_id]: { ...existing, result: msg.result },
        },
      };
    }),
  pushToast: (message, codeOrOptions) => {
    const opts: PushToastOptions =
      typeof codeOrOptions === "string" ? { code: codeOrOptions } : (codeOrOptions ?? {});
    const sticky = opts.id !== undefined;
    const id = opts.id ?? randomId();
    const kind: ToastKind = opts.kind ?? "error";
    set((state) => {
      // Idempotent push: if a toast with this explicit id already exists,
      // keep the existing one rather than duplicating (e.g. successive
      // `tts_preparing` events for the same msg_id).
      if (sticky && state.toasts.some((t) => t.id === id)) return state;
      return {
        toasts: [...state.toasts, { id, message, code: opts.code, kind, createdAt: Date.now() }],
      };
    });
    if (!sticky) {
      setTimeout(() => {
        get().dismissToast(id);
      }, TOAST_AUTO_DISMISS_MS);
    }
    return id;
  },
  dismissToast: (id) =>
    set((state) => ({
      toasts: state.toasts.filter((t) => t.id !== id),
    })),
}));
