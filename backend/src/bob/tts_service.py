"""Local Kokoro TTS engine — single shared instance, off-loop inference.

The :class:`KokoroTtsService` wraps the ``kokoro-onnx`` runtime, loaded
lazily on first call so app startup stays cheap and tests don't have to
mock anything just to import the module. Synthesis runs in the default
thread pool via :func:`asyncio.to_thread` so a long generation never
blocks the FastAPI event loop.

The library returns a NumPy ``float32`` mono waveform plus a sample
rate. We convert to 16-bit little-endian PCM here because that is what
every consumer downstream (browser ``AudioContext``, ``ffplay``,
``afplay`` after a WAV wrap) actually wants.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from threading import Lock
from typing import TYPE_CHECKING, Any, cast

import structlog

from bob.config import Settings, get_settings
from bob.model_downloader import ensure_kokoro_ready

if TYPE_CHECKING:  # pragma: no cover - import only for type checkers
    from kokoro_onnx import Kokoro

_logger = structlog.get_logger(__name__)


@dataclass(frozen=True)
class SynthesisResult:
    """Output of one :meth:`KokoroTtsService.synthesize` call."""

    pcm16: bytes
    """Raw signed 16-bit little-endian PCM, mono, ``sample_rate`` Hz."""

    sample_rate: int


class KokoroTtsService:
    """Thread-safe wrapper around a single ``kokoro_onnx.Kokoro`` instance."""

    def __init__(self, settings: Settings | None = None) -> None:
        self._settings = settings or get_settings()
        self._model: Kokoro | None = None
        self._load_lock = Lock()

    def _ensure_loaded(self) -> Kokoro:
        if self._model is not None:
            return self._model
        with self._load_lock:
            if self._model is not None:
                return self._model
            paths = ensure_kokoro_ready(self._settings)
            # Imported lazily so importing this module is free of native deps.
            from kokoro_onnx import Kokoro

            _logger.info(
                "kokoro.model.load",
                model_path=str(paths.model_path),
                voices_path=str(paths.voices_path),
            )
            self._model = Kokoro(str(paths.model_path), str(paths.voices_path))
            return self._model

    def _synthesize_blocking(self, text: str, voice: str, speed: float) -> SynthesisResult:
        model = self._ensure_loaded()
        # ``kokoro_onnx.Kokoro.create`` returns (np.ndarray[float32], int).
        samples_any, sample_rate_any = cast(
            tuple[Any, Any],
            model.create(text, voice=voice, speed=speed, lang=self._settings.KOKORO_DEFAULT_LANG),
        )
        # Lazy numpy import: keeps the module importable on machines without
        # the runtime deps installed yet.
        import numpy as np

        samples = np.asarray(samples_any, dtype=np.float32)
        sample_rate = int(sample_rate_any)
        # Clip then convert to signed 16-bit little-endian PCM.
        clipped = np.clip(samples, -1.0, 1.0)
        pcm = (clipped * 32767.0).astype("<i2").tobytes()
        return SynthesisResult(pcm16=pcm, sample_rate=sample_rate)

    async def synthesize(
        self,
        text: str,
        *,
        voice: str | None = None,
        speed: float | None = None,
    ) -> SynthesisResult:
        """Run one synthesis off the event loop and return PCM16 bytes."""

        if not text.strip():
            raise ValueError("text must not be empty")
        chosen_voice = voice or self._settings.KOKORO_DEFAULT_VOICE
        chosen_speed = speed if speed is not None else self._settings.KOKORO_DEFAULT_SPEED
        return await asyncio.to_thread(self._synthesize_blocking, text, chosen_voice, chosen_speed)


_default_service: KokoroTtsService | None = None
_default_lock = Lock()


def get_default_tts_service() -> KokoroTtsService:
    """Return the process-wide :class:`KokoroTtsService` (created on demand)."""

    global _default_service
    if _default_service is not None:
        return _default_service
    with _default_lock:
        if _default_service is None:
            _default_service = KokoroTtsService()
        return _default_service
