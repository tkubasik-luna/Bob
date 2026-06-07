/**
 * useMicCapture — the webview « Listen » mic capture path (issue 0099).
 *
 * Owns the microphone for the HUD `new` window. When `enabled` flips true it:
 *
 *   1. consults {@link getCaptureDecision} — for the PRD default `"webview"`
 *      it runs `getUserMedia({audio:{echoCancellation:true,...}})` so the
 *      browser's AEC references Bob's TTS output (barge-in won't trip on Bob's
 *      own voice); for `"rust"` it leaves a documented seam (the native source
 *      is a stub today, issue 0097 — we do NOT block on it);
 *   2. spins up an AudioContext + the `mic-capture-processor` AudioWorklet
 *      (served from `/micWorklet.js`), which coalesces native-rate Float32
 *      blocks into ~30 ms frames;
 *   3. sends `voice_start` (Annexe A.1), then for each worklet frame
 *      downsamples to 16 kHz, quantises to s16le, tags `0x01`, and ships it as
 *      a binary WS frame via `sendBinary`;
 *   4. on disable / unmount, sends `voice_stop` and tears the graph down.
 *
 * The hook is intentionally side-effecting and returns nothing: mounting it
 * (with `enabled` bound to the voice toggle) is the whole API. The mic is
 * acquired lazily on first enable — no permission prompt until the user
 * actually turns voice on.
 *
 * Errors (mic permission denied, worklet load failure) are reported via the
 * optional `onError` callback and leave the turn un-armed; they never throw
 * into React render.
 */

import { useEffect, useRef } from "react";
import type { ClientMessage } from "../types/ws";
import { getCaptureDecision } from "./aec/captureDecision";
import { buildMicFrame } from "./micDownsample";

type Options = {
  /** Bound to the voice toggle: true arms the mic, false closes it. */
  enabled: boolean;
  /** JSON sender (voice_start / voice_stop). */
  send: (msg: ClientMessage) => void;
  /** Binary sender for tagged PCM frames. */
  sendBinary: (data: ArrayBuffer) => void;
  /** Which window owns the mic (defaults to "new"). */
  windowName?: string;
  /** Optional error sink (permission denied, worklet load failure). */
  onError?: (error: unknown) => void;
};

const WORKLET_URL = "/micWorklet.js";
const WORKLET_NAME = "mic-capture-processor";

type Graph = {
  stream: MediaStream;
  ctx: AudioContext;
  source: MediaStreamAudioSourceNode;
  node: AudioWorkletNode;
};

export function useMicCapture({
  enabled,
  send,
  sendBinary,
  windowName = "new",
  onError,
}: Options): void {
  // Latest callbacks via refs so the capture effect doesn't re-run (and
  // re-prompt for the mic) when a parent re-render hands new closures.
  const sendRef = useRef(send);
  const sendBinaryRef = useRef(sendBinary);
  const onErrorRef = useRef(onError);
  useEffect(() => {
    sendRef.current = send;
  }, [send]);
  useEffect(() => {
    sendBinaryRef.current = sendBinary;
  }, [sendBinary]);
  useEffect(() => {
    onErrorRef.current = onError;
  }, [onError]);

  useEffect(() => {
    if (!enabled) return;

    const decision = getCaptureDecision();
    if (decision.path === "rust") {
      // SEAM (issue 0097 / 0100): the Rust-sourced capture path forwards mic
      // frames from the Tauri shell. No real wire exists yet, so we do not
      // block the webview path on it — when the native source ships, route
      // its frames into `sendBinary` here and skip getUserMedia below.
      return;
    }

    let cancelled = false;
    let graph: Graph | null = null;
    let armed = false;

    const start = async (): Promise<void> => {
      try {
        const stream = await navigator.mediaDevices.getUserMedia({
          audio: {
            echoCancellation: true,
            noiseSuppression: true,
            autoGainControl: true,
            channelCount: 1,
          },
        });
        if (cancelled) {
          for (const track of stream.getTracks()) track.stop();
          return;
        }

        const ctx = new AudioContext();
        await ctx.audioWorklet.addModule(WORKLET_URL);
        if (cancelled) {
          for (const track of stream.getTracks()) track.stop();
          void ctx.close();
          return;
        }

        const source = ctx.createMediaStreamSource(stream);
        const node = new AudioWorkletNode(ctx, WORKLET_NAME, {
          numberOfInputs: 1,
          numberOfOutputs: 0,
          processorOptions: { frameMs: 30 },
        });

        const inputRate = ctx.sampleRate;
        node.port.onmessage = (event: MessageEvent) => {
          const samples = event.data as Float32Array;
          if (!samples || samples.length === 0) return;
          const frame = buildMicFrame(samples, inputRate);
          if (frame) sendBinaryRef.current(frame);
        };

        source.connect(node);
        graph = { stream, ctx, source, node };

        // Arm the turn only once the capture graph is live so no binary frame
        // can precede `voice_start`.
        sendRef.current({
          type: "voice_start",
          window: windowName,
          ts_client: Math.round(performance.now()),
        });
        armed = true;
      } catch (error) {
        if (!cancelled) onErrorRef.current?.(error);
      }
    };

    void start();

    return () => {
      cancelled = true;
      if (armed) {
        sendRef.current({ type: "voice_stop", ts_client: Math.round(performance.now()) });
      }
      if (graph) {
        const { stream, ctx, source, node } = graph;
        try {
          node.port.postMessage({ type: "stop" });
        } catch {
          // port may already be closed
        }
        try {
          source.disconnect();
          node.disconnect();
        } catch {
          // nodes may already be disconnected
        }
        for (const track of stream.getTracks()) track.stop();
        void ctx.close();
        graph = null;
      }
    };
  }, [enabled, windowName]);
}
