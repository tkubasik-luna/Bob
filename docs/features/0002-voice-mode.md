# Voice Mode (Bob parle)

Shipped on 2026-05-21 from PRD `prd/0002-voice-mode.md`.

## What it does

A speaker toggle in the chat header turns on a hands-free voice mode. Each assistant reply is synthesized in French and played back automatically while text continues to render in the conversation. Synthesis starts on the first complete sentence, so the user hears Bob before the full message is done generating. Sending a new message while Bob is speaking interrupts playback. The model runs 100% locally (Kokoro).

## Technical surface

- Backend WS events: `assistant_msg` (now carries `msg_id`), `audio_chunk`, `audio_end`, `audio_error`, `tts_preparing`, `tts_ready`. Client request `user_msg` gains optional `voice: bool`.
- Backend HTTP: `POST /debug/tts` returns a WAV for manual synthesis testing.
- New backend modules: `bob.tts_service` (Kokoro ONNX), `bob.model_downloader` (HF artifacts cached in `~/.bob/models/kokoro/`), `bob.text_segmenter` (pure-logic sentence boundary detector), `bob.spoken_text_cleaner` (markdown/URL/code-block scrubbing applied before TTS).
- Modified backend modules: `bob.ws_router` (voice flag, per-msg `msg_id`, pipelined TTS task, interruption via cancellation map, audio event emission), `bob.config` (Kokoro paths / voice / sample rate / download URLs), `bob.main` (mounts debug router).
- New frontend module: `src/audio/audioPlayer.ts` (Web Audio FIFO scheduler with msg_id-aware speaking state observation), hook `src/hooks/useVoiceMode.ts` (session-only toggle).
- Modified frontend: `ChatView` (toggle button, message dispatch, interruption + stale-chunk filter, prep/error toasts, speaking wave indicator), `chatStore` (`speakingMsgId`, sticky toast kinds), `Toast` (info/error variants), `types/ws.ts` (audio event types).
- Python deps added: `kokoro-onnx`, `numpy`.
- Audio wire format: base64 s16le mono PCM at 24 kHz, chunked at ≤256 KB per WS frame.

## Notable decisions

- TTS is local-only. Voice fixed to `ff_siwis`, speed 1.0; no UI for either.
- Voice toggle is **session-only** (no `localStorage`). Reset to OFF on app restart.
- Streaming granularity: sentence-level. The current LLM client returns a complete schema-validated response, not deltas, so per-token streaming is deferred — see the `text_segmenter` docstring. The `SentenceBuffer` API is ready for a future LLM-client refactor.
- Cancellation is the source of truth: when a new `user_msg` arrives, the WS handler cancels every active TTS task for the session and emits a final `audio_end` for each. The frontend additionally filters `audio_chunk` frames by the most-recent `msg_id` to drop chunks already in flight on the socket past the cancel point.
- URLs in spoken text are **stripped** (not replaced by "lien"). Documented in `spoken_text_cleaner`.
- Code-block stripping happens before any other markdown substitution so a fenced block never produces a phantom sentence.
- The Kokoro model (~360 MB) is downloaded lazily on first synthesis. The backend emits `tts_preparing`/`tts_ready` so the frontend can show a sticky info toast during the download. Failures emit `audio_error` and surface as an error toast; the toggle stays ON for retry.

## Issues

- `issues/0008-tts-kokoro-bootstrap.md` — Kokoro service + downloader + debug endpoint — commit 541764c
- `issues/0009-audio-player-toggle-ui.md` — Web Audio player + voice toggle — commit 434e6e7
- `issues/0010-voice-mode-e2e-full-message.md` — end-to-end WS wiring — commit 15a0f5b
- `issues/0011-sentence-streaming-segmenter.md` — sentence segmenter + pipelined TTS — commit 71afbea
- `issues/0013-voice-interruption.md` — interruption + audio event emission — commit d681e86
- `issues/0014-voice-ui-feedback-errors.md` — speaking indicator + prep/error toasts — commit c2d8dca
- `issues/0012-spoken-text-cleanup.md` — markdown/URL cleanup before TTS — commit 969a8b0
