/**
 * micWorklet — AudioWorkletProcessor for the « Listen » mic capture path.
 *
 * Runs on the audio render thread. It receives mono Float32 input blocks (128
 * samples each, at the AudioContext native rate), buffers them into ~`frameMs`
 * chunks, and posts each chunk to the main thread as a *copied* Float32Array
 * (so the transferred buffer is detached from the render thread). The main-
 * thread `MicCapture` hook then downsamples to 16 kHz, quantises to s16le,
 * tags with `0x01`, and ships it as a binary WS frame.
 *
 * Why buffer here instead of posting every 128-sample block? Posting 128
 * samples ~375×/s at 48 kHz floods the message port; coalescing to ~30 ms
 * frames matches Annexe A.1's 20-40 ms target and the backend's per-frame STT
 * coalescing, at a fraction of the message rate.
 *
 * Served from `public/` so it is reachable at `/micWorklet.js` for
 * `audioWorklet.addModule(...)`. Plain JS (no TS) because the worklet global
 * scope is not part of the app's TS program.
 */

const DEFAULT_FRAME_MS = 30;

class MicCaptureProcessor extends AudioWorkletProcessor {
  constructor(options) {
    super();
    const frameMs = options?.processorOptions?.frameMs || DEFAULT_FRAME_MS;
    // `sampleRate` is a global in the AudioWorkletGlobalScope (the context rate).
    this._frameSize = Math.max(1, Math.round((sampleRate * frameMs) / 1000));
    this._buffer = new Float32Array(this._frameSize);
    this._filled = 0;
    this._stopped = false;
    this.port.onmessage = (event) => {
      if (event.data && event.data.type === "stop") {
        this._stopped = true;
      }
    };
  }

  process(inputs) {
    if (this._stopped) {
      return false; // let the node be GC'd
    }
    const input = inputs[0];
    if (!input || input.length === 0) {
      return true;
    }
    const channel = input[0];
    if (!channel) {
      return true;
    }
    for (let i = 0; i < channel.length; i++) {
      this._buffer[this._filled++] = channel[i];
      if (this._filled === this._frameSize) {
        // Copy so the posted buffer is detached from our reused buffer.
        const frame = this._buffer.slice(0, this._frameSize);
        this.port.postMessage(frame, [frame.buffer]);
        this._filled = 0;
      }
    }
    return true;
  }
}

registerProcessor("mic-capture-processor", MicCaptureProcessor);
