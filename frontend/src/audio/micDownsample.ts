/**
 * micDownsample — pure DSP helpers for the « Listen » mic path (issue 0099).
 *
 * The webview captures mic audio at the AudioContext's native rate (often
 * 48 kHz) as Float32 mono. The backend STT contract (Annexe A.1) is **16 kHz
 * mono s16le**. These helpers do that conversion deterministically so they can
 * be unit-tested without any Web Audio runtime:
 *
 *  - {@link downsampleTo16k} resamples Float32 mono → 16 kHz Float32 mono;
 *  - {@link floatToPcm16} quantises Float32 [-1,1] → s16le bytes;
 *  - {@link encodeMicFrame} prepends the `0x01` mic type tag (Annexe A.1).
 *
 * The resampler is linear interpolation. For voice STT this is more than
 * adequate (whisper is robust to mild resampling artefacts) and it is cheap +
 * dependency-free, which matters in an AudioWorklet hot path.
 */

/** First byte of every binary mic WS frame (Annexe A.1): `0x01` = mic frame. */
export const MIC_FRAME_TAG = 0x01;

/** The STT contract sample rate (Annexe A.1). */
export const TARGET_SAMPLE_RATE = 16_000;

/**
 * Resample a Float32 mono buffer from `inputSampleRate` to 16 kHz via linear
 * interpolation. Returns the input unchanged when already at 16 kHz. An empty
 * input yields an empty output.
 *
 * Upsampling (input < 16 kHz) also works, though the mic path only ever
 * downsamples in practice (48 kHz → 16 kHz).
 */
export function downsampleTo16k(
  input: Float32Array,
  inputSampleRate: number,
  targetRate: number = TARGET_SAMPLE_RATE,
): Float32Array {
  if (input.length === 0) return new Float32Array(0);
  if (inputSampleRate === targetRate) return input;
  if (inputSampleRate <= 0) {
    throw new Error(`invalid inputSampleRate: ${inputSampleRate}`);
  }

  const ratio = inputSampleRate / targetRate;
  const outLength = Math.max(1, Math.round(input.length / ratio));
  const out = new Float32Array(outLength);

  for (let i = 0; i < outLength; i++) {
    const srcPos = i * ratio;
    const i0 = Math.floor(srcPos);
    const i1 = Math.min(i0 + 1, input.length - 1);
    const frac = srcPos - i0;
    out[i] = input[i0] * (1 - frac) + input[i1] * frac;
  }
  return out;
}

/**
 * Quantise a Float32 [-1, 1] buffer to signed 16-bit little-endian PCM bytes.
 * Values outside [-1, 1] are clamped. Returns an `ArrayBuffer` so it can be
 * sent directly (after tagging) on a binary WS frame.
 */
export function floatToPcm16(samples: Float32Array): ArrayBuffer {
  const buffer = new ArrayBuffer(samples.length * 2);
  const view = new DataView(buffer);
  for (let i = 0; i < samples.length; i++) {
    let s = samples[i];
    if (s > 1) s = 1;
    else if (s < -1) s = -1;
    // Match the backend decode: positive full-scale maps to 32767.
    view.setInt16(i * 2, Math.round(s * 32767), true);
  }
  return buffer;
}

/**
 * Build one binary mic WS frame: the `0x01` tag byte followed by the s16le PCM
 * payload (Annexe A.1). `pcm16` is the buffer returned by {@link floatToPcm16}.
 */
export function encodeMicFrame(pcm16: ArrayBuffer): ArrayBuffer {
  const out = new Uint8Array(pcm16.byteLength + 1);
  out[0] = MIC_FRAME_TAG;
  out.set(new Uint8Array(pcm16), 1);
  return out.buffer;
}

/**
 * Convenience: downsample → quantise → tag in one call. Returns the ready-to-
 * send binary frame, or `null` when the input produced no samples.
 */
export function buildMicFrame(input: Float32Array, inputSampleRate: number): ArrayBuffer | null {
  const resampled = downsampleTo16k(input, inputSampleRate);
  if (resampled.length === 0) return null;
  return encodeMicFrame(floatToPcm16(resampled));
}
