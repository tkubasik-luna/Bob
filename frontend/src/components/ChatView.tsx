import { type KeyboardEvent, useCallback, useEffect, useRef, useState } from "react";
import {
  enqueue as audioEnqueue,
  stop as audioStop,
  subscribeSpeaking,
} from "../audio/audioPlayer";
import { WS_URL } from "../config";
import { useVoiceMode } from "../hooks/useVoiceMode";
import { useWebSocket } from "../hooks/useWebSocket";
import { useChatStore } from "../store/chatStore";
import type { ChatMessage, ServerMessage } from "../types/ws";
import { Dispatcher } from "./Dispatcher";
import { TaskSidebar } from "./TaskSidebar";
import { ToastContainer } from "./Toast";

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
  const dismissToast = useChatStore((s) => s.dismissToast);
  const speakingMsgId = useChatStore((s) => s.speakingMsgId);
  const setSpeakingMsgId = useChatStore((s) => s.setSpeakingMsgId);
  const upsertTaskCreated = useChatStore((s) => s.upsertTaskCreated);
  const upsertTaskUpdated = useChatStore((s) => s.upsertTaskUpdated);
  const setTaskResult = useChatStore((s) => s.setTaskResult);

  // Bridge audioPlayer → store so `Bubble` can render the wave indicator
  // on the exact bubble currently being voiced. Cleared on natural end
  // AND on interruption (audioPlayer.stop()), per acceptance criteria.
  useEffect(() => {
    const unsubscribe = subscribeSpeaking((id) => {
      setSpeakingMsgId(id);
    });
    return unsubscribe;
  }, [setSpeakingMsgId]);

  // Stable id for the "Préparation de la voix…" toast — keyed by msg_id
  // so concurrent first-message scenarios stay isolated.
  const prepToastId = useCallback((msgId: string) => `tts-prep:${msgId}`, []);

  // Tracks the msg_id of the most recently received assistant_msg. Audio
  // frames carrying a different msg_id are stale (the user interrupted Bob)
  // and must be dropped: the backend cancellation may race with frames
  // already in flight on the socket.
  const currentMsgIdRef = useRef<string | null>(null);
  // The current audio stream (msg_id + sample_rate) announced via the most
  // recent `audio_start`. Subsequent binary frames are decoded against this
  // sample rate and tagged with this msg_id until `audio_end`.
  const audioStreamRef = useRef<{ msgId: string; sampleRate: number } | null>(null);

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
          // Proactive pushes (slice #0021) are auto-emitted by Jarvis without
          // a user prompt — they must NOT interrupt the previous turn's audio
          // and must NOT reset `currentMsgIdRef` (otherwise pending audio
          // frames for the legitimate previous turn would be dropped).
          if (!msg.proactive) {
            audioStop();
            audioStreamRef.current = null;
            if (msg.msg_id) {
              currentMsgIdRef.current = msg.msg_id;
            }
          }
          addAssistantMessage(msg.speech, msg.ui, msg.msg_id, msg.proactive);
          break;
        case "audio_start":
          if (msg.msg_id !== currentMsgIdRef.current) {
            // Header from a cancelled turn — ignore.
            break;
          }
          audioStreamRef.current = { msgId: msg.msg_id, sampleRate: msg.sample_rate };
          break;
        case "audio_end":
          audioStreamRef.current = null;
          // Defensive: if a prep toast somehow survived past audio_end,
          // dismiss it now so the UI never gets stuck.
          dismissToast(prepToastId(msg.msg_id));
          break;
        case "tts_preparing":
          pushToast("Préparation de la voix…", {
            kind: "info",
            id: prepToastId(msg.msg_id),
          });
          break;
        case "tts_ready":
          dismissToast(prepToastId(msg.msg_id));
          break;
        case "audio_error":
          audioStreamRef.current = null;
          dismissToast(prepToastId(msg.msg_id));
          pushToast(`TTS indisponible : ${msg.reason}`, { kind: "error", code: "TTS" });
          break;
        case "error":
          pushToast(msg.message, msg.code);
          setWaiting(false);
          break;
        case "task_created":
          upsertTaskCreated(msg);
          break;
        case "task_updated":
          upsertTaskUpdated(msg);
          break;
        case "task_result":
          setTaskResult(msg);
          break;
      }
    },
    [
      addAssistantMessage,
      setSessionId,
      setWaiting,
      pushToast,
      dismissToast,
      prepToastId,
      upsertTaskCreated,
      upsertTaskUpdated,
      setTaskResult,
    ],
  );

  const handleBinary = useCallback((data: ArrayBuffer) => {
    const stream = audioStreamRef.current;
    if (!stream) return;
    if (stream.msgId !== currentMsgIdRef.current) return;
    audioEnqueue(data, stream.sampleRate, stream.msgId);
  }, []);

  const { status, send } = useWebSocket({
    url: WS_URL,
    onMessage: handleMessage,
    onBinary: handleBinary,
  });
  const { voiceEnabled, toggle: toggleVoice } = useVoiceMode();

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
    send({ type: "user_msg", content: trimmed, ...(voiceEnabled ? { voice: true } : {}) });
    setInput("");
  };

  const onKeyDown = (e: KeyboardEvent<HTMLTextAreaElement>) => {
    if (e.key === "Enter" && !e.shiftKey) {
      e.preventDefault();
      submit();
    }
  };

  return (
    <div className="relative flex h-full bg-neutral-950 text-neutral-100">
      <ToastContainer />
      <div className="flex h-full min-w-0 flex-1 flex-col">
        <header className="flex items-center justify-between border-b border-neutral-800 px-4 py-3">
          <h1 className="text-lg font-semibold tracking-tight">Bob</h1>
          <div className="flex items-center gap-2">
            {connectionStatus !== "open" && (
              <span className="rounded-full bg-red-900/40 px-2 py-0.5 text-xs text-red-200">
                {connectionStatus === "connecting" ? "connexion…" : "déconnecté"}
              </span>
            )}
            <VoiceToggleButton enabled={voiceEnabled} onToggle={toggleVoice} />
          </div>
        </header>

        <div ref={scrollRef} className="flex-1 overflow-y-auto px-4 py-4">
          <div className="mx-auto flex max-w-2xl flex-col gap-3">
            {messages.map((m) => (
              <Bubble key={m.id} message={m} isSpeaking={speakingMsgId === m.id} />
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
      <TaskSidebar />
    </div>
  );
}

function Bubble({
  message,
  isSpeaking = false,
}: {
  message: ChatMessage;
  isSpeaking?: boolean;
}) {
  const isUser = message.role === "user";
  const isProactive = !isUser && message.proactive === true;
  const ui = message.ui ?? [];
  // Proactive assistant pushes (e.g. paraphrased ask_user questions) carry a
  // subtle left accent border + a small "auto" tag so the user can tell Bob
  // spoke unprompted. The text styling stays consistent with regular bubbles
  // so reading flow is unchanged.
  const bubbleClass = isUser
    ? "rounded-br-sm bg-blue-600 text-white"
    : isProactive
      ? "rounded-bl-sm border-l-2 border-amber-400/70 bg-neutral-800 text-neutral-100"
      : "rounded-bl-sm bg-neutral-800 text-neutral-100";
  return (
    <div className={`flex flex-col gap-2 ${isUser ? "items-end" : "items-start"}`}>
      {isProactive && (
        <span
          className="text-[10px] font-medium uppercase tracking-wide text-amber-300/80"
          aria-label="Message proactif de Bob"
          title="Bob a transmis cette question pour une tâche en cours"
        >
          Bob · auto
        </span>
      )}
      <div
        className={`flex max-w-[80%] items-end gap-2 whitespace-pre-wrap rounded-2xl px-3 py-2 text-sm ${bubbleClass}`}
      >
        <span className="min-w-0 flex-1">{message.content}</span>
        {!isUser && isSpeaking && <SpeakingWaveIcon />}
      </div>
      {!isUser && ui.length > 0 && (
        <div className="w-full max-w-[80%]">
          <Dispatcher ui={ui} />
        </div>
      )}
    </div>
  );
}

/**
 * Tiny three-bar animated equalizer shown on the bubble currently being
 * voiced by Kokoro. Pure CSS animation via Tailwind `animate-pulse` with
 * staggered delays — no extra deps. Hidden from screen readers by `aria-hidden`
 * since the indicator is purely decorative (audio itself conveys the info).
 */
function SpeakingWaveIcon() {
  return (
    <span className="inline-flex h-4 items-end gap-0.5" aria-hidden="true" title="Bob parle">
      <span className="block h-1.5 w-0.5 animate-pulse rounded-sm bg-blue-300 [animation-delay:-0.3s]" />
      <span className="block h-3 w-0.5 animate-pulse rounded-sm bg-blue-300 [animation-delay:-0.15s]" />
      <span className="block h-2 w-0.5 animate-pulse rounded-sm bg-blue-300" />
    </span>
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
