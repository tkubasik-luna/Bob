/**
 * audioPlayer — deep module encapsulating Web Audio API for voice playback.
 *
 * Public surface:
 *   - enqueue(pcmB64, sampleRate, msgId): decode base64 PCM and schedule playback gaplessly
 *   - stop(msgId?): interrupt playback immediately + purge queue (optionally only for one msgId)
 *   - subscribeSpeaking(cb): observe "speaking" state (true while any source is playing)
 *
 * PCM format choice: 16-bit signed little-endian, mono. Most TTS engines (incl. Kokoro)
 * emit s16le easily; we convert to float32 in [-1, 1] for AudioBuffer.
 *
 * AudioContext is created lazily on first enqueue (or first explicit resume) to comply
 * with the webview autoplay policy (a user gesture must precede the first play).
 *
 * Scheduling: we track `nextStartTime`. Each chunk is started at
 * `max(ctx.currentTime, nextStartTime)` then we advance `nextStartTime` by the buffer
 * duration. This produces gap-free continuous playback across successive enqueues.
 */

/**
 * Listener signature for the speaking observer.
 *
 * The argument is the `msg_id` of the bubble currently being played, or
 * `null` when nothing is playing. Consumers (typically the chat store) use
 * this to render a "Bob is speaking" indicator on the exact bubble being
 * voiced, and to clear it on natural end OR on interruption.
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
    // Let the context use its native rate; we resample by creating AudioBuffer at the
    // chunk's source sample rate and letting Web Audio resample on playback.
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

function base64ToUint8(b64: string): Uint8Array {
  const bin = atob(b64);
  const out = new Uint8Array(bin.length);
  for (let i = 0; i < bin.length; i++) out[i] = bin.charCodeAt(i);
  return out;
}

function s16leToFloat32(bytes: Uint8Array): Float32Array {
  // Interpret bytes as Int16 little-endian, normalize to [-1, 1).
  const len = bytes.byteLength >> 1;
  const view = new DataView(bytes.buffer, bytes.byteOffset, bytes.byteLength);
  const out = new Float32Array(len);
  for (let i = 0; i < len; i++) {
    out[i] = view.getInt16(i * 2, true) / 32768;
  }
  return out;
}

/**
 * Enqueue a PCM chunk for playback. Accepts:
 *   - base64-encoded s16le mono PCM (default path), OR
 *   - a pre-decoded Float32Array (used by the dev sine generator to skip encoding round-trip).
 */
export function enqueue(pcm: string | Float32Array, sampleRate: number, msgId: string): void {
  const audioCtx = getContext();
  // Resume if suspended (browser may have suspended a fresh context until a gesture).
  if (audioCtx.state === "suspended") {
    void audioCtx.resume();
  }

  const float32 = typeof pcm === "string" ? s16leToFloat32(base64ToUint8(pcm)) : pcm;
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
  // The msg_id "currently speaking" is whichever bubble owns the head of
  // the schedule. Streaming chunks for the same msg_id arrive in order, so
  // setting it on every enqueue is correct and idempotent.
  setSpeakingMsgId(msgId);

  source.onended = () => {
    const idx = scheduled.indexOf(entry);
    if (idx >= 0) scheduled.splice(idx, 1);
    if (scheduled.length === 0) {
      nextStartTime = 0;
      setSpeakingMsgId(null);
    } else {
      // Still playing — keep the msg_id of whatever is at the head of the
      // remaining schedule. In practice all queued chunks belong to the
      // same turn, so this just keeps the current id.
      setSpeakingMsgId(scheduled[0].msgId);
    }
  };
}

/**
 * Stop playback. If `msgId` is provided, only sources tagged with that id are stopped;
 * otherwise all queued/playing sources are cancelled and the schedule resets.
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
    // Recompute nextStartTime from what's still scheduled.
    nextStartTime = Math.max(...scheduled.map((s) => s.endTime));
    setSpeakingMsgId(scheduled[0].msgId);
  }
}

export function subscribeSpeaking(cb: SpeakingListener): () => void {
  listeners.add(cb);
  // Fire immediately with current state so subscribers can sync.
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
 * Used by the dev/temporary test button to validate the playback chain in isolation.
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
    // Gentle fade in/out (10ms) to avoid click artifacts and Web Audio glitch warnings.
    const fadeSamples = Math.min(Math.floor(sampleRate * 0.01), Math.floor(n / 2));
    let gain = 0.2;
    if (i < fadeSamples) gain *= i / fadeSamples;
    else if (i > n - fadeSamples) gain *= (n - i) / fadeSamples;
    out[i] = Math.sin((twoPiF * i) / sampleRate) * gain;
  }
  return out;
}
