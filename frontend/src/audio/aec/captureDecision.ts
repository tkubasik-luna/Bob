/**
 * captureDecision — the AEC-spike **decision artefact** (issue 0097 / PRD 0016
 * Annexe I), the single programmatic source of truth for *which capture source
 * downstream wiring must use*. Consumed by issue 0099 (frontend mic capture).
 *
 * How issue 0099 reads the decision (relative import — the repo has no path
 * alias):
 *
 *     import { getCaptureDecision } from "../audio/aec/captureDecision";
 *     const { path } = getCaptureDecision();
 *     if (path === "webview") {
 *       // getUserMedia + AudioWorklet capture in the webview
 *     } else {
 *       // webview = UI only; invoke the Rust cpal capture command
 *     }
 *
 * Why a frontend constant (not only a JSON file)?
 * -----------------------------------------------
 * The consumer (0099 `MicCapture`) is frontend TypeScript and must branch at
 * MODULE-LOAD time to decide whether to even call `getUserMedia`. A compiled-in
 * constant gives 0099 a synchronous, type-safe, bundler-visible read with no IO
 * and no race against backend boot. The spike ALSO writes a machine-readable
 * verdict JSON to disk (see `writeVerdictArtefact` doc below + the Rust
 * `aec_spike_write_verdict` command) for the human/device follow-up and for any
 * out-of-process consumer; this constant is the canonical in-app read path and
 * MUST be kept in sync with that verdict's `chosen_path`.
 *
 * Default value & the reality constraint
 * ---------------------------------------
 * A headless agent cannot empirically measure ≥25 dB AEC on a real mic, so the
 * spike's criteria C2/C3 land `pending` until a human runs the on-device step.
 * Per the AFK auto-fallback rule (`selectCapturePath`: anything short of an
 * all-pass → `rust`), the deterministic default committed here is therefore
 * **`rust`** — the safe, no-regression fallback. When the on-device run
 * confirms the webview path (≥25 dB + word transcribed), flip
 * `DEFAULT_CAPTURE_DECISION.path` to `"webview"` (and the persisted verdict will
 * agree). This default is itself derived by feeding the scaffolded
 * criteria-results through the real selector — it is NOT a hand-picked guess.
 */

import { type CapturePath, type CriterionResult, selectCapturePath } from "./aecSpikeSelector";

/**
 * The scaffolded criteria-results as they stand from a headless (no-hardware)
 * spike run. C1 is provably wired (entitlement in Info.plist + wry's built-in
 * WKWebView media-permission delegate grants it — see `micCapture.ts` and the
 * Rust seam), but cannot be *empirically confirmed* without launching the
 * bundled app on a device, so it too is `pending` here. C2/C3 need a real mic.
 *
 * Exported so the default decision below is derived from it through the SAME
 * pure selector the runtime uses — no divergence between the documented rule
 * and the committed default.
 */
export const SCAFFOLDED_SPIKE_RESULTS: readonly CriterionResult[] = [
  {
    id: "getusermedia_stream",
    status: "pending",
    detail:
      "Wired: NSMicrophoneUsageDescription in src-tauri/Info.plist + wry 0.55.1 grants " +
      "requestMediaCapturePermissionForOrigin. Empirical confirmation needs the bundled app on device.",
  },
  {
    id: "aec_attenuation_25db",
    status: "pending",
    detail: "Requires a real mic + speaker loop to measure ≥25 dB (human/device follow-up).",
  },
  {
    id: "overlap_word_transcribed",
    status: "pending",
    detail:
      "Requires speaking a word over the TTS on a real device + STT (human/device follow-up).",
  },
];

export interface CaptureDecision {
  /** Schema tag for defensive decoding by 0099 / out-of-process readers. */
  schemaVersion: 1;
  /** The capture source downstream wiring must use. */
  path: CapturePath;
  /**
   * `true` when `path` was chosen WITHOUT a real on-device measurement (the
   * deterministic AFK default). Issue 0099 may surface this so the operator
   * knows the spike's hardware step is still outstanding.
   */
  hardwarePending: boolean;
}

/**
 * The committed, deterministic decision. `path` is derived from
 * {@link SCAFFOLDED_SPIKE_RESULTS} via {@link selectCapturePath} so it can never
 * silently disagree with the documented AFK rule.
 */
export const DEFAULT_CAPTURE_DECISION: CaptureDecision = {
  schemaVersion: 1,
  path: selectCapturePath(SCAFFOLDED_SPIKE_RESULTS),
  hardwarePending: true,
};

/**
 * Canonical read path for downstream consumers (issue 0099).
 *
 * Returns the in-app decision. Kept as a function (not a bare export of the
 * const) so a future iteration can layer a persisted-verdict override on top
 * (read the disk JSON, fall back to the constant) without changing call sites.
 */
export function getCaptureDecision(): CaptureDecision {
  return DEFAULT_CAPTURE_DECISION;
}

/**
 * Filename of the on-disk spike verdict (written by the Rust
 * `aec_spike_write_verdict` command under BOB_DATA_DIR, mirroring the
 * `llm_selection.json` convention). Out-of-process / follow-up tooling reads
 * this; the in-app canonical read is {@link getCaptureDecision}.
 */
export const VERDICT_ARTEFACT_FILENAME = "aec_spike_verdict.json";

/* ────────────────────────────────────────────────────────────────────────────
 * Degraded-mode spec — runtime half-duplex gate (PRD 0016 Annexe G, row
 * "AEC échoue runtime (webview)"). CONSUMED BY ISSUE 0101.
 *
 * This is a SPECIFICATION (doc + the typed constant below), not the runtime
 * implementation — implementing the gate is issue 0101's job. It is the net
 * for the case where AEC was accepted at spike time but degrades at runtime
 * (echo reappears, e.g. user switches to laptop speakers at high volume).
 *
 * Contract for 0101:
 *   • Detection: echo re-detected at runtime (e.g. the same residual-dB
 *     measurement from `aecMeasurement.ts`, run live, drops below
 *     AEC_ATTENUATION_THRESHOLD_DB), OR a manual user toggle.
 *   • Behaviour: enter HALF-DUPLEX — while the TurnFsm is in `bob_speaking`,
 *     MUTE the mic capture (stop forwarding PCM frames upstream / disable the
 *     AudioWorklet sink). Un-mute on the transition out of `bob_speaking`
 *     (`tts_end` → idle, or a confirmed barge-in window — barge-in itself must
 *     still be detectable, so 0101 must decide whether to keep a low-rate VAD
 *     tap alive; default: simplest correct = full mute during `bob_speaking`,
 *     accepting that barge-in falls back to a UI control in degraded mode).
 *   • Surface: a visible flag in the HUD (the PRD's "flag visible") + a
 *     `voice`-category event at severity `warn` so the Debug View and the
 *     attestation harness can assert the degradation.
 *   • Reversibility: degradation is sticky for the session unless the user
 *     re-enables full-duplex; a subsequent good measurement MAY auto-recover
 *     (left to 0101).
 *
 * `HALF_DUPLEX_GATE_SPEC` below is the typed handoff so 0101 imports the
 * trigger state name and threshold rather than re-deriving them.
 * ──────────────────────────────────────────────────────────────────────────── */

/** Typed handoff to issue 0101 describing the runtime half-duplex degraded mode. */
export const HALF_DUPLEX_GATE_SPEC = {
  /** TurnFsm state during which the mic is muted when the gate is engaged. */
  muteDuringState: "bob_speaking",
  /** Debug/attestation event category + severity emitted when the gate engages. */
  event: { category: "voice", severity: "warn", type: "aec_degraded_half_duplex" },
  /**
   * The gate is a NET, not the primary plan: full-duplex with working AEC is
   * the target. This flag documents that engaging it is a degradation.
   */
  isDegradation: true,
} as const;
