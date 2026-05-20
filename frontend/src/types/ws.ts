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
};

export type ClientMessage = UserMsg;

// Server → client
export type SessionMsg = {
  type: "session";
  session_id: string;
};

export type AssistantMsg = {
  type: "assistant_msg";
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

export type ServerMessage = SessionMsg | AssistantMsg | ThinkingMsg | ErrorMsg;

export type ConnectionStatus = "connecting" | "open" | "closed";

export type ChatMessage = {
  id: string;
  role: "user" | "assistant";
  content: string;
};
