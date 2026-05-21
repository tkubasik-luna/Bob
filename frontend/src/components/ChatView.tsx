import { type KeyboardEvent, useCallback, useEffect, useRef, useState } from "react";
import { enqueue as audioEnqueue, generateSineFloat32 } from "../audio/audioPlayer";
import { WS_URL } from "../config";
import { useVoiceMode } from "../hooks/useVoiceMode";
import { useWebSocket } from "../hooks/useWebSocket";
import { useChatStore } from "../store/chatStore";
import type { ChatMessage, ServerMessage } from "../types/ws";
import { Dispatcher } from "./Dispatcher";
import { ToastContainer } from "./Toast";

// Toggle the dev/temporary sine-wave test button. Flip to `false` (or gate on
// import.meta.env.DEV) once the WS audio path is wired in issue 0010.
const SHOW_AUDIO_TEST_BUTTON = import.meta.env.DEV;

export function ChatView() {
  const messages = useChatStore((s) => s.messages);
  const connectionStatus = useChatStore((s) => s.connectionStatus);
  const isWaitingResponse = useChatStore((s) => s.isWaitingResponse);
  const addUserMessage = useChatStore((s) => s.addUserMessage);
  const addAssistantMessage = useChatStore((s) => s.addAssistantMessage);
  const setStatus = useChatStore((s) => s.setStatus);
  const setWaiting = useChatStore((s) => s.setWaiting);
  const setSessionId = useChatStore((s) => s.setSessionId);
  const pushToast = useChatStore((s) => s.pushToast);

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
          pushToast(msg.message, msg.code);
          setWaiting(false);
          break;
      }
    },
    [addAssistantMessage, setSessionId, setWaiting, pushToast],
  );

  const { status, send } = useWebSocket({ url: WS_URL, onMessage: handleMessage });
  const { voiceEnabled, toggle: toggleVoice } = useVoiceMode();

  const playSineTest = useCallback(() => {
    const sampleRate = 24_000;
    audioEnqueue(generateSineFloat32(440, 1, sampleRate), sampleRate, "dev-sine");
  }, []);

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
    <div className="relative flex h-full flex-col bg-neutral-950 text-neutral-100">
      <ToastContainer />
      <header className="flex items-center justify-between border-b border-neutral-800 px-4 py-3">
        <h1 className="text-lg font-semibold tracking-tight">Bob</h1>
        <div className="flex items-center gap-2">
          {connectionStatus !== "open" && (
            <span className="rounded-full bg-red-900/40 px-2 py-0.5 text-xs text-red-200">
              {connectionStatus === "connecting" ? "connexion…" : "déconnecté"}
            </span>
          )}
          {SHOW_AUDIO_TEST_BUTTON && (
            <button
              type="button"
              onClick={playSineTest}
              title="Lire un sinus 440Hz de test (dev)"
              className="rounded-md border border-neutral-800 bg-neutral-900 px-2 py-1 text-xs text-neutral-300 hover:bg-neutral-800"
            >
              ♪ test
            </button>
          )}
          <VoiceToggleButton enabled={voiceEnabled} onToggle={toggleVoice} />
        </div>
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

function VoiceToggleButton({ enabled, onToggle }: { enabled: boolean; onToggle: () => void }) {
  return (
    <button
      type="button"
      onClick={onToggle}
      aria-pressed={enabled}
      title={enabled ? "Désactiver la voix" : "Activer la voix"}
      className={`flex h-8 w-8 items-center justify-center rounded-md border text-sm transition-colors ${
        enabled
          ? "border-blue-500/60 bg-blue-600/20 text-blue-200 hover:bg-blue-600/30"
          : "border-neutral-800 bg-neutral-900 text-neutral-400 hover:bg-neutral-800"
      }`}
    >
      {enabled ? <SpeakerIcon /> : <SpeakerMutedIcon />}
    </button>
  );
}

function SpeakerIcon() {
  return (
    <svg
      xmlns="http://www.w3.org/2000/svg"
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth="2"
      strokeLinecap="round"
      strokeLinejoin="round"
      className="h-4 w-4"
      aria-hidden="true"
    >
      <title>Voix activée</title>
      <path d="M11 5 6 9H2v6h4l5 4z" />
      <path d="M15.54 8.46a5 5 0 0 1 0 7.07" />
      <path d="M19.07 4.93a10 10 0 0 1 0 14.14" />
    </svg>
  );
}

function SpeakerMutedIcon() {
  return (
    <svg
      xmlns="http://www.w3.org/2000/svg"
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth="2"
      strokeLinecap="round"
      strokeLinejoin="round"
      className="h-4 w-4"
      aria-hidden="true"
    >
      <title>Voix désactivée</title>
      <path d="M11 5 6 9H2v6h4l5 4z" />
      <line x1="22" y1="9" x2="16" y2="15" />
      <line x1="16" y1="9" x2="22" y2="15" />
    </svg>
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
