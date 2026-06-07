"""Unit tests for the harness FakeTtsService (PRD 0016 / issue 0100).

The fake TTS must be offline (no model load) and yield a deterministic, fixed
number of non-empty PCM chunks for non-empty text (and nothing for blank text),
so the ``bob attest --audio`` full-duplex scenario can attest audio-out without
Kokoro / espeak-ng.
"""

from __future__ import annotations

from bob.config import Settings
from bob.tts_service import (
    KOKORO_SAMPLE_RATE,
    FakeTtsService,
    KokoroTtsService,
    _build_tts_service,
)


def _settings(chunks: int = 2) -> Settings:
    return Settings.model_construct(TTS_ENGINE="fake", BOB_FAKE_TTS_CHUNKS=chunks)


async def test_yields_fixed_chunk_count() -> None:
    svc = FakeTtsService(_settings(chunks=3))
    chunks = [c async for c in svc.synthesize_stream("bonjour")]
    assert len(chunks) == 3
    assert all(c.pcm16 for c in chunks)  # non-empty PCM
    assert all(c.sample_rate == KOKORO_SAMPLE_RATE for c in chunks)


async def test_blank_text_yields_nothing() -> None:
    svc = FakeTtsService(_settings())
    assert [c async for c in svc.synthesize_stream("   ")] == []


def test_is_cached_and_preload_are_noops() -> None:
    svc = FakeTtsService(_settings())
    assert svc.is_model_cached() is True
    svc.preload()  # must not raise / load anything
    svc.warmup()


async def test_chunks_override_arg() -> None:
    svc = FakeTtsService(_settings(chunks=2), chunks=5)
    chunks = [c async for c in svc.synthesize_stream("salut")]
    assert len(chunks) == 5


def test_build_selects_engine_by_setting() -> None:
    # The pure selector builds a FakeTtsService iff TTS_ENGINE=fake, else Kokoro.
    fake = _build_tts_service(Settings.model_construct(TTS_ENGINE="fake"))
    assert isinstance(fake, FakeTtsService)
    real = _build_tts_service(Settings.model_construct(TTS_ENGINE="kokoro"))
    assert isinstance(real, KokoroTtsService) and not isinstance(real, FakeTtsService)
