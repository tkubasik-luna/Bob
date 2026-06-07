import { describe, expect, test } from "vitest";
import {
  MIC_FRAME_TAG,
  buildMicFrame,
  downsampleTo16k,
  encodeMicFrame,
  floatToPcm16,
} from "./micDownsample";

describe("downsampleTo16k", () => {
  test("48 kHz → 16 kHz reduces length ~3×", () => {
    const input = new Float32Array(4800); // 100 ms @ 48 kHz
    const out = downsampleTo16k(input, 48_000);
    // 100 ms @ 16 kHz = 1600 samples.
    expect(out.length).toBe(1600);
  });

  test("44.1 kHz → 16 kHz produces the rounded target count", () => {
    const input = new Float32Array(44_100); // 1 s @ 44.1 kHz
    const out = downsampleTo16k(input, 44_100);
    expect(out.length).toBe(16_000);
  });

  test("no-op when already 16 kHz (returns same instance)", () => {
    const input = new Float32Array(160);
    const out = downsampleTo16k(input, 16_000);
    expect(out).toBe(input);
  });

  test("empty input yields empty output", () => {
    expect(downsampleTo16k(new Float32Array(0), 48_000).length).toBe(0);
  });

  test("preserves a constant DC level through resampling", () => {
    const input = new Float32Array(4800).fill(0.5);
    const out = downsampleTo16k(input, 48_000);
    for (const s of out) expect(s).toBeCloseTo(0.5, 5);
  });

  test("linearly interpolates a ramp (midpoint value sane)", () => {
    // Ramp 0..1 over 6 samples at 6 Hz -> resample to 3 Hz (3 samples).
    const input = Float32Array.from([0, 0.2, 0.4, 0.6, 0.8, 1.0]);
    const out = downsampleTo16k(input, 6, 3);
    expect(out.length).toBe(3);
    // First output sample maps to source index 0.
    expect(out[0]).toBeCloseTo(0, 5);
    // Monotonic increasing (no overshoot beyond input range).
    expect(out[1]).toBeGreaterThan(out[0]);
    expect(out[2]).toBeGreaterThan(out[1]);
    expect(out[2]).toBeLessThanOrEqual(1);
  });

  test("throws on non-positive input rate", () => {
    expect(() => downsampleTo16k(new Float32Array(10), 0)).toThrow();
  });
});

describe("floatToPcm16", () => {
  test("maps full-scale floats to s16le extremes and clamps", () => {
    const pcm = floatToPcm16(Float32Array.from([0, 1, -1, 2, -2]));
    const view = new DataView(pcm);
    expect(view.byteLength).toBe(10);
    expect(view.getInt16(0, true)).toBe(0);
    expect(view.getInt16(2, true)).toBe(32767);
    expect(view.getInt16(4, true)).toBe(-32767);
    // Clamped above/below [-1, 1].
    expect(view.getInt16(6, true)).toBe(32767);
    expect(view.getInt16(8, true)).toBe(-32767);
  });

  test("is little-endian", () => {
    // 0.5 * 32767 ≈ 16384 (0x4000). LE bytes: 0x00, 0x40.
    const pcm = floatToPcm16(Float32Array.from([0.5]));
    const bytes = new Uint8Array(pcm);
    expect(bytes[0]).toBe(0x00);
    expect(bytes[1]).toBe(0x40);
  });
});

describe("encodeMicFrame / buildMicFrame", () => {
  test("prepends the 0x01 mic tag", () => {
    const pcm = floatToPcm16(Float32Array.from([0, 0]));
    const frame = new Uint8Array(encodeMicFrame(pcm));
    expect(frame[0]).toBe(MIC_FRAME_TAG);
    expect(frame.byteLength).toBe(pcm.byteLength + 1);
  });

  test("buildMicFrame: 48 kHz 30 ms block → tagged 16 kHz frame", () => {
    const input = new Float32Array(1440).fill(0.1); // 30 ms @ 48 kHz
    const frame = buildMicFrame(input, 48_000);
    expect(frame).not.toBeNull();
    const bytes = new Uint8Array(frame as ArrayBuffer);
    expect(bytes[0]).toBe(MIC_FRAME_TAG);
    // 30 ms @ 16 kHz = 480 samples * 2 bytes + 1 tag byte = 961.
    expect(bytes.byteLength).toBe(480 * 2 + 1);
  });

  test("buildMicFrame returns null for empty input", () => {
    expect(buildMicFrame(new Float32Array(0), 48_000)).toBeNull();
  });
});
