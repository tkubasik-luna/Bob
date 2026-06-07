"""Speech-to-text engine — the « Listen » spine (PRD 0016 / issue 0099).

The mic capture path (webview AudioWorklet, frontend ``MicCapture``)
downsamples to **16 kHz mono s16le** and ships **binary WS frames tagged
``0x01``** (Annexe A.1). The WS router strips the tag, validates the PCM,
and feeds it to an :class:`SttEngine` *session* scoped to one voice turn.
The session yields incremental :class:`SttPartial` hypotheses while audio
flows and a single :class:`SttFinal` transcript when the turn is frozen
(endpoint / ``voice_stop`` / socket close).

Why a session abstraction rather than a one-shot ``transcribe(bytes)``?
A voice turn is a *stream*: frames arrive ~20-40 ms apart and we want
partials on the wire as the hypothesis firms up (Annexe A.2
``stt_partial`` carries ``stable_prefix_len``). The session holds the
per-turn buffer + the last-emitted hypothesis so it can compute the
stable-prefix delta and debounce noisy re-emits. ``finalize()`` returns
the frozen transcript and releases the buffer.

Swappable engine
----------------

:class:`SttEngine` is a tiny :class:`typing.Protocol` (``open_session`` +
``is_model_cached`` + ``preload``). Two implementations ship:

- :class:`WhisperCppSttEngine` — the real engine: whisper.cpp via
  ``pywhispercpp`` (Metal/CoreML on Apple Silicon), default model
  ``large-v3-turbo``, downloaded **lazily** the first time a session is
  opened (mirrors :class:`bob.tts_service.KokoroTtsService` —
  ``is_model_cached`` / ``preload``). Importing ``pywhispercpp`` and
  loading the model are both deferred so the backend boots green without
  the native dependency or the weights.
- :class:`FakeSttEngine` — deterministic, scriptable, no native model.
  The whole test surface (unit tests + the ``bob attest --audio``
  scenario) drives this so CI never needs the real weights. It segments
  a scripted transcript into word-by-word partials as audio accumulates.

The default engine is selected by :func:`get_default_stt_engine` off the
``STT_ENGINE`` setting (``whisper_cpp`` in prod, ``fake`` forced in
tests / attest scenarios).
"""

from __future__ import annotations

from dataclasses import dataclass
from threading import Lock
from typing import TYPE_CHECKING, Protocol, runtime_checkable

import structlog

from bob.config import Settings, get_settings

if TYPE_CHECKING:  # pragma: no cover
    from collections.abc import Iterable

_logger = structlog.get_logger(__name__)


#: First byte of every inbound binary WS frame (Annexe A.1): ``0x01`` = mic
#: PCM frame. Reserved so future binary streams (e.g. a Rust-sourced capture
#: path) can multiplex on the same socket with a distinct tag.
MIC_FRAME_TAG: int = 0x01

#: Bytes per sample for signed-16-bit little-endian mono PCM.
_BYTES_PER_SAMPLE = 2


class SttFrameError(ValueError):
    """A binary WS frame did not match the mic-frame contract (Annexe A.1).

    Raised by :func:`decode_pcm_frame` for an empty frame, an unknown type
    tag, or a payload whose byte length is not a whole number of 16-bit
    samples. The WS layer treats this as a *drop this frame* signal (logged
    once, no turn abort) — a single mangled frame must not kill the turn.
    """


def decode_pcm_frame(data: bytes, *, expected_tag: int = MIC_FRAME_TAG) -> bytes:
    """Strip the 1-byte type tag and return the raw s16le PCM payload.

    Annexe A.1: ``1er octet = tag de type (0x01 = mic frame) ... le reste =
    samples``. We validate the tag and that the remaining bytes form a whole
    number of 16-bit samples (s16le is 2 bytes/sample). The samples
    themselves are returned untouched — endianness is the client's
    responsibility (the webview worklet writes little-endian; whisper.cpp
    consumes little-endian on Apple Silicon).

    Raises :class:`SttFrameError` on an empty frame, a wrong tag, or an
    odd-length payload.
    """

    if not data:
        raise SttFrameError("empty binary frame")
    tag = data[0]
    if tag != expected_tag:
        raise SttFrameError(f"unexpected binary frame tag: {tag:#04x} (want {expected_tag:#04x})")
    payload = data[1:]
    if len(payload) % _BYTES_PER_SAMPLE != 0:
        raise SttFrameError(
            f"PCM payload length {len(payload)} is not a whole number of s16le samples"
        )
    return payload


def pcm16_sample_count(pcm: bytes) -> int:
    """Number of s16le samples in a (tag-stripped) PCM payload."""

    return len(pcm) // _BYTES_PER_SAMPLE


@dataclass(frozen=True)
class SttPartial:
    """An incremental whisper hypothesis for an in-flight voice turn.

    ``text`` is the full hypothesis so far (not a delta — Annexe A.2 ships
    the whole ``text`` each time). ``stable_prefix_len`` is the number of
    *leading characters* of ``text`` the engine considers settled (will not
    change in later partials of this turn); the frontend can render the
    stable prefix solidly and the tail as tentative.
    """

    text: str
    stable_prefix_len: int


@dataclass(frozen=True)
class SttFinal:
    """The frozen transcript for a voice turn, emitted once at endpoint."""

    text: str


@runtime_checkable
class SttSession(Protocol):
    """Per-turn streaming transcription session.

    Lifecycle: ``open_session`` -> N x ``accept_frame`` -> ``finalize`` (or
    ``close`` if the turn is abandoned without a final). All methods are
    synchronous and cheap to call from the event loop EXCEPT a real engine's
    transcription, which the WS layer offloads to a thread (see
    :mod:`bob.ws_router`). ``accept_frame`` returns 0..n partials so a slow
    engine can coalesce frames and a fast one can emit per-frame.
    """

    def accept_frame(self, pcm: bytes) -> list[SttPartial]:
        """Feed one decoded s16le PCM frame; return any new partials."""
        ...

    def finalize(self) -> SttFinal:
        """Freeze the turn and return the final transcript."""
        ...

    def close(self) -> None:
        """Release per-turn resources without producing a final."""
        ...


@runtime_checkable
class SttEngine(Protocol):
    """Swappable speech-to-text backend.

    A factory of :class:`SttSession` plus the lazy-model contract mirrored
    from :class:`bob.tts_service.KokoroTtsService`: :meth:`is_model_cached`
    lets the WS layer decide whether to emit a ``stt_preparing`` toast, and
    :meth:`preload` forces the (possibly downloading) model load up front.
    """

    def open_session(self, turn_id: str) -> SttSession:
        """Create a fresh per-turn transcription session."""
        ...

    def is_model_cached(self) -> bool:
        """True when the model weights are already on disk (no download)."""
        ...

    def preload(self) -> None:
        """Force the model to load (downloading if absent)."""
        ...


# --- Fake engine (deterministic, native-free) -------------------------------


class _FakeSession:
    """Word-by-word partials from a scripted transcript as audio accumulates.

    Deterministic and native-free: it ignores the PCM *content* and reveals
    one more word of the scripted transcript per ``_words_per_chunk`` worth
    of accumulated samples, so a fixture WAV streamed in frames produces a
    realistic-looking partial sequence with a stable, asserted final.

    ``revise_to`` (PRD 0016 / issue 0104) models an end-of-phrase STT REVISION:
    the partials reveal ``transcript`` word-by-word (what the live consumers — the
    Thinker and the SpeculativeDraft — see DURING speech), but :meth:`finalize`
    returns ``revise_to`` instead. This is the realistic case a speculative draft
    must guard against: the pre-written reply was built on the streamed partial,
    yet the settled clause diverged. ``None`` (the default) keeps the exact prior
    behaviour (final == the streamed transcript), so every existing fixture is
    byte-for-byte unchanged.
    """

    def __init__(
        self, transcript: str, *, samples_per_word: int, revise_to: str | None = None
    ) -> None:
        self._words = transcript.split()
        self._samples_per_word = max(1, samples_per_word)
        self._samples = 0
        self._revealed = 0
        self._revise_to = revise_to

    def accept_frame(self, pcm: bytes) -> list[SttPartial]:
        self._samples += pcm16_sample_count(pcm)
        target = min(len(self._words), self._samples // self._samples_per_word)
        out: list[SttPartial] = []
        while self._revealed < target:
            self._revealed += 1
            text = " ".join(self._words[: self._revealed])
            # The whole revealed prefix is "stable" in the fake (it never
            # rewrites a word), so stable_prefix_len == len(text) minus the
            # last (still-growing) word — model the common case where the
            # trailing token is tentative.
            stable = len(" ".join(self._words[: max(0, self._revealed - 1)]))
            out.append(SttPartial(text=text, stable_prefix_len=stable))
        return out

    def finalize(self) -> SttFinal:
        # An end-of-phrase revision (issue 0104) overrides the streamed transcript
        # at freeze time; otherwise the final is exactly what the partials built.
        if self._revise_to is not None:
            return SttFinal(text=self._revise_to)
        return SttFinal(text=" ".join(self._words))

    def close(self) -> None:
        return None


class FakeSttEngine:
    """Scriptable :class:`SttEngine` for tests + the ``--audio`` attest path.

    Construction:

    - ``transcript`` — the canned transcript every session reveals word-by-word
      as PCM accumulates (so partials are realistic). Also the FINAL transcript
      unless ``revise_to`` overrides it.
    - ``samples_per_word`` — how many s16le samples must accumulate before
      the next word is revealed. Defaults small so a short fixture surfaces
      the whole transcript.
    - ``revise_to`` — when set, the FINAL transcript a session freezes to
      (modelling an end-of-phrase STT revision, issue 0104). The partials still
      reveal ``transcript`` during speech; only :meth:`finalize` differs. ``None``
      (the default) keeps the final equal to the streamed transcript.

    Always "cached" (no model, no download) so the lazy-download branch is
    skipped on the fake path and the suite is deterministic.
    """

    def __init__(
        self,
        *,
        transcript: str = "",
        samples_per_word: int = 1600,
        revise_to: str | None = None,
    ) -> None:
        self.transcript = transcript
        self.samples_per_word = samples_per_word
        self.revise_to = revise_to
        self.opened_turns: list[str] = []

    def open_session(self, turn_id: str) -> SttSession:
        self.opened_turns.append(turn_id)
        return _FakeSession(
            self.transcript, samples_per_word=self.samples_per_word, revise_to=self.revise_to
        )

    def is_model_cached(self) -> bool:
        return True

    def preload(self) -> None:
        return None


# --- whisper.cpp engine (real, lazy-loaded) ---------------------------------


class _WhisperCppSession:
    """Accumulate PCM for a turn; transcribe on finalize (and on partials).

    whisper.cpp transcribes a *buffer*, not a stream, so we accumulate the
    decoded s16le frames and run a transcription pass over the growing buffer
    to produce partials, and a final pass on :meth:`finalize`. Partials are
    debounced: a re-transcription only runs once ``_partial_every_samples``
    of new audio has arrived since the last pass, and a partial is only
    emitted when the hypothesis text actually changed. The stable-prefix
    length is the common prefix between the previous and the new hypothesis —
    a cheap, monotone-ish proxy that lets the client render settled text.
    """

    def __init__(
        self,
        engine: WhisperCppSttEngine,
        *,
        partial_every_samples: int,
    ) -> None:
        self._engine = engine
        self._buf = bytearray()
        self._partial_every = max(1, partial_every_samples)
        self._samples_at_last_pass = 0
        self._last_text = ""

    def accept_frame(self, pcm: bytes) -> list[SttPartial]:
        self._buf.extend(pcm)
        total = pcm16_sample_count(bytes(self._buf))
        if total - self._samples_at_last_pass < self._partial_every:
            return []
        self._samples_at_last_pass = total
        text = self._engine.transcribe_pcm(bytes(self._buf))
        if not text or text == self._last_text:
            return []
        stable = _common_prefix_len(self._last_text, text)
        self._last_text = text
        return [SttPartial(text=text, stable_prefix_len=stable)]

    def finalize(self) -> SttFinal:
        if not self._buf:
            return SttFinal(text=self._last_text)
        text = self._engine.transcribe_pcm(bytes(self._buf))
        return SttFinal(text=text or self._last_text)

    def close(self) -> None:
        self._buf.clear()


def _common_prefix_len(a: str, b: str) -> int:
    """Length of the longest common leading substring of ``a`` and ``b``."""

    n = min(len(a), len(b))
    i = 0
    while i < n and a[i] == b[i]:
        i += 1
    return i


class WhisperCppSttEngine:
    """whisper.cpp (``pywhispercpp``) engine with lazy model download.

    The native model + ``pywhispercpp`` import are both deferred to the first
    :meth:`open_session` (via :meth:`_ensure_loaded`). ``pywhispercpp``
    downloads the named model into its own cache on first construction, so
    :meth:`is_model_cached` probes that cache directory the same way the
    Kokoro service probes the HF hub cache — the WS layer uses it to decide
    whether to emit a ``stt_preparing`` toast before paying the download.

    Thread-safety: ``pywhispercpp.Model`` is not declared safe for concurrent
    transcription, so all access is serialized through ``_synth_lock``. Voice
    turns are sequential per session anyway (one turn speaking at a time),
    but a single shared model across turns still needs the guard.
    """

    def __init__(self, settings: Settings | None = None) -> None:
        self._settings = settings or get_settings()
        self._model: object | None = None
        self._load_lock = Lock()
        self._infer_lock = Lock()

    def _model_cache_dir(self) -> object:
        from pathlib import Path

        return Path.home() / ".cache" / "pywhispercpp" / "models"

    def is_model_cached(self) -> bool:
        """True when a whisper.cpp ``ggml-<model>.bin`` is already on disk.

        Conservative: any failure to probe (e.g. odd home dir) returns True so
        we never *falsely* claim a download is needed and block on a toast.
        """

        try:
            from pathlib import Path

            cache = self._model_cache_dir()
            assert isinstance(cache, Path)
            # pywhispercpp names files ggml-<model>.bin
            return (cache / f"ggml-{self._settings.STT_MODEL}.bin").exists()
        except Exception:
            return True

    def preload(self) -> None:
        """Force the model to load (downloading via pywhispercpp if absent)."""

        self._ensure_loaded()

    def _ensure_loaded(self) -> object:
        if self._model is not None:
            return self._model
        with self._load_lock:
            if self._model is not None:
                return self._model
            try:
                from pywhispercpp.model import Model  # type: ignore[import-not-found]
            except ImportError as exc:  # pragma: no cover - env without native dep
                raise SttEngineUnavailableError(
                    "pywhispercpp is not installed; install it to enable the whisper.cpp "
                    "STT engine, or set STT_ENGINE=fake"
                ) from exc
            _logger.info(
                "stt.whisper.load",
                model=self._settings.STT_MODEL,
                language=self._settings.STT_LANGUAGE,
            )
            self._model = Model(
                self._settings.STT_MODEL,
                language=self._settings.STT_LANGUAGE,
                print_realtime=False,
                print_progress=False,
            )
            return self._model

    def transcribe_pcm(self, pcm: bytes) -> str:
        """Transcribe an s16le mono 16 kHz PCM buffer to text.

        Converts the buffer to the float32 [-1, 1] array ``pywhispercpp``
        expects and runs one synchronous transcription pass under the infer
        lock. Returns the concatenated segment text, stripped.
        """

        import numpy as np

        model = self._ensure_loaded()
        if not pcm:
            return ""
        samples = np.frombuffer(pcm, dtype="<i2").astype(np.float32) / 32768.0
        with self._infer_lock:
            segments = _run_whisper(model, samples)
        return " ".join(seg.strip() for seg in segments if seg.strip()).strip()

    def open_session(self, turn_id: str) -> SttSession:
        # Load eagerly here so a download/import failure surfaces at session
        # open (the WS layer's degradation path), not mid-frame.
        self._ensure_loaded()
        return _WhisperCppSession(
            self,
            partial_every_samples=self._settings.STT_SAMPLE_RATE,  # ~1s windows
        )


class SttEngineUnavailableError(RuntimeError):
    """The whisper.cpp engine cannot load (missing ``pywhispercpp`` / model).

    Surfaced at :meth:`WhisperCppSttEngine.open_session` so the WS layer can
    abort the turn cleanly (``end_reason:error``) per Annexe G instead of
    crashing.
    """


def _run_whisper(model: object, samples: object) -> Iterable[str]:
    """Run ``pywhispercpp`` transcription, returning per-segment text.

    Isolated so the ``model.transcribe`` call shape (which returns a list of
    segment objects with a ``.text`` attribute) is in one place and easy to
    adapt if the binding changes.
    """

    transcribe = model.transcribe  # type: ignore[attr-defined]
    result = transcribe(samples)
    out: list[str] = []
    for seg in result:
        text = getattr(seg, "text", None)
        if text is None and isinstance(seg, str):
            text = seg
        if isinstance(text, str):
            out.append(text)
    return out


# --- Default engine selection -----------------------------------------------

_default_engine: SttEngine | None = None
_default_lock = Lock()


def _build_engine(settings: Settings) -> SttEngine:
    if settings.STT_ENGINE == "fake":
        # The canned transcript is injected by the attest harness via
        # ``BOB_FAKE_STT_TRANSCRIPT`` (empty in normal dev/test → empty finals).
        # ``BOB_FAKE_STT_REVISE_TO`` (issue 0104) optionally overrides the FINAL
        # transcript to model an end-of-phrase revision; empty keeps final ==
        # streamed transcript.
        return FakeSttEngine(
            transcript=settings.BOB_FAKE_STT_TRANSCRIPT,
            revise_to=settings.BOB_FAKE_STT_REVISE_TO or None,
        )
    return WhisperCppSttEngine(settings)


def get_default_stt_engine() -> SttEngine:
    """Return the process-wide :class:`SttEngine` (created on demand)."""

    global _default_engine
    if _default_engine is not None:
        return _default_engine
    with _default_lock:
        if _default_engine is None:
            _default_engine = _build_engine(get_settings())
        return _default_engine


def set_default_stt_engine(engine: SttEngine | None) -> None:
    """Override the process-wide engine (tests / attest scenarios).

    Passing ``None`` resets to lazy construction from settings on next access.
    """

    global _default_engine
    with _default_lock:
        _default_engine = engine
