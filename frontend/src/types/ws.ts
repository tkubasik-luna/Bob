/**
 * WebSocket contract V0 — manually derived from prd/0001-bob-mvp-foundation.md.
 * Keep in sync with backend `bob.ws_router`.
 */

export type ComponentDescriptor = {
  component: string;
  props: Record<string, unknown>;
};

// Client → server
export type UserMsg = {
  type: "user_msg";
  content: string;
  /** When true, the server will synthesize speech for the assistant reply. */
  voice?: boolean;
};

/** Slice #0024 — client tells the backend to hide a done/failed task from
 * future sidebar replays. The SQLite row is preserved (dismissed flag) so
 * the drawer can still render it if directly addressed. */
export type DismissTaskMsg = {
  type: "dismiss_task";
  task_id: string;
};

/** Slice #0024 — drawer-open: ask the backend for the full transcript of a
 * task. Backend replies with a single ``task_messages_snapshot``. Live
 * appends after open arrive via ``task_message`` push events. */
export type RequestTaskMessagesMsg = {
  type: "request_task_messages";
  task_id: string;
};

/** Slice #0023 — sidebar cancel button on a non-terminal card. Backend
 * routes this to `TaskScheduler.cancel(task_id, reason="user_cancelled")`
 * which interrupts the asyncio runner if any, then transitions the row
 * to `failed` with the reason persisted in `task.result`. The frontend
 * relies on the resulting `task_updated` + `task_result` events to
 * repopulate the card — no echo from this client→server event itself. */
export type CancelTaskMsg = {
  type: "cancel_task";
  task_id: string;
};

export type ClientMessage = UserMsg | DismissTaskMsg | RequestTaskMessagesMsg | CancelTaskMsg;

// Server → client
export type SessionMsg = {
  type: "session";
  session_id: string;
};

export type AssistantMsg = {
  type: "assistant_msg";
  /** Stable id for this assistant turn — used to correlate audio frames. */
  msg_id?: string;
  speech: string;
  ui: ComponentDescriptor[];
  /** Marks a message Bob pushed without a matching user prompt (slice #0021).
   * Set to `true` by `Orchestrator.generate_proactive_message` when a
   * sub-agent emits `ask_user` and Jarvis paraphrases the question for the
   * user. The frontend uses it to render a subtle visual cue distinguishing
   * the message from regular replies. Default `false` server-side. */
  proactive?: boolean;
};

export type ThinkingMsg = {
  type: "thinking";
  state: "start" | "end";
};

export type ErrorMsg = {
  type: "error";
  message: string;
  code?: string;
};

/** JSON header sent once per assistant turn just before the first PCM frame.
 *  Subsequent binary WS frames (ArrayBuffer) carry the raw s16le mono PCM
 *  at the announced `sample_rate`, and belong to this `msg_id` until the
 *  matching `audio_end`. */
export type AudioStartMsg = {
  type: "audio_start";
  msg_id: string;
  sample_rate: number;
};

export type AudioEndMsg = {
  type: "audio_end";
  msg_id: string;
};

/** Backend emits this once before the first chunk when the local Kokoro
 * model needs to be downloaded. The frontend shows a "Préparation de la
 * voix…" info toast that is dismissed on the matching `tts_ready` (or
 * `audio_error`) event. */
export type TtsPreparingMsg = {
  type: "tts_preparing";
  msg_id: string;
};

/** Paired with `tts_preparing` — model is now loaded and synthesis is
 * about to stream. Frontend dismisses the prep toast. */
export type TtsReadyMsg = {
  type: "tts_ready";
  msg_id: string;
};

/** Synthesis (or initial download) failed. Text response has already been
 * sent; the voice toggle stays ON so the next message retries. */
export type AudioErrorMsg = {
  type: "audio_error";
  msg_id: string;
  reason: string;
};

/** Lifecycle state of a sub-task in the sidebar. Mirrors
 * `bob.task_store.TaskState` on the backend. */
export type TaskState = "pending" | "running" | "waiting_input" | "done" | "failed";

/** A sub-task rendered in the right-hand sidebar. The frontend keeps a
 * `Record<string, Task>` so each WS event can upsert by `id`. */
export type Task = {
  id: string;
  title: string;
  goal: string;
  state: TaskState;
  needsAttention?: boolean;
  result?: string;
  createdAt: string;
  updatedAt?: string;
  /** Slice #0024 — the user has dismissed the card from the sidebar.
   * Defaults to `false` on the wire (backend filters dismissed=true out
   * of replay). The frontend simply drops the task from its map on
   * dismiss so the flag is rarely surfaced here. */
  dismissed?: boolean;
  /** Slice #0022 — latest intermediate status emitted by the sub-agent
   * via the `progress` action. Only meaningful while `state === "running"`;
   * the store clears it on any transition out of `running` so a stale
   * status never lingers under a `done` / `failed` card. */
  progressStatus?: string;
};

/** Slice #0024 — one row in a task's transcript, rendered inside the
 * drawer. Mirrors `bob.task_store.TaskMessage` on the backend. */
export type TaskMessage = {
  id: number;
  role: "system" | "user" | "assistant" | "tool";
  content: string;
  action: "done" | "ask_user" | "progress" | null;
  created_at: string;
};

/** Emitted on spawn (state=pending) and on WS connect for every known task
 * (state = current state). Frontend upserts unconditionally. */
export type TaskCreatedMsg = {
  type: "task_created";
  task_id: string;
  title: string;
  goal: string;
  state: TaskState;
  created_at: string;
  /** Set on tasks replayed at connect time so the frontend can distinguish
   * historical events if it ever needs to. Live events omit the flag. */
  replayed?: boolean;
};

/** Emitted on every state / attention transition past the initial spawn.
 *
 * Slice #0022 adds the optional `progress_status` field: the sub-agent
 * runner sets it on the `task_updated` event that follows a `progress`
 * action emit (state stays `running`). State-only transitions (e.g.
 * `pending → running`, `running → done`) omit the field. The store keeps
 * the latest non-null value on the task while it stays `running` and
 * clears it on any other transition. */
export type TaskUpdatedMsg = {
  type: "task_updated";
  task_id: string;
  state: TaskState;
  needs_attention?: boolean;
  updated_at: string;
  progress_status?: string;
  replayed?: boolean;
};

/** Emitted when a task gets its final result payload. Sent right after the
 * matching `task_updated` on the live path, and replayed at connect time
 * for tasks that already had a result persisted. */
export type TaskResultMsg = {
  type: "task_result";
  task_id: string;
  result: string;
  replayed?: boolean;
};

/** Slice #0024 — full transcript snapshot for a task. Emitted in response
 * to a client `request_task_messages` event when the drawer opens. */
export type TaskMessagesSnapshotMsg = {
  type: "task_messages_snapshot";
  task_id: string;
  messages: TaskMessage[];
};

/** Slice #0024 — live append: emitted by the sub-agent runner and the
 * orchestrator whenever they persist a row via `task_store.append_message`.
 * The drawer dedupes against the snapshot via `message_id`. */
export type TaskMessageMsg = {
  type: "task_message";
  task_id: string;
  message_id: number;
  role: "system" | "user" | "assistant" | "tool";
  content: string;
  action: "done" | "ask_user" | "progress" | null;
  created_at: string;
};

export type ServerMessage =
  | SessionMsg
  | AssistantMsg
  | ThinkingMsg
  | ErrorMsg
  | AudioStartMsg
  | AudioEndMsg
  | TtsPreparingMsg
  | TtsReadyMsg
  | AudioErrorMsg
  | TaskCreatedMsg
  | TaskUpdatedMsg
  | TaskResultMsg
  | TaskMessagesSnapshotMsg
  | TaskMessageMsg;

export type ConnectionStatus = "connecting" | "open" | "closed";

export type ChatMessage = {
  id: string;
  role: "user" | "assistant";
  content: string;
  ui?: ComponentDescriptor[];
  /** `true` for assistant bubbles Bob pushed without a user prompt (slice
   * #0021). Renders a subtle border accent in `Bubble`. */
  proactive?: boolean;
};
