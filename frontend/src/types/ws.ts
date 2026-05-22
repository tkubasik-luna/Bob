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

export type ClientMessage = UserMsg;

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

export type ServerMessage =
  | SessionMsg
  | AssistantMsg
  | ThinkingMsg
  | ErrorMsg
  | AudioStartMsg
  | AudioEndMsg
  | TtsPreparingMsg
  | TtsReadyMsg
  | AudioErrorMsg;

export type ConnectionStatus = "connecting" | "open" | "closed";

export type ChatMessage = {
  id: string;
  role: "user" | "assistant";
  content: string;
  ui?: ComponentDescriptor[];
};
