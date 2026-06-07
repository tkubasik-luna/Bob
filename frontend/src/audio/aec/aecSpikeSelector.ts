/**
 * aecSpikeSelector — pure path-selector + verdict builder for the AEC spike
 * (issue 0097, PRD 0016 Annexe I, "Spike de dérisquage").
 *
 * Annexe I defines a PASS as the conjunction of three criteria measured in the
 * Tauri v2 webview (WKWebView, macOS):
 *   C1 `getusermedia_stream`   — getUserMedia({audio:{echoCancellation:true}})
 *                                returns an active MediaStream (mic entitlement
 *                                present + WKWebView media-permission delegate
 *                                granting).
 *   C2 `aec_attenuation_25db`  — playing a known TTS during capture, echo is
 *                                attenuated ≥ 25 dB (see aecMeasurement.ts).
 *   C3 `overlap_word_transcribed` — a word spoken over the TTS transcribes
 *                                correctly (sanity that AEC didn't shred speech).
 *
 * AFK auto-fallback rule (no human gate): if ANY criterion fails (or could not
 * be evaluated), the chosen capture path is `rust` — capture + playback + AEC
 * move into Rust (cpal + webrtc-audio-processing), webview becomes UI-only.
 * Only an all-pass selects `webview`.
 *
 * This module is PURE: criteria-results in → (`webview` | `rust`) + verdict
 * JSON out. The empirical measurement that produces the criteria-results lives
 * in `micCapture.ts`; here we only encode the decision, so it is unit-tested
 * with exact in→out assertions. Issue 0099 consumes the chosen path via
 * `captureDecision.ts`.
 */

/** The two capture architectures the PRD considers. */
export type CapturePath = "webview" | "rust";

/** Stable identifiers for the three Annexe-I criteria (used as JSON keys). */
export type SpikeCriterionId =
  | "getusermedia_stream"
  | "aec_attenuation_25db"
  | "overlap_word_transcribed";

export const SPIKE_CRITERION_IDS: readonly SpikeCriterionId[] = [
  "getusermedia_stream",
  "aec_attenuation_25db",
  "overlap_word_transcribed",
];

/**
 * Outcome of evaluating one criterion.
 *
 * `pending` is distinct from `false`: a headless agent cannot empirically
 * measure a real mic, so criteria it could not evaluate are `pending`, NOT a
 * fake pass. For the AFK fallback decision, `pending` is treated exactly like a
 * fail (anything short of a confirmed pass → Rust) — but the verdict preserves
 * the distinction so a human/device follow-up can tell "measured-and-failed"
 * apart from "not-yet-measured-on-hardware".
 */
export type CriterionStatus = "pass" | "fail" | "pending";

export interface CriterionResult {
  id: SpikeCriterionId;
  status: CriterionStatus;
  /** Optional measured figure (e.g. attenuation dB) for the verdict / debugging. */
  measured?: number;
  /** Human-readable note (why it failed, what was measured, follow-up needed). */
  detail?: string;
}

export interface SpikeVerdict {
  /** Schema tag so consumers (issue 0099) can decode defensively. */
  schema_version: 1;
  /** ISO-8601 timestamp the verdict was produced. */
  produced_at: string;
  /** `true` only if every criterion passed. */
  ok: boolean;
  /** The auto-selected capture path. `webview` iff `ok`, else `rust`. */
  chosen_path: CapturePath;
  /** Per-criterion breakdown, in {@link SPIKE_CRITERION_IDS} order. */
  criteria: CriterionResult[];
  /**
   * `true` when the verdict was produced without a real on-device measurement
   * (one or more criteria are `pending`). The empirical hardware PASS (≥25 dB
   * on a real mic) is a follow-up human/device step; this flag makes that
   * explicit and prevents a scaffolded verdict from masquerading as a measured
   * one.
   */
  hardware_pending: boolean;
}

/**
 * Select the capture path from criteria-results (AFK rule).
 *
 * Returns `webview` ONLY when every listed criterion has status `pass` and all
 * three criteria are present; any `fail`, any `pending`, or any missing
 * criterion yields `rust`. Order-independent.
 */
export function selectCapturePath(results: readonly CriterionResult[]): CapturePath {
  const byId = new Map(results.map((r) => [r.id, r.status]));
  for (const id of SPIKE_CRITERION_IDS) {
    if (byId.get(id) !== "pass") return "rust";
  }
  return "webview";
}

/** True iff every criterion is present and `pass`. */
export function allCriteriaPass(results: readonly CriterionResult[]): boolean {
  return selectCapturePath(results) === "webview";
}

/** True iff at least one criterion is `pending` (no real hardware measurement yet). */
export function hasPendingCriteria(results: readonly CriterionResult[]): boolean {
  return results.some((r) => r.status === "pending");
}

/**
 * Build the spike verdict JSON from criteria-results.
 *
 * Pure: the only non-determinism is the timestamp, which the caller may pin via
 * `now` for reproducible tests. The criteria are normalised into
 * {@link SPIKE_CRITERION_IDS} order, and any criterion absent from `results` is
 * materialised as `pending` (so the verdict always lists all three).
 */
export function buildSpikeVerdict(
  results: readonly CriterionResult[],
  now: Date = new Date(),
): SpikeVerdict {
  const byId = new Map(results.map((r) => [r.id, r]));
  const criteria: CriterionResult[] = SPIKE_CRITERION_IDS.map(
    (id) => byId.get(id) ?? { id, status: "pending", detail: "not evaluated" },
  );
  const chosen_path = selectCapturePath(criteria);
  return {
    schema_version: 1,
    produced_at: now.toISOString(),
    ok: chosen_path === "webview",
    chosen_path,
    criteria,
    hardware_pending: hasPendingCriteria(criteria),
  };
}
