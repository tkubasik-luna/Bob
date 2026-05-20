import { create } from "zustand";
import type { ChatMessage, ComponentDescriptor, ConnectionStatus } from "../types/ws";

export type Toast = {
  id: string;
  message: string;
  code?: string;
  createdAt: number;
};

const TOAST_AUTO_DISMISS_MS = 5_000;

type ChatState = {
  messages: ChatMessage[];
  connectionStatus: ConnectionStatus;
  isWaitingResponse: boolean;
  sessionId: string | null;
  toasts: Toast[];
  addUserMessage: (content: string) => void;
  addAssistantMessage: (content: string, ui?: ComponentDescriptor[]) => void;
  setStatus: (status: ConnectionStatus) => void;
  setWaiting: (waiting: boolean) => void;
  setSessionId: (id: string | null) => void;
  pushToast: (message: string, code?: string) => void;
  dismissToast: (id: string) => void;
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
  addUserMessage: (content) =>
    set((state) => ({
      messages: [...state.messages, { id: randomId(), role: "user", content }],
    })),
  addAssistantMessage: (content, ui) =>
    set((state) => ({
      messages: [...state.messages, { id: randomId(), role: "assistant", content, ui }],
    })),
  setStatus: (connectionStatus) => set({ connectionStatus }),
  setWaiting: (isWaitingResponse) => set({ isWaitingResponse }),
  setSessionId: (sessionId) => set({ sessionId }),
  pushToast: (message, code) => {
    const id = randomId();
    set((state) => ({
      toasts: [...state.toasts, { id, message, code, createdAt: Date.now() }],
    }));
    setTimeout(() => {
      get().dismissToast(id);
    }, TOAST_AUTO_DISMISS_MS);
  },
  dismissToast: (id) =>
    set((state) => ({
      toasts: state.toasts.filter((t) => t.id !== id),
    })),
}));
