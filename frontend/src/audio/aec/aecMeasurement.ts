/**
 * aecMeasurement — pure, deterministic DSP helpers for the AEC spike (issue 0097,
 * PRD 0016 Annexe I criterion 2).
 *
 * The spike plays a known TTS reference through the speakers while the mic
 * captures. If the webview's echo canceller works, the reference signal is
 * strongly attenuated in the captured mic stream. This module turns two
 * captured energy measurements into an **echo-attenuation figure in dB** —
 * the number Annexe I gates on (≥ 25 dB).
 *
 * EVERYTHING HERE IS PURE. No Web Audio, no `getUserMedia`, no timers. The
 * orchestration that actually captures real buffers lives in `micCapture.ts`;
 * this file only does the math, so it can be unit-tested on fixture buffers
 * with exact in→out assertions (PRD testing decision: unit tests on all
 * modules). `micCapture` measures two windows and hands the RMS values here.
 *
 * Measurement model
 * -----------------
 * Two capture windows of the SAME duration:
 *   1. `reference` window — the TTS reference plays at a known level, mic
 *      open, AEC ON. The residual echo that survives cancellation lands here.
 *   2. `silence` window — the reference is NOT playing (or muted). This is the
 *      ambient/self-noise floor of the mic in the room.
 *
 * We also know the `referencePlaybackRms` — the RMS of the reference signal as
 * it was sent to the speakers (full-level, pre-room). Echo attenuation is how
 * far below the played-back reference the residual echo sits:
 *
 *     attenuation_dB = 20 * log10(referencePlaybackRms / residualEchoRms)
 *
 * where `residualEchoRms` is the echo energy in the reference window with the
 * ambient floor removed in the power domain (so a quiet room doesn't inflate
 * the score):
 *
 *     residualEchoRms = sqrt(max(refWindowRms² − silenceWindowRms², 0))
 *
 * A larger number = better cancellation. We clamp to a finite ceiling so a
 * perfectly-cancelled (or all-zero) residual reports a large-but-finite dB
 * rather than `Infinity`, which keeps the verdict JSON serialisable and the
 * selector comparisons total.
 */

/** dB value reported when the residual echo is at (or below) the noise floor. */
export const MAX_ATTENUATION_DB = 120;

/** Annexe I criterion 2 threshold: echo must be attenuated at least this much. */
export const AEC_ATTENUATION_THRESHOLD_DB = 25;

/**
 * Root-mean-square of a PCM buffer expressed as normalised floats in [-1, 1].
 *
 * Deterministic and total: an empty buffer has no energy, so RMS is 0 (rather
 * than NaN from a 0/0). Non-finite samples (a misbehaving capture stub) are
 * treated as silence so the figure stays well-defined.
 */
export function rms(samples: Float32Array | readonly number[]): number {
  const n = samples.length;
  if (n === 0) return 0;
  let sumSq = 0;
  for (let i = 0; i < n; i++) {
    const v = samples[i];
    if (Number.isFinite(v)) sumSq += v * v;
  }
  return Math.sqrt(sumSq / n);
}

/** Convert a linear amplitude ratio to decibels, clamped to {@link MAX_ATTENUATION_DB}. */
export function amplitudeRatioToDb(numerator: number, denominator: number): number {
  // Denominator at/below zero means the residual is indistinguishable from
  // nothing → treat as full (clamped) attenuation rather than dividing by zero.
  if (!(denominator > 0) || !Number.isFinite(denominator)) {
    return MAX_ATTENUATION_DB;
  }
  if (!(numerator > 0) || !Number.isFinite(numerator)) {
    // No reference energy at all → not a meaningful measurement; report 0 dB
    // (no attenuation demonstrated) so the criterion fails loudly rather than
    // silently passing.
    return 0;
  }
  const db = 20 * Math.log10(numerator / denominator);
  if (!Number.isFinite(db)) return MAX_ATTENUATION_DB;
  return Math.min(db, MAX_ATTENUATION_DB);
}

/** Inputs to {@link computeEchoAttenuationDb}, all in normalised-float RMS units. */
export interface EchoMeasurement {
  /** RMS of the reference signal as played to the speakers (full level). */
  referencePlaybackRms: number;
  /** RMS captured by the mic WHILE the reference played, AEC ON (residual echo + noise). */
  capturedWithReferenceRms: number;
  /** RMS captured by the mic with the reference muted (ambient/self-noise floor). */
  capturedSilenceRms: number;
}

/**
 * Compute echo attenuation in dB from three RMS measurements.
 *
 * Pure and deterministic — identical inputs always yield an identical number.
 * The ambient floor is subtracted in the POWER domain before taking the ratio,
 * so a quiet room cannot inflate the score, and a captured window quieter than
 * the floor (sampling jitter) clamps the residual to 0 → {@link MAX_ATTENUATION_DB}.
 */
export function computeEchoAttenuationDb(m: EchoMeasurement): number {
  const refWindowPower = m.capturedWithReferenceRms * m.capturedWithReferenceRms;
  const floorPower = m.capturedSilenceRms * m.capturedSilenceRms;
  const residualPower = Math.max(refWindowPower - floorPower, 0);
  const residualEchoRms = Math.sqrt(residualPower);
  return amplitudeRatioToDb(m.referencePlaybackRms, residualEchoRms);
}

/** Whether a measured attenuation passes the Annexe I ≥ 25 dB gate. */
export function passesAttenuationThreshold(
  attenuationDb: number,
  thresholdDb: number = AEC_ATTENUATION_THRESHOLD_DB,
): boolean {
  return Number.isFinite(attenuationDb) && attenuationDb >= thresholdDb;
}
