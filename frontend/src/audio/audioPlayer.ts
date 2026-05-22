/**
 * audioPlayer — deep module encapsulating Web Audio API for voice playback.
 *
 * Public surface:
 *   - enqueue(pcm, sampleRate, msgId): decode s16le PCM (ArrayBuffer / Float32Array)
 *     and schedule playback gaplessly.
 *   - stop(msgId?): interrupt playback immediately + purge queue (optionally
 *     only for one msgId).
 *   - subscribeSpeaking(cb): observe "speaking" state (msgId or null).
 *
 * Wire format: PCM frames arrive on the WS as **binary** frames (ArrayBuffer).
 * No base64 round-trip — that overhead and the JSON parse cost both went
 * away in the engine rewrite.
 *
 * AudioContext is created lazily on first enqueue to comply with the webview
 * autoplay policy (a user gesture must precede the first play).
 *
 * Scheduling: we track `nextStartTime`. Each chunk is started at
 * `max(ctx.currentTime, nextStartTime)` then we advance `nextStartTime` by the
 * buffer duration. This produces gap-free continuous playback across
 * successive enqueues.
 */

export type SpeakingListener = (speakingMsgId: string | null) => void;

type ScheduledSource = {
  source: AudioBufferSourceNode;
  msgId: string;
  endTime: number;
};

let ctx: AudioContext | null = null;
let nextStartTime = 0;
const scheduled: ScheduledSource[] = [];
const listeners = new Set<SpeakingListener>();
let speakingMsgId: string | null = null;

function getContext(): AudioContext {
  if (!ctx) {
    const Ctor: typeof AudioContext =
      window.AudioContext ??
      (window as unknown as { webkitAudioContext: typeof AudioContext }).webkitAudioContext;
    ctx = new Ctor();
  }
  return ctx;
}

function setSpeakingMsgId(v: string | null): void {
  if (speakingMsgId === v) return;
  speakingMsgId = v;
  for (const l of listeners) l(v);
}

function s16leBytesToFloat32(bytes: ArrayBuffer | Uint8Array): Float32Array {
  // Interpret bytes as Int16 little-endian, normalize to [-1, 1).
  // Accepts either a raw ArrayBuffer (binary WS frame) or a Uint8Array
  // (for callers that already have a view).
  const view =
    bytes instanceof Uint8Array
      ? new DataView(bytes.buffer, bytes.byteOffset, bytes.byteLength)
      : new DataView(bytes);
  const len = view.byteLength >> 1;
  const out = new Float32Array(len);
  for (let i = 0; i < len; i++) {
    out[i] = view.getInt16(i * 2, true) / 32768;
  }
  return out;
}

/**
 * Enqueue a PCM chunk for playback. Accepts:
 *   - ArrayBuffer of s16le mono PCM (binary WS frame), OR
 *   - a pre-decoded Float32Array (used by the dev sine generator).
 */
export function enqueue(pcm: ArrayBuffer | Float32Array, sampleRate: number, msgId: string): void {
  const audioCtx = getContext();
  if (audioCtx.state === "suspended") {
    void audioCtx.resume();
  }

  const float32 = pcm instanceof Float32Array ? pcm : s16leBytesToFloat32(pcm);
  if (float32.length === 0) return;

  const buffer = audioCtx.createBuffer(1, float32.length, sampleRate);
  buffer.copyToChannel(float32, 0, 0);

  const source = audioCtx.createBufferSource();
  source.buffer = buffer;
  source.connect(audioCtx.destination);

  const startAt = Math.max(audioCtx.currentTime, nextStartTime);
  source.start(startAt);
  const endTime = startAt + buffer.duration;
  nextStartTime = endTime;

  const entry: ScheduledSource = { source, msgId, endTime };
  scheduled.push(entry);
  setSpeakingMsgId(msgId);

  source.onended = () => {
    const idx = scheduled.indexOf(entry);
    if (idx >= 0) scheduled.splice(idx, 1);
    if (scheduled.length === 0) {
      nextStartTime = 0;
      setSpeakingMsgId(null);
    } else {
      setSpeakingMsgId(scheduled[0].msgId);
    }
  };
}

/**
 * Stop playback. If `msgId` is provided, only sources tagged with that id are
 * stopped; otherwise all queued/playing sources are cancelled.
 */
export function stop(msgId?: string): void {
  if (!ctx) return;
  const remaining: ScheduledSource[] = [];
  for (const entry of scheduled) {
    if (msgId === undefined || entry.msgId === msgId) {
      try {
        entry.source.onended = null;
        entry.source.stop();
      } catch {
        // Source may already have finished; safe to ignore.
      }
      entry.source.disconnect();
    } else {
      remaining.push(entry);
    }
  }
  scheduled.length = 0;
  for (const r of remaining) scheduled.push(r);

  if (scheduled.length === 0) {
    nextStartTime = 0;
    setSpeakingMsgId(null);
  } else {
    nextStartTime = Math.max(...scheduled.map((s) => s.endTime));
    setSpeakingMsgId(scheduled[0].msgId);
  }
}

export function subscribeSpeaking(cb: SpeakingListener): () => void {
  listeners.add(cb);
  cb(speakingMsgId);
  return () => {
    listeners.delete(cb);
  };
}

export function isSpeaking(): boolean {
  return speakingMsgId !== null;
}

export function getSpeakingMsgId(): string | null {
  return speakingMsgId;
}

/**
 * Generate a 1-second 440Hz sine wave as Float32 PCM at the given sample rate.
 * Dev tool to validate the playback chain in isolation.
 */
export function generateSineFloat32(
  freqHz = 440,
  durationSec = 1,
  sampleRate = 24_000,
): Float32Array {
  const n = Math.floor(sampleRate * durationSec);
  const out = new Float32Array(n);
  const twoPiF = 2 * Math.PI * freqHz;
  for (let i = 0; i < n; i++) {
    const fadeSamples = Math.min(Math.floor(sampleRate * 0.01), Math.floor(n / 2));
    let gain = 0.2;
    if (i < fadeSamples) gain *= i / fadeSamples;
    else if (i > n - fadeSamples) gain *= (n - i) / fadeSamples;
    out[i] = Math.sin((twoPiF * i) / sampleRate) * gain;
  }
  return out;
}
