import { useCallback, useEffect, useRef } from "react";
import {
  enqueue as audioEnqueue,
  stop as audioStop,
  subscribeSpeaking,
} from "../audio/audioPlayer";
import { WS_URL } from "../config";
import { useChatStore } from "../store/chatStore";
import type { ClientMessage, ConnectionStatus, ServerMessage } from "../types/ws";
import { useVoiceMode } from "./useVoiceMode";
import { useWebSocket } from "./useWebSocket";

type UseChatWsBridgeResult = {
  status: ConnectionStatus;
  send: (msg: ClientMessage) => void;
};

/**
 * Wires the chat WebSocket end-to-end: spins up a single `useWebSocket`,
 * routes every `ServerMessage` into the store, bridges the audio player to
 * `speakingMsgId`, and mirrors the connection status back to the store.
 *
 * This is the same dispatch logic `ChatView` runs inline today (see
 * `ChatView.handleMessage`) — extracted so the `?ui=new` window can mount the
 * full pipeline without rendering ChatView. The two copies will be deduped in
 * a later issue; for now we accept the verbatim duplication to keep the
 * refactor narrowly scoped.
 *
 * Returns `{ status, send }` so callers can read the live connection state
 * and dispatch outbound frames. `send` is provided to the InputField via
 * `SphereWsContext` (see `frontend/src/components/sphere/sphereWsContext.tsx`).
 */
export function useChatWsBridge(): UseChatWsBridgeResult {
  const addAssistantMessage = useChatStore((s) => s.addAssistantMessage);
  const setStatus = useChatStore((s) => s.setStatus);
  const setWaiting = useChatStore((s) => s.setWaiting);
  const setSessionId = useChatStore((s) => s.setSessionId);
  const pushToast = useChatStore((s) => s.pushToast);
  const dismissToast = useChatStore((s) => s.dismissToast);
  const setSpeakingMsgId = useChatStore((s) => s.setSpeakingMsgId);
  const upsertTaskCreated = useChatStore((s) => s.upsertTaskCreated);
  const upsertTaskUpdated = useChatStore((s) => s.upsertTaskUpdated);
  const setTaskResult = useChatStore((s) => s.setTaskResult);
  const setTaskMessagesSnapshot = useChatStore((s) => s.setTaskMessagesSnapshot);
  const appendTaskMessage = useChatStore((s) => s.appendTaskMessage);
  const appendSpeechDelta = useChatStore((s) => s.appendSpeechDelta);
  const setStreamingUi = useChatStore((s) => s.setStreamingUi);
  const clearStreamingAssistant = useChatStore((s) => s.clearStreamingAssistant);

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
          // A user-turn reply supersedes any in-flight audio (the user moved
          // on); a proactive push (slice #0021 paraphrase, or a task-done
          // synthesis under PRD 0004 voice mode) must NOT hard-cut it.
          if (!msg.proactive) {
            audioStop();
            audioStreamRef.current = null;
          }
          // Register the msg_id for BOTH kinds. Proactive task-done pushes now
          // carry their own TTS stream (ws_router synthesises for proactive
          // assistant_msg in voice mode); without claiming the audio channel
          // here, the matching `audio_start` is rejected as stale below and
          // every PCM frame is dropped — i.e. no voice at task exit.
          if (msg.msg_id) {
            currentMsgIdRef.current = msg.msg_id;
          }
          addAssistantMessage(msg.speech, msg.ui, msg.msg_id, msg.proactive);
          // PRD 0006 / issue 0049 — the persisted bubble takes over from
          // the in-flight streamed buffer. Clear it so `TranscriptLine`
          // stops mirroring a duplicate of the same text and `SphereUI`
          // falls back to the final `messages` array.
          clearStreamingAssistant();
          break;
        case "speech_delta":
          // PRD 0006 / issue 0049 — accumulate the streamed `say.speech`
          // suffix. The sphere transcript reads from `streamingAssistant`
          // to render the partial phrase before the closing `assistant_msg`.
          appendSpeechDelta(msg.msg_id, msg.delta);
          break;
        case "ui_payload":
          // PRD 0006 / issue 0049 — first (and only) ui frame for the
          // streamed turn. Opens the markdown overlay immediately; the
          // closing `assistant_msg` carries the same payload but the
          // overlay is already up by then.
          setStreamingUi(msg.msg_id, msg.ui);
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
        case "task_messages_snapshot":
          setTaskMessagesSnapshot(msg);
          break;
        case "task_message":
          appendTaskMessage(msg);
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
      setTaskMessagesSnapshot,
      appendTaskMessage,
      appendSpeechDelta,
      setStreamingUi,
      clearStreamingAssistant,
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

  // Mirror hook status into the store so the badge/UI stays reactive everywhere.
  useEffect(() => {
    setStatus(status);
  }, [status, setStatus]);

  // Mirror voice mode to the backend as sticky session state so proactive
  // pushes (sub-task done synthesis, paraphrased ask_user) get TTS too.
  // Sends on every toggle AND every time the WS reaches `open` (covers
  // initial connect + reconnect re-sync).
  const { voiceEnabled } = useVoiceMode();
  useEffect(() => {
    if (status !== "open") return;
    send({ type: "voice_mode", enabled: voiceEnabled });
  }, [status, voiceEnabled, send]);

  return { status, send };
}
