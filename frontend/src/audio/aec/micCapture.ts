/**
 * micCapture — the webview-native capture path + reproducible AEC measurement
 * harness for the spike (issue 0097, PRD 0016 "Pipeline audio & STT" / Annexe I).
 *
 * This is the LIVE side of the spike: it actually calls
 * `getUserMedia({ audio: { echoCancellation: true } })` in the Tauri v2
 * webview, plays a known reference (TTS/WAV) through `audioPlayer`, captures the
 * mic for two windows, and feeds the measured RMS values into the PURE math in
 * `aecMeasurement.ts` to produce the dB attenuation that Annexe I criterion 2
 * gates on. The deterministic core (dB math, path selection, verdict shape)
 * lives in sibling pure modules and is unit-tested; this orchestration depends
 * on real hardware (mic + speakers) and therefore is exercised on-device, not
 * in vitest.
 *
 * Native prerequisites for the `getUserMedia` call to succeed on macOS:
 *   1. `NSMicrophoneUsageDescription` in the bundled Info.plist
 *      (`frontend/src-tauri/Info.plist`) — without it macOS terminates the app
 *      on first mic access.
 *   2. The WKWebView media-permission delegate. wry 0.55.1 (this app's webview)
 *      already implements `webView:requestMediaCapturePermissionForOrigin:…:
 *      decisionHandler:` and calls it with `WKPermissionDecision::Grant`, so the
 *      prompt is granted by the framework. The Rust seam in
 *      `src-tauri/src/aec_spike.rs` documents this and is where a manual
 *      override would live if a future webview version regresses.
 *
 * AEC ownership: per the PRD, the WKWebView cancels its own output, so the
 * reference we play through `audioPlayer` (→ system output) is what the canceller
 * removes from the mic input. Hence the measurement compares the played-back
 * reference level against the residual that survives in the captured stream.
 */

import { enqueue, stop } from "../audioPlayer";
import { type EchoMeasurement, computeEchoAttenuationDb, rms } from "./aecMeasurement";

/** Sample rate the spike captures/measures at (mono). PRD downsample target. */
export const CAPTURE_SAMPLE_RATE = 16_000;

/** Default duration of each measurement window. */
const DEFAULT_WINDOW_MS = 1_500;

/** Outcome of attempting to open the mic. Mirrors what Annexe I criterion 1 needs. */
export interface OpenMicResult {
  stream: MediaStream;
  /** `true` if the stream has at least one live (`readyState === "live"`) audio track. */
  active: boolean;
}

/**
 * Open the mic with echo cancellation requested.
 *
 * This is the literal Annexe-I-criterion-1 call. Throws if `getUserMedia` is
 * unavailable (non-webview/test env) or the user/OS denies — callers map a
 * throw or an inactive stream to a `fail` criterion result.
 */
export async function openEchoCancelledMic(): Promise<OpenMicResult> {
  const md = navigator.mediaDevices;
  if (!md?.getUserMedia) {
    throw new Error("getUserMedia unavailable (not a media-capable webview)");
  }
  const stream = await md.getUserMedia({
    audio: {
      echoCancellation: true,
      // Request a mono, 16 kHz-ish capture to match the downstream pipeline.
      // These are hints; the AudioWorklet in issue 0099 does the authoritative
      // downsample. We keep them here so the spike measures a representative path.
      noiseSuppression: false,
      autoGainControl: false,
      channelCount: 1,
      sampleRate: CAPTURE_SAMPLE_RATE,
    },
  });
  const active = stream.getAudioTracks().some((t) => t.readyState === "live");
  return { stream, active };
}

/** Stop every track on a stream (releases the mic, drops the OS indicator). */
export function closeMic(stream: MediaStream): void {
  for (const track of stream.getTracks()) track.stop();
}

/**
 * Capture the mic into a single Float32 buffer for `windowMs`, via an
 * AudioContext + AnalyserNode (time-domain). Returns the concatenated samples.
 *
 * Implemented with the same Web Audio primitives `audioPlayer`/`useAudioLevel`
 * use, so it shares the webview's audio graph behaviour. The buffer is what we
 * RMS in the harness.
 */
async function captureWindow(stream: MediaStream, windowMs: number): Promise<Float32Array> {
  const Ctor: typeof AudioContext =
    window.AudioContext ??
    (window as unknown as { webkitAudioContext: typeof AudioContext }).webkitAudioContext;
  const ctx = new Ctor();
  try {
    const source = ctx.createMediaStreamSource(stream);
    const analyser = ctx.createAnalyser();
    analyser.fftSize = 2048;
    source.connect(analyser);

    const collected: number[] = [];
    const frame = new Float32Array(analyser.fftSize);
    const deadline = performance.now() + windowMs;
    // Poll the time-domain data at the analyser's natural cadence until the
    // window elapses. Real-frame data; no synthetic samples.
    while (performance.now() < deadline) {
      analyser.getFloatTimeDomainData(frame);
      for (let i = 0; i < frame.length; i++) collected.push(frame[i]);
      await new Promise((r) => setTimeout(r, 20));
    }
    return Float32Array.from(collected);
  } finally {
    void ctx.close();
  }
}

/** A reference signal to play during the echo window, as s16le PCM + its sample rate. */
export interface ReferenceSignal {
  pcm: ArrayBuffer | Float32Array;
  sampleRate: number;
  /** RMS of the reference at the level it is played (full-scale, pre-room). */
  playbackRms: number;
}

export interface MeasureEchoOptions {
  windowMs?: number;
  /** Injected for tests; defaults to the real `captureWindow`. */
  capture?: (stream: MediaStream, windowMs: number) => Promise<Float32Array>;
  /** Injected for tests; defaults to `audioPlayer.enqueue`. */
  playReference?: (signal: ReferenceSignal) => void;
}

/**
 * Reproducible echo-attenuation measurement.
 *
 * Sequence:
 *   1. Capture a SILENCE window (reference muted) → ambient/self-noise floor.
 *   2. Play the reference and capture a REFERENCE window → residual echo + noise.
 *   3. Feed the two captured RMS values + the known `playbackRms` into the pure
 *      `computeEchoAttenuationDb`.
 *
 * Returns the attenuation in dB. Deterministic given the same captured buffers
 * — which is why the capture and playback are injectable: a test can drive
 * fixture buffers through the SAME path and assert the exact dB, while on-device
 * the real Web Audio capture is used. The pure math is additionally unit-tested
 * directly in `aecMeasurement.test.ts`.
 */
export async function measureEchoAttenuationDb(
  stream: MediaStream,
  reference: ReferenceSignal,
  opts: MeasureEchoOptions = {},
): Promise<number> {
  const windowMs = opts.windowMs ?? DEFAULT_WINDOW_MS;
  const capture = opts.capture ?? captureWindow;
  const play =
    opts.playReference ??
    ((sig: ReferenceSignal) => enqueue(sig.pcm, sig.sampleRate, "aec-spike-reference"));

  // 1. Floor (reference muted).
  const silence = await capture(stream, windowMs);

  // 2. Reference playing.
  play(reference);
  const withReference = await capture(stream, windowMs);
  stop("aec-spike-reference");

  const measurement: EchoMeasurement = {
    referencePlaybackRms: reference.playbackRms,
    capturedWithReferenceRms: rms(withReference),
    capturedSilenceRms: rms(silence),
  };
  return computeEchoAttenuationDb(measurement);
}
