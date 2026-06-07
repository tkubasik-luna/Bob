"""Local Kokoro TTS engine — streaming KPipeline with single-shot back-compat.

The :class:`KokoroTtsService` wraps the upstream ``kokoro.KPipeline`` runtime
(misaki G2P + PyTorch model, ``hexgrad/Kokoro-82M``).

Engine architecture
-------------------

KPipeline is **not thread-safe**: the underlying PyTorch model is a single
mutable instance and concurrent ``pipeline(...)`` calls would race. We
serialize all KPipeline access through ``_synth_lock`` (a plain
:class:`threading.Lock`). Multiple WS sessions / concurrent synth requests
queue at the lock; the event loop is never blocked because acquisition
happens inside the producer thread.

Streaming
---------

KPipeline already yields ``(graphemes, phonemes, audio)`` tuples chunk by
chunk (one ~250 ms PCM block per yield). The previous implementation
threw that streaming away by concatenating into a single sentence-sized
blob before the WS started sending. :meth:`synthesize_stream` instead
pumps each chunk across an :class:`asyncio.Queue` so the WS router can
push it to the client immediately. First-audio latency drops from
"full sentence synth" to "first KPipeline chunk".

Text preprocessing
------------------

Text is run through :func:`bob.text_normalizer.normalize_for_tts` before
hitting KPipeline. This replaces curly quotes, dashes, ellipsis, NBSP,
emoji, etc. — the punctuation/symbol noise that historically broke the
phonemizer with ``"number of lines in input and output must be equal"``.
The previous post-facto retry hack (which silently dropped words) is gone.

Warmup
------

:meth:`warmup` runs a tiny synthesis after preload so the first user
message doesn't pay the PyTorch graph capture / voice-tensor load cost.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import AsyncIterator
from dataclasses import dataclass
from pathlib import Path
from threading import Lock
from typing import TYPE_CHECKING, Any, cast

import structlog

from bob.config import Settings, get_settings
from bob.text_normalizer import normalize_for_tts

if TYPE_CHECKING:  # pragma: no cover
    from kokoro import KPipeline

_logger = structlog.get_logger(__name__)

# Kokoro's vocoder output rate. Constant of the model — not a config dial.
KOKORO_SAMPLE_RATE: int = 24_000

_WARMUP_TEXT = "Bonjour."


@dataclass(frozen=True)
class SynthesisChunk:
    """One chunk yielded by :meth:`KokoroTtsService.synthesize_stream`."""

    pcm16: bytes
    """Signed 16-bit little-endian PCM, mono, ``sample_rate`` Hz."""

    sample_rate: int


@dataclass(frozen=True)
class SynthesisResult:
    """Full PCM for one :meth:`KokoroTtsService.synthesize` call."""

    pcm16: bytes
    sample_rate: int


def _hf_cache_dir_for(repo_id: str) -> Path:
    safe = "models--" + repo_id.replace("/", "--")
    return Path.home() / ".cache" / "huggingface" / "hub" / safe


def _audio_to_pcm16(audio: Any) -> bytes:
    """Convert a KPipeline audio chunk (torch tensor) to signed 16-bit LE PCM."""

    import numpy as np

    arr = audio.detach().cpu().numpy().astype(np.float32, copy=False)
    clipped = np.clip(arr, -1.0, 1.0)
    return bytes((clipped * 32767.0).astype("<i2").tobytes())


class KokoroTtsService:
    """Thread-safe streaming wrapper around a single KPipeline instance."""

    sample_rate: int = KOKORO_SAMPLE_RATE

    def __init__(self, settings: Settings | None = None) -> None:
        self._settings = settings or get_settings()
        self._pipeline: KPipeline | None = None
        self._load_lock = Lock()
        # Guards every KPipeline(...) call. KPipeline holds a single PyTorch
        # model + voice tensor cache that is not safe for concurrent use.
        self._synth_lock = Lock()

    def is_model_cached(self) -> bool:
        cache = _hf_cache_dir_for(self._settings.KOKORO_HF_REPO_ID)
        return (cache / "snapshots").exists()

    def preload(self) -> None:
        """Force model load (downloading if absent) ahead of any synthesis call."""

        self._ensure_loaded()

    def warmup(self) -> None:
        """Run one tiny synthesis to JIT the graph + warm voice tensor cache.

        Discards output. Safe to call multiple times; subsequent calls are
        essentially free once the first one has run.
        """

        pipeline = self._ensure_loaded()
        voice = self._settings.KOKORO_DEFAULT_VOICE
        speed = self._settings.KOKORO_DEFAULT_SPEED
        _logger.info("kokoro.warmup.begin", voice=voice)
        with self._synth_lock:
            generator = cast(
                Any,
                pipeline(_WARMUP_TEXT, voice=voice, speed=speed),
            )
            for _gs, _ps, _audio in generator:
                pass
        _logger.info("kokoro.warmup.done")

    def _ensure_loaded(self) -> KPipeline:
        if self._pipeline is not None:
            return self._pipeline
        with self._load_lock:
            if self._pipeline is not None:
                return self._pipeline
            from kokoro import KPipeline

            _logger.info(
                "kokoro.pipeline.load",
                repo_id=self._settings.KOKORO_HF_REPO_ID,
                lang_code=self._settings.KOKORO_LANG_CODE,
            )
            self._pipeline = KPipeline(
                lang_code=self._settings.KOKORO_LANG_CODE,
                repo_id=self._settings.KOKORO_HF_REPO_ID,
            )
            return self._pipeline

    async def synthesize_stream(
        self,
        text: str,
        *,
        voice: str | None = None,
        speed: float | None = None,
    ) -> AsyncIterator[SynthesisChunk]:
        """Yield PCM16 chunks as KPipeline produces them.

        Runs KPipeline in a background thread (it's sync) and bridges each
        ``(_, _, audio)`` tuple back to the event loop via an
        :class:`asyncio.Queue`. The thread holds :attr:`_synth_lock` for the
        duration of the iteration so concurrent calls serialize.

        Cancellation: if the consuming coroutine is cancelled mid-stream, the
        producer thread keeps running until KPipeline finishes the current
        text (we can't preempt the torch model from outside its loop). That
        is fine — the consumer simply stops draining the queue and we let
        the thread complete in the background.
        """

        if not text.strip():
            return

        normalized = normalize_for_tts(text)
        if not normalized:
            return

        chosen_voice = voice or self._settings.KOKORO_DEFAULT_VOICE
        chosen_speed = speed if speed is not None else self._settings.KOKORO_DEFAULT_SPEED

        loop = asyncio.get_running_loop()
        queue: asyncio.Queue[SynthesisChunk | Exception | None] = asyncio.Queue()

        def producer() -> None:
            try:
                pipeline = self._ensure_loaded()
                with self._synth_lock:
                    generator = cast(
                        Any,
                        pipeline(normalized, voice=chosen_voice, speed=chosen_speed),
                    )
                    for _gs, _ps, audio in generator:
                        pcm = _audio_to_pcm16(audio)
                        if not pcm:
                            continue
                        chunk = SynthesisChunk(pcm16=pcm, sample_rate=self.sample_rate)
                        loop.call_soon_threadsafe(queue.put_nowait, chunk)
            except Exception as exc:
                loop.call_soon_threadsafe(queue.put_nowait, exc)
            finally:
                loop.call_soon_threadsafe(queue.put_nowait, None)

        producer_task = loop.run_in_executor(None, producer)
        try:
            while True:
                item = await queue.get()
                if item is None:
                    break
                if isinstance(item, Exception):
                    raise item
                yield item
        finally:
            # Producer will finish on its own (we can't kill sync KPipeline).
            # Awaiting here ensures the thread is reaped before we return.
            with contextlib.suppress(Exception, asyncio.CancelledError):
                await producer_task

    async def synthesize(
        self,
        text: str,
        *,
        voice: str | None = None,
        speed: float | None = None,
    ) -> SynthesisResult:
        """Back-compat single-shot: drain :meth:`synthesize_stream` into one blob.

        Kept for callers (and tests) that don't need streaming semantics. New
        code should prefer :meth:`synthesize_stream`.
        """

        if not text.strip():
            raise ValueError("text must not be empty")

        parts: list[bytes] = []
        async for chunk in self.synthesize_stream(text, voice=voice, speed=speed):
            parts.append(chunk.pcm16)
        return SynthesisResult(pcm16=b"".join(parts), sample_rate=self.sample_rate)


class FakeTtsService(KokoroTtsService):
    """Deterministic, offline, native-free TTS for the attestation harness.

    PRD 0016 / issue 0100. Subclasses :class:`KokoroTtsService` so it slots into
    the same provider type but overrides every Kokoro / KPipeline touch-point:
    it loads nothing and :meth:`synthesize_stream` yields a fixed number of
    short *silent* PCM16 chunks for any non-empty text (empty text yields
    nothing, matching the real engine). This lets the ``bob attest --audio``
    full-duplex scenario drive audio-out → ``audio_chunk`` events → the FSM
    ``bob_speaking`` transition with zero dependency on espeak-ng / torch, the
    same way :class:`bob.attest.fake_backend.FakeLlmClient` /
    :class:`bob.stt_engine.FakeSttEngine` fake their layers.
    """

    #: ~50 ms of s16le silence at the model sample rate — small but non-empty.
    _CHUNK_SAMPLES = 1200

    def __init__(self, settings: Settings | None = None, *, chunks: int | None = None) -> None:
        super().__init__(settings)
        resolved = chunks if chunks is not None else self._settings.BOB_FAKE_TTS_CHUNKS
        self._chunks = max(1, resolved)
        # Per-chunk pacing (issue 0101): 0 = instant (default). The barge-in
        # scenario sets a small delay so Bob holds the floor long enough to be
        # interrupted mid-reply.
        self._chunk_ms = max(0, self._settings.BOB_FAKE_TTS_CHUNK_MS)

    def is_model_cached(self) -> bool:
        return True

    def preload(self) -> None:
        return None

    def warmup(self) -> None:
        return None

    async def synthesize_stream(
        self,
        text: str,
        *,
        voice: str | None = None,
        speed: float | None = None,
    ) -> AsyncIterator[SynthesisChunk]:
        if not text.strip():
            return
        pcm = b"\x00\x00" * self._CHUNK_SAMPLES
        for _ in range(self._chunks):
            if self._chunk_ms:
                # Pacing so a barge-in window can land mid-reply (issue 0101).
                # Cooperative: a cancel (barge-in / new turn) interrupts here.
                await asyncio.sleep(self._chunk_ms / 1000.0)
            yield SynthesisChunk(pcm16=pcm, sample_rate=self.sample_rate)


_default_service: KokoroTtsService | None = None
_default_lock = Lock()


def _build_tts_service(settings: Settings) -> KokoroTtsService:
    if settings.TTS_ENGINE == "fake":
        return FakeTtsService(settings)
    return KokoroTtsService(settings)


def get_default_tts_service() -> KokoroTtsService:
    """Return the process-wide TTS service (created on demand from settings)."""

    global _default_service
    if _default_service is not None:
        return _default_service
    with _default_lock:
        if _default_service is None:
            _default_service = _build_tts_service(get_settings())
        return _default_service
