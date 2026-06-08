"""Unit tests for the « Listen » STT engine (issue 0099).

Covers the binary frame decoder (tag stripping + endianness + validation) and
the deterministic fake engine (word-by-word partials → stable final). The real
whisper.cpp engine is exercised by a single test MARKED slow/optional that
skips when ``pywhispercpp`` (or its model) is absent — CI never needs it.
"""

from __future__ import annotations

import struct

import pytest

from bob.stt_engine import (
    MIC_FRAME_TAG,
    FakeSttEngine,
    SttFinal,
    SttFrameError,
    SttPartial,
    WhisperCppSttEngine,
    decode_pcm_frame,
    pcm16_sample_count,
)


def _frame(*samples: int, tag: int = MIC_FRAME_TAG) -> bytes:
    """Build a tagged binary frame from s16le samples."""

    return bytes([tag]) + struct.pack(f"<{len(samples)}h", *samples)


def test_decode_strips_tag_and_returns_pcm() -> None:
    frame = _frame(0, 1000, -1000, 32767)
    pcm = decode_pcm_frame(frame)
    assert pcm == struct.pack("<4h", 0, 1000, -1000, 32767)
    assert pcm16_sample_count(pcm) == 4


def test_decode_preserves_little_endian_sample_values() -> None:
    # 0x3412 little-endian == 0x1234 big-endian; assert we read LE.
    frame = bytes([MIC_FRAME_TAG]) + b"\x34\x12"
    pcm = decode_pcm_frame(frame)
    (value,) = struct.unpack("<h", pcm)
    assert value == 0x1234


def test_decode_rejects_empty_frame() -> None:
    with pytest.raises(SttFrameError):
        decode_pcm_frame(b"")


def test_decode_rejects_unknown_tag() -> None:
    with pytest.raises(SttFrameError):
        decode_pcm_frame(_frame(1, 2, tag=0x02))


def test_decode_rejects_odd_length_payload() -> None:
    # 3 payload bytes after the tag is not a whole number of s16le samples.
    with pytest.raises(SttFrameError):
        decode_pcm_frame(bytes([MIC_FRAME_TAG, 0x01, 0x02, 0x03]))


def test_fake_engine_reveals_words_as_audio_accumulates() -> None:
    engine = FakeSttEngine(transcript="bonjour le monde", samples_per_word=4)
    session = engine.open_session("turn-1")
    assert engine.opened_turns == ["turn-1"]

    pcm = struct.pack("<4h", 0, 0, 0, 0)  # 4 samples / frame == 1 word / frame
    seen: list[SttPartial] = []
    for _ in range(3):
        seen.extend(session.accept_frame(pcm))

    assert [p.text for p in seen] == ["bonjour", "bonjour le", "bonjour le monde"]
    # The trailing token is tentative — the stable prefix excludes it.
    assert seen[1].stable_prefix_len == len("bonjour")
    final = session.finalize()
    assert isinstance(final, SttFinal)
    assert final.text == "bonjour le monde"


def test_fake_engine_is_always_cached_and_preload_is_noop() -> None:
    engine = FakeSttEngine(transcript="x")
    assert engine.is_model_cached() is True
    engine.preload()  # must not raise / must not download anything


def test_fake_engine_empty_transcript_yields_no_partials() -> None:
    engine = FakeSttEngine(transcript="", samples_per_word=1)
    session = engine.open_session("t")
    assert session.accept_frame(struct.pack("<8h", *([0] * 8))) == []
    assert session.finalize().text == ""


# --- Windowed partials (anti-quadratic) — native-free via a stub engine ------


class _RecordingEngine:
    """A WhisperCppSttEngine stand-in that records each transcribe call size.

    Returns a deterministic text derived from the byte length so partials
    "change" and get emitted, while letting the test assert that PARTIAL passes
    are capped to the trailing window and the FINAL pass sees the whole buffer.
    """

    def __init__(self) -> None:
        self.partial_sizes: list[int] = []
        self.calls: list[int] = []

    def transcribe_pcm(self, pcm: bytes) -> str:
        self.calls.append(len(pcm))
        return f"len={len(pcm)}"


def test_whisper_session_windows_partial_pass_but_finalizes_full_buffer() -> None:
    from bob.stt_engine import _WhisperCppSession  # internal: bounded-window logic

    engine = _RecordingEngine()
    # window = 4 samples (8 bytes); cadence = every 2 samples of new audio.
    session = _WhisperCppSession(
        engine,  # type: ignore[arg-type]
        partial_every_samples=2,
        partial_window_samples=4,
    )

    # Feed 10 samples (20 bytes) in 5 frames of 2 samples each.
    frame = struct.pack("<2h", 0, 0)  # 2 samples, 4 bytes
    for _ in range(5):
        session.accept_frame(frame)

    # Every PARTIAL pass transcribed at most the trailing window (4 samples = 8 B).
    assert engine.calls, "expected at least one partial pass"
    assert max(engine.calls) <= 4 * 2  # window_samples * bytes_per_sample
    # The FINAL pass sees the FULL 10-sample buffer (20 bytes), not the window.
    final = session.finalize()
    assert engine.calls[-1] == 10 * 2
    assert final.text == "len=20"


def test_whisper_session_whole_buffer_when_window_disabled() -> None:
    from bob.stt_engine import _WhisperCppSession

    engine = _RecordingEngine()
    session = _WhisperCppSession(
        engine,  # type: ignore[arg-type]
        partial_every_samples=2,
        partial_window_samples=0,  # disabled → legacy whole-buffer partials
    )
    frame = struct.pack("<2h", 0, 0)
    for _ in range(5):
        session.accept_frame(frame)
    # With the cap disabled, the last partial pass saw the full buffer so far.
    assert max(engine.calls) == 10 * 2


# --- Real engine (slow / optional) ------------------------------------------


def _whisper_available() -> bool:
    try:
        import pywhispercpp.model  # type: ignore[import-not-found]  # noqa: F401
    except Exception:
        return False
    engine = WhisperCppSttEngine()
    return engine.is_model_cached()


@pytest.mark.slow
@pytest.mark.skipif(
    not _whisper_available(),
    reason="pywhispercpp + whisper model not installed (native, opt-in)",
)
def test_whisper_cpp_engine_transcribes_fixture() -> None:  # pragma: no cover - native
    """Real whisper.cpp transcription of a fixture (opt-in, native model).

    Skipped unless ``pywhispercpp`` AND a cached model are present. This is the
    only test that touches the native engine; it never runs in the default CI
    path (the suite is deterministic via :class:`FakeSttEngine`).
    """

    from pathlib import Path

    fixture = Path(__file__).parent / "fixtures" / "audio" / "fr_sample.wav"
    if not fixture.exists():
        pytest.skip("no real FR fixture present")

    from bob.attest.drive import wav_to_pcm16_frames

    engine = WhisperCppSttEngine()
    session = engine.open_session("real")
    for frame in wav_to_pcm16_frames(fixture):
        from bob.stt_engine import decode_pcm_frame as _decode

        session.accept_frame(_decode(frame))
    final = session.finalize()
    assert final.text.strip() != ""
