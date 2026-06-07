//! AEC spike — native seam (issue 0097 / PRD 0016 Annexe I).
//!
//! This module holds the macOS-native pieces of the microphone-capture +
//! echo-cancellation spike, plus the Rust-side **fallback seam**.
//!
//! ## WKWebView media-capture permission (criterion 1)
//!
//! On macOS the webview must answer WKWebView's
//! `webView:requestMediaCapturePermissionForOrigin:initiatedByFrame:type:decisionHandler:`
//! delegate for `getUserMedia({audio:…})` to resolve. **wry 0.55.1 (this app's
//! webview backend) already implements that delegate and calls it with
//! `WKPermissionDecision::Grant`.** So there is intentionally NO hand-rolled
//! objc UIDelegate here: a second UIDelegate set on the same `WKWebView` would
//! replace wry's and break the framework's own handling. The remaining native
//! requirement — the one thing wry can't supply — is the
//! `NSMicrophoneUsageDescription` Info.plist key, added in `src-tauri/Info.plist`.
//!
//! If a future wry release regresses this delegate, the override belongs right
//! here (set a custom `WKUIDelegate` via `objc2` + `objc2-web-kit`, both already
//! in the dependency tree, on the `WKWebView` obtained from the Tauri window's
//! raw webview handle). We document the hook rather than ship a redundant one.
//!
//! ## Decision artefact (on-disk side)
//!
//! `aec_spike_write_verdict` persists the spike verdict JSON under
//! `BOB_DATA_DIR` (default `~/.bob`), mirroring the `llm_selection.json`
//! convention. The in-app canonical read path stays the frontend constant in
//! `src/audio/aec/captureDecision.ts`; this file is for the human/device
//! follow-up and any out-of-process reader.
//!
//! ## Rust fallback seam
//!
//! When the spike selects `rust` (any Annexe-I criterion not a confirmed pass),
//! capture + playback + AEC move into Rust via `cpal` + `webrtc-audio-processing`,
//! with the webview reduced to UI. That integration is gated behind the
//! `rust-aec-fallback` Cargo feature so the default build never needs the C++
//! audio-processing toolchain. The seam (module, deps, the command that selects
//! it) is real; the full DSP wiring is the documented follow-up — see
//! `rust_capture`.

use std::path::PathBuf;

use serde::{Deserialize, Serialize};

/// The capture architecture chosen by the spike. Mirror of the frontend
/// `CapturePath` (`src/audio/aec/aecSpikeSelector.ts`).
///
/// Part of the seam contract issue 0099 consumes when it dispatches capture to
/// Rust; not referenced by other Rust code yet, hence `allow(dead_code)`.
#[allow(dead_code)]
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "lowercase")]
pub enum CapturePath {
  Webview,
  Rust,
}

/// Filename of the persisted verdict under `BOB_DATA_DIR`. Mirror of
/// `VERDICT_ARTEFACT_FILENAME` in `captureDecision.ts`.
pub const VERDICT_ARTEFACT_FILENAME: &str = "aec_spike_verdict.json";

/// Resolve `BOB_DATA_DIR` (env override, else `~/.bob`), matching the backend's
/// `bob.config` convention so the verdict lands beside `llm_selection.json`.
fn bob_data_dir() -> PathBuf {
  if let Ok(dir) = std::env::var("BOB_DATA_DIR") {
    return PathBuf::from(dir);
  }
  let home = std::env::var("HOME").unwrap_or_else(|_| ".".to_string());
  PathBuf::from(home).join(".bob")
}

/// Persist the spike verdict JSON (produced by the frontend harness) to
/// `{BOB_DATA_DIR}/aec_spike_verdict.json`.
///
/// Accepts the verdict as an already-serialised JSON string so the canonical
/// shape stays owned by the frontend `buildSpikeVerdict`; Rust only writes it
/// through verbatim (after a parse-check that it is valid JSON). Returns the
/// absolute path written.
#[tauri::command]
pub fn aec_spike_write_verdict(verdict_json: String) -> Result<String, String> {
  // Validate it parses as JSON so we never persist a corrupt artefact.
  serde_json::from_str::<serde_json::Value>(&verdict_json)
    .map_err(|e| format!("verdict is not valid JSON: {e}"))?;

  let dir = bob_data_dir();
  std::fs::create_dir_all(&dir).map_err(|e| format!("create {}: {e}", dir.display()))?;
  let path = dir.join(VERDICT_ARTEFACT_FILENAME);
  std::fs::write(&path, verdict_json).map_err(|e| format!("write {}: {e}", path.display()))?;
  Ok(path.to_string_lossy().into_owned())
}

/// Report whether the Rust AEC fallback capture path is COMPILED INTO this
/// build (the `rust-aec-fallback` feature). Lets the frontend / integrator
/// detect at runtime whether selecting `rust` is actually backed by a build
/// that can serve it, vs. needs a rebuild with the feature on.
#[tauri::command]
pub fn aec_spike_rust_fallback_available() -> bool {
  cfg!(feature = "rust-aec-fallback")
}

/// Rust fallback capture seam (issue 0097 fallback; consumed when the spike
/// selects `CapturePath::Rust`). Gated behind `rust-aec-fallback`.
///
/// The full `cpal` (capture + playback) + `webrtc-audio-processing` (AEC) loop
/// is the documented follow-up; this seam fixes the module boundary and the
/// public entrypoint issue 0099 will call so the decision flow is real today.
#[cfg(feature = "rust-aec-fallback")]
pub mod rust_capture {
  //! Capture + playback + AEC entirely in Rust. Webview = UI only.
  //!
  //! Wiring sketch (the follow-up implementation):
  //!   * `cpal` opens the default input (mic) and output (speaker) streams.
  //!   * `webrtc_audio_processing::Processor` is fed the render (far-end /
  //!     speaker) frames via `process_render_frame` and the capture (mic)
  //!     frames via `process_capture_frame`, with `EchoCancellation` enabled,
  //!     at 16 kHz mono in 10 ms frames.
  //!   * The cancelled mic frames are forwarded upstream (the binary WS
  //!     channel from PRD Annexe A.1), exactly like the webview path would.

  /// Start the Rust capture+AEC pipeline. Returns a handle the caller stops
  /// on `voice_stop`.
  ///
  /// NOTE: this is the SEAM. It returns an explicit "not yet implemented"
  /// error so a premature call fails loudly rather than silently doing
  /// nothing — the full pipeline is the documented follow-up gated on this
  /// feature.
  pub fn start_rust_capture() -> Result<RustCaptureHandle, String> {
    Err(
      "rust-aec-fallback pipeline not yet implemented (issue 0097 follow-up); \
       seam + deps are wired so this build can host it"
        .to_string(),
    )
  }

  /// Opaque handle to a running Rust capture pipeline.
  pub struct RustCaptureHandle {
    _private: (),
  }

  impl RustCaptureHandle {
    /// Stop capture + playback and release the devices.
    pub fn stop(self) {}
  }
}

#[cfg(test)]
mod tests {
  use super::*;

  #[test]
  fn data_dir_honours_env_override() {
    // SAFETY: single-threaded unit test; we set + read one env var.
    unsafe { std::env::set_var("BOB_DATA_DIR", "/tmp/bob-test-aec") };
    assert_eq!(bob_data_dir(), PathBuf::from("/tmp/bob-test-aec"));
    unsafe { std::env::remove_var("BOB_DATA_DIR") };
  }

  #[test]
  fn capture_path_serializes_lowercase() {
    assert_eq!(
      serde_json::to_string(&CapturePath::Webview).unwrap(),
      "\"webview\""
    );
    assert_eq!(
      serde_json::to_string(&CapturePath::Rust).unwrap(),
      "\"rust\""
    );
  }

  #[test]
  fn write_verdict_rejects_non_json() {
    let err = aec_spike_write_verdict("not json {".to_string()).unwrap_err();
    assert!(err.contains("not valid JSON"), "got: {err}");
  }

  #[test]
  fn write_verdict_persists_valid_json_under_data_dir() {
    let tmp = std::env::temp_dir().join("bob-test-aec-write");
    let _ = std::fs::remove_dir_all(&tmp);
    // SAFETY: single-threaded unit test.
    unsafe { std::env::set_var("BOB_DATA_DIR", tmp.to_str().unwrap()) };

    let verdict = r#"{"schema_version":1,"chosen_path":"rust","ok":false}"#;
    let written = aec_spike_write_verdict(verdict.to_string()).unwrap();
    assert!(written.ends_with(VERDICT_ARTEFACT_FILENAME));
    let back = std::fs::read_to_string(&written).unwrap();
    assert_eq!(back, verdict);

    unsafe { std::env::remove_var("BOB_DATA_DIR") };
    let _ = std::fs::remove_dir_all(&tmp);
  }

  #[test]
  fn rust_fallback_availability_matches_feature_flag() {
    assert_eq!(
      aec_spike_rust_fallback_available(),
      cfg!(feature = "rust-aec-fallback")
    );
  }
}
