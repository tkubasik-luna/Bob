import { create } from "zustand";
import type { ChatMessage, ConnectionStatus } from "../types/ws";

type ChatState = {
  messages: ChatMessage[];
  connectionStatus: ConnectionStatus;
  isWaitingResponse: boolean;
  sessionId: string | null;
  addUserMessage: (content: string) => void;
  addAssistantMessage: (content: string) => void;
  setStatus: (status: ConnectionStatus) => void;
  setWaiting: (waiting: boolean) => void;
  setSessionId: (id: string | null) => void;
};

function randomId(): string {
  // Prefer crypto.randomUUID where available; fall back to a simple random string.
  if (typeof crypto !== "undefined" && typeof crypto.randomUUID === "function") {
    return crypto.randomUUID();
  }
  return `${Date.now()}-${Math.random().toString(36).slice(2, 10)}`;
}

export const useChatStore = create<ChatState>((set) => ({
  messages: [],
  connectionStatus: "connecting",
  isWaitingResponse: false,
  sessionId: null,
  addUserMessage: (content) =>
    set((state) => ({
      messages: [...state.messages, { id: randomId(), role: "user", content }],
    })),
  addAssistantMessage: (content) =>
    set((state) => ({
      messages: [...state.messages, { id: randomId(), role: "assistant", content }],
    })),
  setStatus: (connectionStatus) => set({ connectionStatus }),
  setWaiting: (isWaitingResponse) => set({ isWaitingResponse }),
  setSessionId: (sessionId) => set({ sessionId }),
}));
