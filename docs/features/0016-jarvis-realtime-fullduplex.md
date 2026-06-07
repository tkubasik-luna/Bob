# 0016 — Jarvis Temps Réel Full-Duplex (+ harnais d'attestation agent)

Shipped on 2026-06-08 from PRD `prd/0016-jarvis-realtime-fullduplex.md`.

## What it does

Turns Jarvis from a turn-by-turn text/voice assistant into a **full-duplex real-time voice agent**: the mic is always open while voice mode is ON, Bob transcribes in streaming and understands before you finish, thinks in parallel (a background Thinker maintains conversation state), pre-drafts the answer speculatively for a near-instant reply, can be **barged-in** mid-sentence (and remembers what it already said), and drops light backchannels ("mm", "ok") in your pauses. Each LLM role (Speaker/Thinker/Draft/sub-agent) is independently assignable to Claude or an LM Studio model from the HUD. Text input still works (zero regression). Transversally, a headless **`bob attest` CLI** drives the real WS pipeline with fixture input and asserts machine-readable invariants, so the agent can validate each slice in autonomy without a mic.

## Technical surface

- **`bob` CLI** (`backend/src/bob/attest/`): `bob attest <scenario.yaml>` boots an isolated ephemeral backend (temp `BOB_DATA_DIR`, dedicated port, `fake` LLM/STT/TTS), drives `/ws/chat` (text + binary mic frames), asserts on `/ws/debug`, emits Annexe-C verdict JSON + exit 0/1. `--deep` adds a TTS→STT round-trip. 13 declarative scenarios under `scenarios/`.
- **Audio I/O**: frontend `useMicCapture` (getUserMedia + AudioWorklet, 16 kHz mono, binary WS frames tagged `0x01`) ; backend binary WS channel in `ws_router`; `SttEngine` (whisper.cpp via `pywhispercpp`, optional `stt` extra, lazy download) + deterministic `FakeSttEngine`.
- **Real-time orchestration** (`voice_loop.py`, `turn_fsm.py`, `vad.py`, `endpointer.py`, `bargein.py`, `thinker_loop.py`, `live_transcript_state.py`, `speculative_draft.py`, `backchannel.py`, `latency.py`): a compact FSM (`idle/user_speaking/thinking/bob_speaking` + barge-in), hybrid endpoint (VAD silence + Thinker `user_turn_complete` confirmed by a stable partial), background Thinker + Speculative Draft on distinct role clients, prefix/similarity commit gate.
- **Per-role LLM** (`llm_selection_store.py` `RoleSelectionStore` v2, `llm/factory.py` per-role builders, `llm_swap.py`, `llm_router.py` per-role GET/PUT, `model_budget.py`, `lm_studio_manager.py` v2 multi-load + ref-counted offload). HUD: per-role section + STT section + budget feedback + `FloorIndicator` in `SettingsControl` / Piste HUD.
- **Voice WS events** (`category:"voice"`, payload under `payload.ws_event`): `stt_partial`, `stt_final`, `turn_state`, `thinker_snapshot`, `thinker_consult`, `backchannel`, `draft_status`, `bargein`, `turn_latency`, `audio_chunk`, `voice_turn_persisted`, `voice_retention_purged`.
- **Migrations**: `0010_voice_turns.sql`, `0011_voice_audio_blobs.sql`. Persistence (`voice_store.py`) + bounded `VoiceRetentionPolicy` (audio by size 1.5 GiB, turns by age 30 d). Final transcript links into Jarvis history.
- **Selection file**: `llm_selection.json` schema_version 2 (roles + stt + budget) with defensive decode + 1→2 migration.

## Notable decisions

- **Cascade-parallel, not unified S2S** — keep the existing Jarvis brain; Listen/Think/Speak run in parallel on distinct role clients. A committed speculative Draft is re-injected into the *normal* say-path (trivial validation), so the cold and anticipated paths converge.
- **Black-box attestation over the real WS** — assertions are on invariants/contracts (FSM state reached, latency < target, barge-in cut < 300 ms, committed == spoken, role used model), never exact text; `fake` LLM/STT/TTS keep runs deterministic. Each slice ships its scenario; DoD = scenario green.
- **`emit_event` nests the voice payload under `payload.ws_event`** — all voice matchers read there (distinct from `emit_debug`'s flat `payload`, used by `llm`/`output` events).
- **Capture default = `webview`** (PRD path), `hardwarePending: true`; the Rust `cpal`+webrtc fallback is the spike-failure path (`FALLBACK_CAPTURE_PATH`). `setCaptureDecisionOverride` is the runtime/test seam.
- **`LMStudioManager` v2 reverts offload-first** to multi-load + ref-counted selective offload; `ModelBudget` (footprint = disk + KV ∝ ctx; per-host ceiling) is the OOM guard that makes the reversion safe — refuses *before* a budget-exceeding load.
- **Half-duplex gate** (mute mic during `bob_speaking`) is the documented net if AEC fails at runtime — backchannels stay in pauses (no overlap FSM state).
- **Known gaps to honour when evolving** (deliberately deferred — not faked): per-host boot wiring is not yet rewired into `main.py`, so the e2e `role_used_model` / budget-refusal scenarios are unit/integration-tested rather than driven through a live load; no REST endpoints yet for live per-host budget usage, per-host (`?base_url=`) model lists, or STT-model write; `t_draft_ready`/`t_commit_decision` were the only pending latency marks and are now live. The AEC ≥25 dB / spoken-word criteria of the 0097 spike need an on-device run (the dB math + path selector are unit-proven).

## Issues

- `issues/0097-spike-aec-getusermedia.md` — AEC + getUserMedia spike, AFK auto-fallback selector — commit `f25848a`
- `issues/0098-attest-harness-skeleton.md` — `bob attest` CLI skeleton (ephemeral + fake LLM + ScenarioRunner) — commit `7a44d3a`
- `issues/0106-per-role-selection.md` — per-role LLM selection store + factory + swap + router — commit `97cfc01`
- `issues/0099-listen-stt-pipeline.md` — Listen: mic capture, binary WS, whisper.cpp STT — commit `a05e85e`
- `issues/0100-fullduplex-loop-bare.md` — bare full-duplex loop (VAD, Endpointer, TurnFsm, say-path) — commit `9b7fb3d`
- `issues/0107-model-budget-multiload.md` — ModelBudget + LMStudioManager v2 multi-load — commit `b19b560`
- `issues/0101-bargein.md` — barge-in (cut + commit spoken prefix + half-duplex net) — commit `27d71da`
- `issues/0102-thinker-livestate-provider.md` — Thinker loop + LiveTranscriptState + provider — commit `b3d7810`
- `issues/0109-voice-retention.md` — voice-turn persistence + bounded retention — commit `4ed4140`
- `issues/0110-latency-instrumentation.md` — turn_latency event + latency assertions (+`--deep`) — commit `0c6ddb9`
- `issues/0108-role-picker-ui.md` — per-role HUD picker + STT section + floor indicator — commit `52c968b`
- `issues/0103-semantic-endpoint.md` — semantic endpoint (Thinker user_turn_complete + confirmation) — commit `9506ea5`
- `issues/0105-backchannels.md` — proactivity-gated backchannels in pauses — commit `895f71e`
- `issues/0104-speculative-draft.md` — speculative Draft + commit gate (anticipation) — commit `88d7ee4`
