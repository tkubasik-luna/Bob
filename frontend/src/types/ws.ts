/**
 * WebSocket contract V0 — manually derived from prd/0001-bob-mvp-foundation.md.
 * Keep in sync with backend `bob.ws_router`.
 */

/** Props for the `Markdown` UI component — single rich-text content slot.
 * Mirrors `backend/src/bob/ui_registry.py::MARKDOWN`. */
export type MarkdownProps = {
  content: string;
};

/** Props for the `Mail` UI component — a single Gmail message rendered as
 * an overlay card. Mirrors `backend/src/bob/ui_registry.py::MAIL`. Fields
 * marked optional in the schema (`from.role`, `flags`, `attachments`) are
 * optional here too; the rest are required. */
export type MailProps = {
  from: {
    name: string;
    email: string;
    /** Optional human-readable role / org affiliation. */
    role?: string;
  };
  /** ISO 8601 timestamp (e.g. `2026-05-28T14:22:00Z`). */
  receivedAt: string;
  subject: string;
  /** Gmail-style snippet of the message body. */
  bodyPreview: string;
  /** Visual flag pills rendered in the header (`PRIORITY`, `UNREAD`, …).
   * Defaults to an empty array on the wire. */
  flags?: ("priority" | "unread" | "starred")[];
  /** Attachment chips rendered under the body. Defaults to empty. */
  attachments?: {
    name: string;
    sizeBytes: number;
    mime: string;
  }[];
  threadId: string;
  messageId: string;
  /** Full Gmail web URL the OPEN button browses to. */
  gmailWebUrl: string;
};

/** Discriminated union of the components the LLM can emit. The
 * `component` field selects the variant; `props` is typed accordingly so
 * `SphereUI`'s dispatcher gets exhaustiveness checks for free.
 *
 * The catch-all `{ component: string; props: Record<string, unknown> }`
 * branch keeps us forward-compatible with components added on the backend
 * but not yet known to the frontend — they fall through the dispatcher
 * and render nothing (no runtime crash). */
export type ComponentDescriptor =
  | { component: "Markdown"; props: MarkdownProps }
  | { component: "Mail"; props: MailProps }
  | { component: string; props: Record<string, unknown> };

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

/** Slice #0025 — heartbeat indicating whether the user is currently
 * composing a message. Frontend debounces keystrokes (500 ms) and sends
 * `true` on first keystroke + `false` on inactivity or submit. The backend
 * holds proactive Jarvis pushes (paraphrased questions, done synthesis)
 * while typing is true so they don't pop in front of the user's reply. */
export type ClientTypingMsg = {
  type: "client_typing";
  typing: boolean;
};

/** PRD 0004 — sticky voice mode for the session. Sent on toggle (and on
 * connect/reconnect for re-sync). The backend stores the flag per session
 * so subsequent proactive assistant pushes (sub-task done synthesis,
 * paraphrased ask_user) are voiced too — not only direct replies to a
 * `user_msg` carrying its own `voice: true`. */
export type VoiceModeMsg = {
  type: "voice_mode";
  enabled: boolean;
};

export type ClientMessage =
  | UserMsg
  | DismissTaskMsg
  | RequestTaskMessagesMsg
  | CancelTaskMsg
  | ClientTypingMsg
  | VoiceModeMsg;

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

/** PRD 0006 / issue 0049 — incremental chunk of the assistant's speech as it
 *  streams from the LLM. The frontend accumulates `delta` into a per-msg_id
 *  buffer so the sphere transcript can render the spoken phrase character-
 *  by-character before the final `assistant_msg` arrives. TTS itself stays
 *  server-side (Kokoro on the backend); the wire stays text-only here. */
export type SpeechDeltaMsg = {
  type: "speech_delta";
  /** Turn-stable id, shared with the eventual `assistant_msg` frame. */
  msg_id: string;
  /** Newly-visible suffix of the streamed `say.speech` field, NOT the
   *  accumulated buffer. The consumer is responsible for concatenation. */
  delta: string;
};

/** PRD 0006 / issue 0049 — emitted once on argument-object close when the
 *  `say` tool carried a non-null `ui`. Opens the markdown overlay immediately,
 *  before the closing `assistant_msg` lands. Omitted entirely when `ui` is
 *  null / missing — the absence of this frame is the "no overlay" signal. */
export type UiPayloadMsg = {
  type: "ui_payload";
  /** Same `msg_id` as the streamed `speech_delta` frames + the eventual
   *  `assistant_msg`. */
  msg_id: string;
  /** Server-driven component descriptor — same `{ component, props }` shape
   *  the legacy `assistant_msg.ui` array carries. The frontend hands the
   *  descriptor to the existing `Dispatcher` / `MarkdownOverlay` plumbing
   *  without re-parsing the contract. */
  ui: ComponentDescriptor;
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
  /** PRD 0008 / issue 0064; PRD 0010 / issue 0066 made it a LIST — the
   * structured deliverable the sub-agent's terminal `done` resolved to, as an
   * ordered list of section descriptors (a single card is a list-of-one),
   * carried alongside the `result` text. Present only for tasks with a
   * structured deliverable; summary-only tasks render off `result` as before.
   * The task-result effect opens the `SectionsOverlay` from this list. */
  resultPayload?: ComponentDescriptor[];
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
  /** PRD 0008 / issue 0064; PRD 0010 / issue 0066 made it a LIST — the
   * structured deliverable as an ordered list of section descriptors
   * (`{ component, props }[]`; a single card is a list-of-one). Sent on the
   * live completion event and replayed at connect time when the task persisted
   * a structured deliverable. Omitted for summary-only tasks, so older clients
   * keep rendering off `result`. The backend ships the REAL props here (the
   * overlay needs subject / body to render); only the debug sinks see a
   * redacted copy. */
  result_payload?: ComponentDescriptor[];
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

/** PRD 0011 / issue 0069 — a live chunk of a running sub-agent's reasoning
 * (chain-of-thought). Emitted on the chat WS per reasoning delta during a
 * sub-task run, tagged by `agent_ref` (the sub-task's `task_id`). The frontend
 * `activityFeedStore` accumulates `delta` into a per-agent reasoning buffer so
 * the `AgentBlock` can render the streaming reasoning text token-by-token.
 *
 * Purely COSMETIC: the sub-agent's action is always parsed server-side from the
 * aggregated content, never from this stream. A model / endpoint without a
 * reasoning channel simply never emits this event (degraded mode). */
export type ReasoningDeltaMsg = {
  type: "reasoning_delta";
  /** The running sub-task's `task_id`. */
  agent_ref: string;
  /** Newly-visible suffix of the reasoning stream, NOT the accumulated
   * buffer. The consumer concatenates. */
  delta: string;
};

/** PRD 0011 / issue 0071 — the curated agent-activity taxonomy. Mirrors
 * `bob.sub_agent.activity_projector.AgentActivityKind`. A discrete agent action
 * (`tool_call`, `ask_user`) or a salient incident (`stall`, `cap`, `retry`,
 * `validation_failed`) or a lifecycle bookend (`started`, `finished`). Passing
 * validations are suppressed server-side and never reach the wire. */
export type AgentActivityKind =
  | "started"
  | "finished"
  | "tool_call"
  | "ask_user"
  | "stall"
  | "cap"
  | "retry"
  | "validation_failed";

/** Visual state a chip renders in. Mirrors
 * `bob.sub_agent.activity_projector.AgentActivityStatus`. */
export type AgentActivityStatus = "running" | "ok" | "error" | "warn" | "info";

/** PRD 0011 / issue 0071 — a discrete agent-activity chip, emitted on the chat
 * WS interleaved chronologically with the `reasoning_delta` stream. Tagged by
 * `agent_ref` (the sub-task's `task_id`) so the `activityFeedStore` can append
 * it to the right per-agent timeline (kept ordered for the lanes work, issue
 * 0073). The `label` is already redacted server-side (Mail subject / body), so
 * it never carries email content. */
export type AgentActivityMsg = {
  type: "agent_activity";
  /** The producing sub-task's `task_id`. */
  agent_ref: string;
  kind: AgentActivityKind;
  /** Short, user-facing, redacted label for the chip. */
  label: string;
  status: AgentActivityStatus;
};

export type ServerMessage =
  | SessionMsg
  | ReasoningDeltaMsg
  | AgentActivityMsg
  | AssistantMsg
  | SpeechDeltaMsg
  | UiPayloadMsg
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
