import { describe, expect, test } from "vitest";
import {
  AEC_ATTENUATION_THRESHOLD_DB,
  MAX_ATTENUATION_DB,
  amplitudeRatioToDb,
  computeEchoAttenuationDb,
  passesAttenuationThreshold,
  rms,
} from "./aecMeasurement";

/** Build a constant-amplitude buffer of `n` samples at value `a`. */
function constBuffer(a: number, n: number): Float32Array {
  return Float32Array.from({ length: n }, () => a);
}

describe("rms", () => {
  test("empty buffer has zero energy (no NaN)", () => {
    expect(rms(new Float32Array(0))).toBe(0);
  });

  test("constant amplitude buffer RMS equals |amplitude|", () => {
    expect(rms(constBuffer(0.5, 1000))).toBeCloseTo(0.5, 12);
    expect(rms(constBuffer(-0.5, 1000))).toBeCloseTo(0.5, 12);
  });

  test("RMS of a full-scale square wave is 1", () => {
    const buf = Float32Array.from({ length: 1000 }, (_, i) => (i % 2 === 0 ? 1 : -1));
    expect(rms(buf)).toBeCloseTo(1, 12);
  });

  test("non-finite samples are treated as silence", () => {
    const buf = Float32Array.from([0.5, Number.NaN, 0.5, Number.POSITIVE_INFINITY]);
    // Only the two 0.5 samples contribute: sqrt((0.25+0.25)/4) = sqrt(0.125).
    expect(rms(buf)).toBeCloseTo(Math.sqrt(0.125), 12);
  });
});

describe("amplitudeRatioToDb", () => {
  test("ratio of 1 is 0 dB", () => {
    expect(amplitudeRatioToDb(0.3, 0.3)).toBeCloseTo(0, 12);
  });

  test("a 10x amplitude ratio is +20 dB", () => {
    expect(amplitudeRatioToDb(1, 0.1)).toBeCloseTo(20, 12);
  });

  test("a ~17.8x amplitude ratio is ~25 dB (the threshold)", () => {
    // 25 dB → ratio = 10^(25/20) ≈ 17.7828.
    const ratio = 10 ** (25 / 20);
    expect(amplitudeRatioToDb(ratio, 1)).toBeCloseTo(25, 6);
  });

  test("zero/negative denominator clamps to MAX (fully cancelled residual)", () => {
    expect(amplitudeRatioToDb(0.5, 0)).toBe(MAX_ATTENUATION_DB);
    expect(amplitudeRatioToDb(0.5, -1)).toBe(MAX_ATTENUATION_DB);
  });

  test("zero numerator (no reference energy) reports 0 dB, never a silent pass", () => {
    expect(amplitudeRatioToDb(0, 0.1)).toBe(0);
  });
});

describe("computeEchoAttenuationDb", () => {
  test("residual at floor → MAX attenuation (perfect cancellation)", () => {
    // Captured-with-reference equals the silence floor → residual power 0.
    const db = computeEchoAttenuationDb({
      referencePlaybackRms: 0.5,
      capturedWithReferenceRms: 0.01,
      capturedSilenceRms: 0.01,
    });
    expect(db).toBe(MAX_ATTENUATION_DB);
  });

  test("known residual yields the textbook dB (floor-subtracted)", () => {
    // Floor 0 → residual = captured = 0.05; reference 0.5 → 20log10(10) = 20 dB.
    const db = computeEchoAttenuationDb({
      referencePlaybackRms: 0.5,
      capturedWithReferenceRms: 0.05,
      capturedSilenceRms: 0,
    });
    expect(db).toBeCloseTo(20, 9);
  });

  test("ambient floor is removed in the power domain (quiet room can't inflate)", () => {
    // captured² = 0.05² = 0.0025; floor² = 0.03² = 0.0009; residual = sqrt(0.0016)=0.04.
    // reference 0.4 → 20log10(0.4/0.04) = 20 dB.
    const db = computeEchoAttenuationDb({
      referencePlaybackRms: 0.4,
      capturedWithReferenceRms: 0.05,
      capturedSilenceRms: 0.03,
    });
    expect(db).toBeCloseTo(20, 9);
  });

  test("captured below floor (jitter) clamps residual to 0 → MAX", () => {
    const db = computeEchoAttenuationDb({
      referencePlaybackRms: 0.5,
      capturedWithReferenceRms: 0.01,
      capturedSilenceRms: 0.02,
    });
    expect(db).toBe(MAX_ATTENUATION_DB);
  });

  test("weak cancellation (residual close to reference) is a low dB", () => {
    // residual 0.4 vs reference 0.5 → 20log10(1.25) ≈ 1.94 dB.
    const db = computeEchoAttenuationDb({
      referencePlaybackRms: 0.5,
      capturedWithReferenceRms: 0.4,
      capturedSilenceRms: 0,
    });
    expect(db).toBeCloseTo(1.938, 2);
    expect(passesAttenuationThreshold(db)).toBe(false);
  });
});

describe("passesAttenuationThreshold", () => {
  test("threshold default is 25 dB", () => {
    expect(AEC_ATTENUATION_THRESHOLD_DB).toBe(25);
  });

  test("exactly 25 dB passes, just under fails", () => {
    expect(passesAttenuationThreshold(25)).toBe(true);
    expect(passesAttenuationThreshold(24.999)).toBe(false);
  });

  test("non-finite never passes", () => {
    expect(passesAttenuationThreshold(Number.NaN)).toBe(false);
    expect(passesAttenuationThreshold(Number.POSITIVE_INFINITY)).toBe(false);
  });

  test("custom threshold is honoured", () => {
    expect(passesAttenuationThreshold(30, 35)).toBe(false);
    expect(passesAttenuationThreshold(40, 35)).toBe(true);
  });
});
