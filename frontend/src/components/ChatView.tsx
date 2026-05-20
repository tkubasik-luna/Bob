import { type KeyboardEvent, useCallback, useEffect, useRef, useState } from "react";
import { WS_URL } from "../config";
import { useWebSocket } from "../hooks/useWebSocket";
import { useChatStore } from "../store/chatStore";
import type { ChatMessage, ServerMessage } from "../types/ws";
import { Dispatcher } from "./Dispatcher";

export function ChatView() {
  const messages = useChatStore((s) => s.messages);
  const connectionStatus = useChatStore((s) => s.connectionStatus);
  const isWaitingResponse = useChatStore((s) => s.isWaitingResponse);
  const addUserMessage = useChatStore((s) => s.addUserMessage);
  const addAssistantMessage = useChatStore((s) => s.addAssistantMessage);
  const setStatus = useChatStore((s) => s.setStatus);
  const setWaiting = useChatStore((s) => s.setWaiting);
  const setSessionId = useChatStore((s) => s.setSessionId);

  const handleMessage = useCallback(
    (msg: ServerMessage) => {
      switch (msg.type) {
        case "session":
          setSessionId(msg.session_id);
          break;
        case "thinking":
          setWaiting(msg.state === "start");
          break;
        case "assistant_msg":
          addAssistantMessage(msg.speech, msg.ui);
          break;
        case "error":
          addAssistantMessage(`[error] ${msg.message}`);
          setWaiting(false);
          break;
      }
    },
    [addAssistantMessage, setSessionId, setWaiting],
  );

  const { status, send } = useWebSocket({ url: WS_URL, onMessage: handleMessage });

  // Mirror hook status into the store so the badge/UI stays reactive everywhere.
  useEffect(() => {
    setStatus(status);
  }, [status, setStatus]);

  const [input, setInput] = useState("");
  const scrollRef = useRef<HTMLDivElement | null>(null);

  // Auto-scroll to bottom on new message or waiting state change.
  // `messages` and `isWaitingResponse` are read so the effect re-runs when they change.
  useEffect(() => {
    void messages;
    void isWaitingResponse;
    const node = scrollRef.current;
    if (node) node.scrollTop = node.scrollHeight;
  }, [messages, isWaitingResponse]);

  const trimmed = input.trim();
  const canSend = trimmed.length > 0 && status === "open";

  const submit = () => {
    if (!canSend) return;
    addUserMessage(trimmed);
    send({ type: "user_msg", content: trimmed });
    setInput("");
  };

  const onKeyDown = (e: KeyboardEvent<HTMLTextAreaElement>) => {
    if (e.key === "Enter" && !e.shiftKey) {
      e.preventDefault();
      submit();
    }
  };

  return (
    <div className="flex h-full flex-col bg-neutral-950 text-neutral-100">
      <header className="flex items-center justify-between border-b border-neutral-800 px-4 py-3">
        <h1 className="text-lg font-semibold tracking-tight">Bob</h1>
        {connectionStatus !== "open" && (
          <span className="rounded-full bg-red-900/40 px-2 py-0.5 text-xs text-red-200">
            {connectionStatus === "connecting" ? "connexion…" : "déconnecté"}
          </span>
        )}
      </header>

      <div ref={scrollRef} className="flex-1 overflow-y-auto px-4 py-4">
        <div className="mx-auto flex max-w-2xl flex-col gap-3">
          {messages.map((m) => (
            <Bubble key={m.id} message={m} />
          ))}
          {isWaitingResponse && <ThinkingDots />}
        </div>
      </div>

      <div className="border-t border-neutral-800 px-4 py-3">
        <div className="mx-auto flex max-w-2xl items-end gap-2">
          <textarea
            value={input}
            onChange={(e) => setInput(e.target.value)}
            onKeyDown={onKeyDown}
            rows={2}
            placeholder="Écris un message à Bob…"
            className="flex-1 resize-none rounded-md border border-neutral-800 bg-neutral-900 px-3 py-2 text-sm text-neutral-100 placeholder-neutral-500 focus:border-neutral-600 focus:outline-none"
          />
          <button
            type="button"
            onClick={submit}
            disabled={!canSend}
            className="rounded-md bg-neutral-100 px-4 py-2 text-sm font-medium text-neutral-900 disabled:cursor-not-allowed disabled:bg-neutral-800 disabled:text-neutral-500"
          >
            Envoyer
          </button>
        </div>
      </div>
    </div>
  );
}

function Bubble({ message }: { message: ChatMessage }) {
  const isUser = message.role === "user";
  const ui = message.ui ?? [];
  return (
    <div className={`flex flex-col gap-2 ${isUser ? "items-end" : "items-start"}`}>
      <div
        className={`max-w-[80%] whitespace-pre-wrap rounded-2xl px-3 py-2 text-sm ${
          isUser
            ? "rounded-br-sm bg-blue-600 text-white"
            : "rounded-bl-sm bg-neutral-800 text-neutral-100"
        }`}
      >
        {message.content}
      </div>
      {!isUser && ui.length > 0 && (
        <div className="w-full max-w-[80%]">
          <Dispatcher ui={ui} />
        </div>
      )}
    </div>
  );
}

function ThinkingDots() {
  const [dots, setDots] = useState(".");
  useEffect(() => {
    const id = setInterval(() => {
      setDots((d) => (d.length >= 3 ? "." : `${d}.`));
    }, 400);
    return () => clearInterval(id);
  }, []);
  return (
    <div className="flex justify-start">
      <div className="rounded-2xl rounded-bl-sm bg-neutral-800 px-3 py-2 text-sm text-neutral-400">
        {dots}
      </div>
    </div>
  );
}
