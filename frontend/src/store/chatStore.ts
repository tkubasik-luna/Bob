import { create } from "zustand";
import type {
  ChatMessage,
  ComponentDescriptor,
  ConnectionStatus,
  Task,
  TaskCreatedMsg,
  TaskMessage,
  TaskMessageMsg,
  TaskMessagesSnapshotMsg,
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

/** PRD 0006 / issue 0049 — in-flight streamed assistant turn.
 *
 *  Populated incrementally by `speech_delta` frames between user submit and
 *  the closing `assistant_msg`. The `speech` field grows on every delta and
 *  the optional `ui` is filled by the single `ui_payload` frame. Cleared on
 *  the closing `assistant_msg` (which spawns the persisted ChatMessage) or
 *  on a fresh user turn (an interruption resets it before the new stream
 *  starts).
 *
 *  Consumers:
 *  - `TranscriptLine` renders `speech` progressively so the sphere shows
 *    each word as it streams from the LLM.
 *  - `SphereUI` opens the `MarkdownOverlay` immediately when `ui` lands,
 *    without waiting for the final `assistant_msg`. */
export type StreamingAssistant = {
  msgId: string;
  speech: string;
  ui: ComponentDescriptor | null;
};

/** PRD 0016 / issue 0099 — the live STT hypothesis for the in-flight voice
 * turn. Populated by `stt_partial` frames as the user speaks and settled by
 * `stt_final`; cleared when Bob's reply lands. `stablePrefixLen` is the count
 * of leading characters the engine considers settled (render solid; the tail
 * is tentative). Without this slice the STT events were silently dropped and
 * the user saw nothing while speaking — the "voice module doesn't work" gap. */
export type LiveUserTranscript = {
  turnId: string;
  text: string;
  stablePrefixLen: number;
  /** True once `stt_final` froze the turn (the say-path now owns the text). */
  final: boolean;
};

type ChatState = {
  messages: ChatMessage[];
  connectionStatus: ConnectionStatus;
  isWaitingResponse: boolean;
  sessionId: string | null;
  toasts: Toast[];
  /** msg_id of the assistant bubble currently being voiced by audioPlayer,
   * or null when nothing is playing. Driven by `subscribeSpeaking`. */
  speakingMsgId: string | null;
  /** PRD 0006 / issue 0049 — live state of the streaming Jarvis turn, or
   * `null` when no turn is in flight. */
  streamingAssistant: StreamingAssistant | null;
  /** PRD 0016 / issue 0099 — live STT hypothesis for the in-flight voice turn,
   * or `null` when the user isn't speaking / the turn settled into a reply. */
  liveUserTranscript: LiveUserTranscript | null;
  /** Sub-tasks driven by `task_*` WS events (slice #0019). Keyed by id so
   * each event is an idempotent upsert. */
  tasks: Record<string, Task>;
  /** Slice #0024 — per-task transcript cache. Populated by snapshot replies
   * and merged on every `task_message` live event (dedupe by `id`). Lives
   * on the store so the drawer can read it reactively. */
  taskMessages: Record<string, TaskMessage[]>;
  /** Slice #0024 — id of the task whose drawer is currently open, or `null`
   * when the drawer is closed. */
  openTaskId: string | null;
  addUserMessage: (content: string) => void;
  /** Add an assistant message. When `msgId` is provided (server-issued
   * id from the `assistant_msg` frame) it is used as the React key AND as
   * the correlation id for audio playback / `speakingMsgId`. Falls back to
   * a generated id for older code paths / tests. */
  addAssistantMessage: (
    content: string,
    ui?: ComponentDescriptor[],
    msgId?: string,
    proactive?: boolean,
  ) => void;
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
  /** Slice #0024 — drop the task from the in-memory map (sidebar hide).
   * Backend persistence is the WS event the caller fires alongside this. */
  dismissTask: (taskId: string) => void;
  /** Slice #0024 — open the drawer on a specific task, or close it (`null`). */
  openTask: (taskId: string | null) => void;
  /** Slice #0024 — replace the cached transcript for a task with the
   * server-provided snapshot. */
  setTaskMessagesSnapshot: (msg: TaskMessagesSnapshotMsg) => void;
  /** Slice #0024 — append a live message to a task's cached transcript.
   * Dedupes by `message_id` so re-running the snapshot fetch is safe. */
  appendTaskMessage: (msg: TaskMessageMsg) => void;
  /** PRD 0006 / issue 0049 — append the latest `speech_delta` suffix into
   *  the in-flight streaming buffer (creating one when `msgId` changes). */
  appendSpeechDelta: (msgId: string, delta: string) => void;
  /** PRD 0006 / issue 0049 — record the `ui_payload` for the in-flight turn.
   *  No-op when the active stream's `msg_id` doesn't match (stale frame). */
  setStreamingUi: (msgId: string, ui: ComponentDescriptor) => void;
  /** PRD 0006 / issue 0049 — drop the in-flight streaming state. Called when
   *  the closing `assistant_msg` lands (so the persisted bubble takes over)
   *  AND on user submit (interrupts any leftover stream). */
  clearStreamingAssistant: () => void;
  /** PRD 0016 / issue 0099 — record the latest STT hypothesis for the voice
   *  turn. `final` settles the turn (frozen transcript). */
  setLiveTranscript: (
    turnId: string,
    text: string,
    stablePrefixLen: number,
    final?: boolean,
  ) => void;
  /** PRD 0016 / issue 0099 — drop the live transcript (Bob replied / aborted). */
  clearLiveTranscript: () => void;
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
  streamingAssistant: null,
  liveUserTranscript: null,
  tasks: {},
  taskMessages: {},
  openTaskId: null,
  addUserMessage: (content) =>
    set((state) => ({
      messages: [...state.messages, { id: randomId(), role: "user", content }],
    })),
  addAssistantMessage: (content, ui, msgId, proactive) =>
    set((state) => ({
      messages: [
        ...state.messages,
        {
          id: msgId ?? randomId(),
          role: "assistant",
          content,
          ui,
          ...(proactive ? { proactive: true } : {}),
        },
      ],
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
      // Slice #0022 — progress_status only lives on `running` tasks. If
      // the event carries one (progress emit), store it; on any non-running
      // transition (done / failed / waiting_input) clear the field so a
      // stale status never lingers under a terminal card.
      if (msg.state === "running") {
        if (msg.progress_status !== undefined) {
          next.progressStatus = msg.progress_status;
        }
      } else {
        next.progressStatus = undefined;
      }
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
              // PRD 0008 / issue 0064 — keep the structured deliverable
              // descriptors so the task-result effect can rebuild the matching
              // sections (Mail → MailCard in SectionsOverlay) instead of
              // wrapping as Markdown.
              resultPayload: msg.result_payload,
            },
          },
        };
      }
      return {
        tasks: {
          ...state.tasks,
          [msg.task_id]: {
            ...existing,
            result: msg.result,
            // PRD 0008 / issue 0064 — carry the structured deliverable.
            resultPayload: msg.result_payload,
          },
        },
      };
    }),
  dismissTask: (taskId) =>
    set((state) => {
      // Slice #0024 — hide the card from the sidebar without losing the
      // backend row. The drawer auto-closes if it was showing this task,
      // and we keep the cached transcript around in case the user wants
      // to inspect it again later (the backend row survives).
      if (!(taskId in state.tasks) && state.openTaskId !== taskId) return state;
      const { [taskId]: _removed, ...rest } = state.tasks;
      return {
        tasks: rest,
        openTaskId: state.openTaskId === taskId ? null : state.openTaskId,
      };
    }),
  openTask: (openTaskId) => set({ openTaskId }),
  setTaskMessagesSnapshot: (msg) =>
    set((state) => ({
      taskMessages: { ...state.taskMessages, [msg.task_id]: msg.messages },
    })),
  appendTaskMessage: (msg) =>
    set((state) => {
      const existing = state.taskMessages[msg.task_id] ?? [];
      // Dedupe by `id` so a snapshot + live message round-trip is idempotent.
      if (existing.some((m) => m.id === msg.message_id)) return state;
      const next: TaskMessage = {
        id: msg.message_id,
        role: msg.role,
        content: msg.content,
        action: msg.action,
        created_at: msg.created_at,
      };
      return {
        taskMessages: {
          ...state.taskMessages,
          [msg.task_id]: [...existing, next],
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
  appendSpeechDelta: (msgId, delta) =>
    set((state) => {
      // A new msg_id (or no in-flight stream yet) starts a fresh buffer.
      // The previous one is implicitly discarded — the closing
      // `assistant_msg` already turned it into a persisted bubble; if it
      // hadn't, a retry path on the backend rolled a new id (see
      // bob.orchestrator._run_jarvis_turn_with_retry) and the previous
      // attempt's spoken bytes were a false start.
      const current = state.streamingAssistant;
      if (current === null || current.msgId !== msgId) {
        return { streamingAssistant: { msgId, speech: delta, ui: null } };
      }
      return {
        streamingAssistant: { ...current, speech: current.speech + delta },
      };
    }),
  setStreamingUi: (msgId, ui) =>
    set((state) => {
      const current = state.streamingAssistant;
      if (current === null) {
        // ui_payload before any speech_delta — defensive: open the buffer
        // with an empty speech string so the overlay still surfaces. In
        // practice the backend always emits at least one speech_delta
        // before the ui_payload (StreamEmitter.finalize runs after the
        // last `feed`).
        return { streamingAssistant: { msgId, speech: "", ui } };
      }
      if (current.msgId !== msgId) {
        // Stale frame from a previous turn — drop it.
        return state;
      }
      return { streamingAssistant: { ...current, ui } };
    }),
  clearStreamingAssistant: () => set({ streamingAssistant: null }),
  setLiveTranscript: (turnId, text, stablePrefixLen, final = false) =>
    set({ liveUserTranscript: { turnId, text, stablePrefixLen, final } }),
  clearLiveTranscript: () => set({ liveUserTranscript: null }),
}));
