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
 * The PRD default capture source is **`webview`** (getUserMedia + AudioWorklet,
 * AEC by WKWebView); the Rust path is the FALLBACK chosen only when the spike
 * measures an AEC failure. A headless agent cannot empirically measure ≥25 dB
 * on a real mic, so C2/C3 stay `pending` until a human runs the on-device step;
 * pre-spike we run the PRD default (`webview`) with `hardwarePending: true` so
 * the « Listen » pipeline (issue 0099) is functional, NOT dead-on-arrival. The
 * AFK auto-fallback rule (`selectCapturePath`: anything short of an all-pass →
 * `rust`) is what an on-device spike FAILURE would select — exported as
 * {@link FALLBACK_CAPTURE_PATH}; flip `DEFAULT_CAPTURE_DECISION.path` to it if
 * the device run fails the AEC criteria.
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
 * The committed, deterministic decision.
 *
 * `path` is the **PRD 0016 default capture source — `webview`** (getUserMedia +
 * AudioWorklet, AEC handled by WKWebView). The Rust path is the documented
 * *fallback the spike selects only on measured AEC failure*
 * ({@link selectCapturePath} over a non-pass spike → `rust`; see
 * {@link FALLBACK_CAPTURE_PATH}). Pre-spike (AFK, no hardware) we run the PRD
 * default optimistically with `hardwarePending: true` so issue 0099's mic
 * capture is functional; the on-device spike flips this to `rust` IF the ≥25 dB
 * / word-transcribed criteria fail.
 */
export const DEFAULT_CAPTURE_DECISION: CaptureDecision = {
  schemaVersion: 1,
  path: "webview",
  hardwarePending: true,
};

/**
 * What the AFK auto-fallback rule yields for the current (all-`pending`)
 * scaffolded spike results: `rust`. Exported so a follow-up that records a real
 * spike FAILURE can switch {@link DEFAULT_CAPTURE_DECISION} to this without
 * re-deriving the rule, and so the rule stays asserted in tests.
 */
export const FALLBACK_CAPTURE_PATH: CapturePath = selectCapturePath(SCAFFOLDED_SPIKE_RESULTS);

//: Runtime/test override of the capture path (issue 0099). `null` = use the
//: committed default. The AEC runtime probe (issue 0101) may also flip this.
let _captureDecisionOverride: CapturePath | null = null;

/**
 * Override the capture path at runtime (or in tests); `null` clears it.
 *
 * Lets the AEC runtime probe / degraded-mode gate (issue 0101) switch the
 * source without rebuilding, and lets 0099's tests drive both branches.
 */
export function setCaptureDecisionOverride(path: CapturePath | null): void {
  _captureDecisionOverride = path;
}

/**
 * Canonical read path for downstream consumers (issue 0099).
 *
 * Returns the active decision: the {@link setCaptureDecisionOverride} value when
 * set, else the committed {@link DEFAULT_CAPTURE_DECISION}. Kept as a function
 * so a future iteration can layer the persisted-verdict JSON on top without
 * changing call sites.
 */
export function getCaptureDecision(): CaptureDecision {
  if (_captureDecisionOverride !== null) {
    return { ...DEFAULT_CAPTURE_DECISION, path: _captureDecisionOverride };
  }
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
