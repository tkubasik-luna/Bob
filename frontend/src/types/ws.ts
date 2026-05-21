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

export type AudioChunkMsg = {
  type: "audio_chunk";
  msg_id: string;
  seq: number;
  /** Base64-encoded s16le mono PCM. */
  pcm_b64: string;
  sample_rate: number;
};

export type AudioEndMsg = {
  type: "audio_end";
  msg_id: string;
};

export type ServerMessage =
  | SessionMsg
  | AssistantMsg
  | ThinkingMsg
  | ErrorMsg
  | AudioChunkMsg
  | AudioEndMsg;

export type ConnectionStatus = "connecting" | "open" | "closed";

export type ChatMessage = {
  id: string;
  role: "user" | "assistant";
  content: string;
  ui?: ComponentDescriptor[];
};
