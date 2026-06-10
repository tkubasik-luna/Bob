import { useCallback, useEffect, useRef } from "react";
import {
  enqueue as audioEnqueue,
  stop as audioStop,
  subscribeSpeaking,
} from "../audio/audioPlayer";
import { WS_URL } from "../config";
import { useActivityFeedStore } from "../store/activityFeedStore";
import { useChatStore } from "../store/chatStore";
import type { ClientMessage, ConnectionStatus, ServerMessage } from "../types/ws";
import { useVoiceMode } from "./useVoiceMode";
import { useWebSocket } from "./useWebSocket";

type UseChatWsBridgeResult = {
  status: ConnectionStatus;
  send: (msg: ClientMessage) => void;
  /** Raw binary sender for the « Listen » mic path (issue 0099). */
  sendBinary: (data: ArrayBuffer) => void;
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
  const setLiveTranscript = useChatStore((s) => s.setLiveTranscript);
  const clearLiveTranscript = useChatStore((s) => s.clearLiveTranscript);
  const appendReasoningDelta = useActivityFeedStore((s) => s.appendReasoningDelta);
  const appendActivity = useActivityFeedStore((s) => s.appendActivity);
  const setPerf = useActivityFeedStore((s) => s.setPerf);
  const setAnswer = useActivityFeedStore((s) => s.setAnswer);
  const markAgentFinished = useActivityFeedStore((s) => s.markAgentFinished);
  const rehydrateFromTasks = useActivityFeedStore((s) => s.rehydrateFromTasks);
  const markJarvisTurnStart = useActivityFeedStore((s) => s.markJarvisTurnStart);
  const commitJarvisTurn = useActivityFeedStore((s) => s.commitJarvisTurn);

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
  // Stable id for the "Préparation de la transcription…" toast, keyed by the
  // voice turn so a first-use whisper download doesn't look like a frozen app.
  const sttPrepToastId = useCallback((turnId: string) => `stt-prep:${turnId}`, []);

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
          // PRD 0014 — open a new Jarvis turn so the BobCard chat transcript can
          // split the flat (accumulating) jarvis lane into per-turn blocks. The
          // pending start binds to this turn's `assistant_msg` below.
          if (msg.state === "start") {
            markJarvisTurnStart();
          }
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
            // PRD 0014 — bind the pending Jarvis turn start to this reply's id so
            // the transcript can resolve this turn's reasoning/tasks slice.
            commitJarvisTurn(msg.msg_id);
          }
          addAssistantMessage(msg.speech, msg.ui, msg.msg_id, msg.proactive);
          // PRD 0006 / issue 0049 — the persisted bubble takes over from
          // the in-flight streamed buffer. Clear it so `TranscriptLine`
          // stops mirroring a duplicate of the same text and `SphereUI`
          // falls back to the final `messages` array.
          clearStreamingAssistant();
          // Bob has replied — the user's live STT transcript has served its
          // purpose; drop it so it doesn't linger over the new turn.
          clearLiveTranscript();
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
        case "stt_partial":
          // Live whisper hypothesis as the user speaks. Without this the STT
          // events were silently dropped and the user got zero feedback.
          setLiveTranscript(msg.turn_id, msg.text, msg.stable_prefix_len, false);
          break;
        case "stt_final":
          // Frozen transcript at endpoint — settle the live line; the say-path
          // now owns the text. Kept on screen until Bob's reply clears it.
          setLiveTranscript(msg.turn_id, msg.text, msg.text.length, true);
          break;
        case "stt_preparing":
          // First-use whisper download — surface a toast so the wait isn't a
          // silent freeze (mirrors tts_preparing).
          pushToast("Préparation de la transcription…", {
            kind: "info",
            id: sttPrepToastId(msg.turn_id),
          });
          break;
        case "stt_ready":
          dismissToast(sttPrepToastId(msg.turn_id));
          break;
        case "wake_word":
          // « Yo Bob » heard by the standby detector — confirm instantly so
          // the user knows the wake worked while the listening turn opens
          // (the orb's écoute mood follows from the turn_state event).
          pushToast(`« ${msg.phrase} » détecté — j'écoute`, {
            kind: "info",
            id: `wake_${msg.turn_id}`,
          });
          break;
        case "voice_turn_error":
          // STT engine unavailable / failed mid-turn / download failed. The
          // backend aborted the turn cleanly; surface it + return to idle
          // instead of leaving the user staring at a dead mic.
          dismissToast(sttPrepToastId(msg.turn_id));
          clearLiveTranscript();
          setWaiting(false);
          pushToast(`Transcription indisponible : ${msg.reason}`, {
            kind: "error",
            code: "STT",
          });
          break;
        case "task_created":
          upsertTaskCreated(msg);
          // PRD 0011 / issue 0077 — a task replayed already in a terminal state
          // (the backend can replay `task_created` with the CURRENT state, not
          // just `pending`) must rehydrate its finished lane too, in case the
          // matching `task_updated` isn't separately replayed.
          if (msg.replayed && (msg.state === "done" || msg.state === "failed")) {
            markAgentFinished(msg.task_id, msg.state);
            rehydrateFromTasks(Object.values(useChatStore.getState().tasks));
          }
          break;
        case "task_updated":
          upsertTaskUpdated(msg);
          // PRD 0011 / issue 0074 — a terminal transition (done / failed; the
          // backend collapses degraded / timeout / force-terminate onto
          // `failed`) flips the agent's block from the live ACTIVE timeline to
          // the COLLAPSED summary. The timeline arrays are RETAINED so the user
          // can expand the collapsed block to re-read the reasoning.
          if (msg.state === "done" || msg.state === "failed") {
            markAgentFinished(msg.task_id, msg.state);
            // PRD 0011 / issue 0077 — REHYDRATE on reload. On (re)connect the
            // backend replays the `task_*` frames for every persisted task (the
            // snapshot/bootstrap source). For a REPLAYED terminal task there is
            // no live reasoning stream to register its lane, so the finished
            // block would never appear (lanes render off `agentOrder`, which is
            // grown by reasoning / activity events). Reconstruct the lanes from
            // the now-updated chatStore task map: this registers the finished
            // lane (+ final state) so the collapsed block shows up, with its
            // result button resolving the replayed `result_payload`. No-op for
            // the live path (the lane already exists) thanks to the store's
            // change-detection. We guard on `replayed` to avoid touching lanes
            // on every live terminal transition.
            if (msg.replayed) {
              rehydrateFromTasks(Object.values(useChatStore.getState().tasks));
            }
          }
          break;
        case "task_result":
          setTaskResult(msg);
          // Reasoning-streaming PRD — the deliverable IS the sub-agent's settled
          // answer; route it into the lane's answer block (same slice Jarvis uses
          // via `agent_answer`).
          setAnswer(msg.task_id, msg.result);
          // PRD 0011 / issue 0077 — a replayed result implies a finished task.
          // Rehydrate so the lane is present and its "résultat" button resolves
          // the just-stored `result_payload`, even if the terminal `task_updated`
          // ordering left the lane unregistered.
          if (msg.replayed) {
            rehydrateFromTasks(Object.values(useChatStore.getState().tasks));
          }
          break;
        case "task_messages_snapshot":
          setTaskMessagesSnapshot(msg);
          break;
        case "task_message":
          appendTaskMessage(msg);
          break;
        case "reasoning_delta":
          // PRD 0011 / issue 0069 — live reasoning of a running sub-task.
          // Accumulated per `agent_ref` so `AgentBlock` renders it streaming.
          appendReasoningDelta(msg);
          break;
        case "agent_activity":
          // PRD 0011 / issue 0071 — a discrete activity chip, appended to the
          // agent's timeline INTERLEAVED chronologically with the reasoning.
          appendActivity(msg);
          break;
        case "agent_perf":
          // Reasoning-streaming PRD — terminal per-agent perf footer (tok/s,
          // ttft, tokens). One per streamed call; routed to the agent's lane.
          setPerf(msg);
          break;
        case "agent_answer":
          // Reasoning-streaming PRD — Jarvis's settled reply, distinct from its
          // chain-of-thought; rendered as the lane's answer block.
          setAnswer(msg.agent_ref, msg.text);
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
      sttPrepToastId,
      setLiveTranscript,
      clearLiveTranscript,
      upsertTaskCreated,
      upsertTaskUpdated,
      setTaskResult,
      setTaskMessagesSnapshot,
      appendTaskMessage,
      appendSpeechDelta,
      setStreamingUi,
      clearStreamingAssistant,
      appendReasoningDelta,
      appendActivity,
      setPerf,
      setAnswer,
      markAgentFinished,
      rehydrateFromTasks,
      markJarvisTurnStart,
      commitJarvisTurn,
    ],
  );

  const handleBinary = useCallback((data: ArrayBuffer) => {
    const stream = audioStreamRef.current;
    if (!stream) return;
    if (stream.msgId !== currentMsgIdRef.current) return;
    audioEnqueue(data, stream.sampleRate, stream.msgId);
  }, []);

  const { status, send, sendBinary } = useWebSocket({
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

  return { status, send, sendBinary };
}
