import { create } from "zustand";
import type { ChatMessage, ComponentDescriptor, ConnectionStatus } from "../types/ws";

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
