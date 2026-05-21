"""Debug HTTP endpoints — manual verification of backend slices.

Currently exposes ``POST /debug/tts`` which synthesizes a short FR
utterance with Kokoro and returns a WAV file. Intended for ``curl`` /
``afplay`` smoke testing while the real WebSocket voice pipeline is
still being wired up.
"""

from __future__ import annotations

import struct
from collections.abc import Callable

import structlog
from fastapi import APIRouter, HTTPException, Response
from pydantic import BaseModel, Field

from bob.tts_service import KokoroTtsService, SynthesisResult, get_default_tts_service

router = APIRouter(prefix="/debug", tags=["debug"])
_logger = structlog.get_logger(__name__)

# DI seam so tests / future call sites can swap the service factory without
# touching FastAPI internals. Mirrors the ws_router pattern.
_tts_service_provider: Callable[[], KokoroTtsService] = get_default_tts_service


def set_tts_service_provider(provider: Callable[[], KokoroTtsService]) -> None:
    """Override the TTS-service factory used by the debug endpoint."""

    global _tts_service_provider
    _tts_service_provider = provider


def reset_tts_service_provider() -> None:
    """Restore the default TTS-service factory."""

    global _tts_service_provider
    _tts_service_provider = get_default_tts_service


class DebugTtsRequest(BaseModel):
    """Body for ``POST /debug/tts``."""

    text: str = Field(..., min_length=1, description="Text to synthesize.")


def _wrap_pcm16_as_wav(pcm16: bytes, sample_rate: int) -> bytes:
    """Wrap mono 16-bit little-endian PCM in a minimal WAV (RIFF) container."""

    num_channels = 1
    bits_per_sample = 16
    byte_rate = sample_rate * num_channels * bits_per_sample // 8
    block_align = num_channels * bits_per_sample // 8
    data_size = len(pcm16)
    fmt_chunk = struct.pack(
        "<4sIHHIIHH",
        b"fmt ",
        16,
        1,  # PCM format code
        num_channels,
        sample_rate,
        byte_rate,
        block_align,
        bits_per_sample,
    )
    data_chunk = struct.pack("<4sI", b"data", data_size) + pcm16
    riff_size = 4 + len(fmt_chunk) + len(data_chunk)
    return struct.pack("<4sI4s", b"RIFF", riff_size, b"WAVE") + fmt_chunk + data_chunk


@router.post(
    "/tts",
    responses={200: {"content": {"audio/wav": {}}}},
)
async def debug_tts(payload: DebugTtsRequest) -> Response:
    """Synthesize ``payload.text`` with the default voice and return a WAV."""

    service = _tts_service_provider()
    try:
        result: SynthesisResult = await service.synthesize(payload.text)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        _logger.exception("debug.tts.failed", text_len=len(payload.text))
        raise HTTPException(status_code=500, detail="TTS synthesis failed") from exc

    wav = _wrap_pcm16_as_wav(result.pcm16, result.sample_rate)
    _logger.info(
        "debug.tts.ok",
        text_len=len(payload.text),
        pcm_bytes=len(result.pcm16),
        sample_rate=result.sample_rate,
    )
    return Response(content=wav, media_type="audio/wav")
