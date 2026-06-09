"""Say-path integration tests for the speech pipeline rewiring (issue 0121).

Two layers:

1. Direct ``ws_router._synthesize_and_stream`` runs against a stub WebSocket +
   a chunk-yielding fake TTS — asserting the wire contract (``audio_start`` →
   PCM bytes → ``audio_end``), the 0117 marks, the batched ``audio_chunk_batch``
   debug events (and the absence of per-chunk ones), and the per-sentence
   error degrade.
2. A full ``/ws/chat`` round trip over :class:`fastapi.testclient.TestClient`
   with ``voice: true`` — the real say-path spawn, through the pipeline, with
   nominal client-side playback frames unchanged.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Iterator
from typing import Any, cast

import pytest
from fastapi import WebSocket
from fastapi.testclient import TestClient
from starlette.testclient import WebSocketTestSession

from bob import debug_log, turn_metrics, ws_router
from bob import jarvis_store as jarvis_store_module
from bob.main import app
from bob.orchestrator import Orchestrator, OrchestratorResponse
from bob.tts_service import KokoroTtsService, SynthesisChunk
from bob.turn_metrics import TurnLatencyMetrics

_SAMPLE_RATE = 24_000


class _ChunkedFakeTts:
    """Chunk-yielding TTS double: N instant PCM chunks per non-empty sentence."""

    def __init__(self, chunks_per_sentence: int = 2, fail_on: str | None = None) -> None:
        self._chunks = chunks_per_sentence
        self._fail_on = fail_on
        self.sentences: list[str] = []

    def is_model_cached(self) -> bool:
        return True

    async def synthesize_stream(
        self,
        text: str,
        *,
        voice: str | None = None,
        speed: float | None = None,
    ) -> AsyncIterator[SynthesisChunk]:
        if not text.strip():
            return
        self.sentences.append(text)
        if self._fail_on is not None and self._fail_on in text:
            raise RuntimeError("phonemizer exploded")
        for _ in range(self._chunks):
            yield SynthesisChunk(pcm16=b"\x00\x00" * 32, sample_rate=_SAMPLE_RATE)


class _StubWebSocket:
    """Records the say-path's outbound frames (JSON + binary), in order."""

    def __init__(self) -> None:
        self.frames: list[tuple[str, Any]] = []

    async def send_json(self, frame: dict[str, Any]) -> None:
        self.frames.append(("json", frame))

    async def send_bytes(self, data: bytes) -> None:
        self.frames.append(("bytes", data))

    def json_types(self) -> list[str]:
        return [frame["type"] for kind, frame in self.frames if kind == "json"]

    def binary_count(self) -> int:
        return sum(1 for kind, _ in self.frames if kind == "bytes")


@pytest.fixture()
def fake_tts() -> Iterator[_ChunkedFakeTts]:
    fake = _ChunkedFakeTts()
    ws_router.set_tts_service_provider(lambda: cast(KokoroTtsService, fake))
    debug_log.clear()
    try:
        yield fake
    finally:
        ws_router.reset_tts_service_provider()


def _voice_events(subtype: str) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for event in debug_log.snapshot():
        if event.category != "voice":
            continue
        ws_event = (event.payload or {}).get("ws_event") or {}
        if ws_event.get("type") == subtype:
            out.append(ws_event)
    return out


# --- direct say-path runs ---------------------------------------------------------


async def test_say_path_streams_start_chunks_end_via_pipeline(
    fake_tts: _ChunkedFakeTts,
) -> None:
    stub = _StubWebSocket()
    await ws_router._synthesize_and_stream(
        cast(WebSocket, stub), "sess-1", "msg-1", "Première phrase. Deuxième phrase."
    )

    # Nominal playback contract unchanged: one audio_start, all PCM frames,
    # one final audio_end. Both sentences were synthesized (2 chunks each).
    assert stub.json_types() == ["audio_start", "audio_end"]
    assert stub.binary_count() == 4
    assert fake_tts.sentences == ["Première phrase.", "Deuxième phrase."]
    first_json = next(frame for kind, frame in stub.frames if kind == "json")
    assert first_json == {"type": "audio_start", "msg_id": "msg-1", "sample_rate": _SAMPLE_RATE}


async def test_say_path_emits_batched_summary_not_per_chunk_events(
    fake_tts: _ChunkedFakeTts,
) -> None:
    stub = _StubWebSocket()
    await ws_router._synthesize_and_stream(
        cast(WebSocket, stub), "sess-1", "msg-2", "Première phrase. Deuxième phrase."
    )

    # Issue 0121: zero per-chunk debug events on the say-path; the batched
    # summaries account for every outbound chunk exactly once.
    assert _voice_events("audio_chunk") == []
    batches = _voice_events("audio_chunk_batch")
    assert batches, "expected at least one audio_chunk_batch event"
    assert sum(batch["count"] for batch in batches) == 4
    assert all(batch["msg_id"] == "msg-2" for batch in batches)
    assert sum(batch["bytes"] for batch in batches) == 4 * 64


async def test_say_path_places_0117_marks_on_a_metered_turn(
    fake_tts: _ChunkedFakeTts,
) -> None:
    collector = TurnLatencyMetrics()
    turn_metrics.set_default_collector(collector)
    token = turn_metrics.current_metrics_turn_id.set("turn-0121")
    try:
        collector.begin_turn("turn-0121")
        stub = _StubWebSocket()
        await ws_router._synthesize_and_stream(
            cast(WebSocket, stub), "sess-1", "msg-3", "Bonjour à tous."
        )
        summary = collector.finish_turn("turn-0121")
    finally:
        turn_metrics.current_metrics_turn_id.reset(token)
        turn_metrics.set_default_collector(None)

    assert summary is not None
    stages = cast(dict[str, float], summary["stages_ms"])
    assert "tts_first_chunk" in stages
    assert "audio_first_byte" in stages


async def test_say_path_sentence_error_degrades_to_audio_error_and_continues() -> None:
    fake = _ChunkedFakeTts(fail_on="cassée")
    ws_router.set_tts_service_provider(lambda: cast(KokoroTtsService, fake))
    debug_log.clear()
    try:
        stub = _StubWebSocket()
        await ws_router._synthesize_and_stream(
            cast(WebSocket, stub), "sess-1", "msg-4", "Ça marche. Phrase cassée. Toujours là."
        )
    finally:
        ws_router.reset_tts_service_provider()

    # One audio_error for the broken sentence, the other two still played,
    # and the stream still terminated cleanly.
    assert stub.json_types() == ["audio_start", "audio_error", "audio_end"]
    assert stub.binary_count() == 4


async def test_say_path_empty_text_sends_only_audio_end(fake_tts: _ChunkedFakeTts) -> None:
    stub = _StubWebSocket()
    await ws_router._synthesize_and_stream(cast(WebSocket, stub), "sess-1", "msg-5", "   ")
    assert stub.frames == [("json", {"type": "audio_end", "msg_id": "msg-5"})]


# --- full /ws/chat round trip ------------------------------------------------------


class _FakeOrchestrator:
    def __init__(self, speech: str) -> None:
        self._speech = speech

    async def process_user_message(
        self, session_id: str, user_content: str
    ) -> OrchestratorResponse:
        store = jarvis_store_module.get_default_store()
        store.append("user", user_content)
        store.append("assistant", self._speech)
        return OrchestratorResponse(speech=self._speech, ui=[])


def _drain_audio(ws: WebSocketTestSession) -> tuple[list[dict[str, Any]], int]:
    """Receive frames until ``audio_end``; return (JSON frames, binary count)."""

    json_frames: list[dict[str, Any]] = []
    binary = 0
    for _ in range(200):
        message = ws.receive()
        if message.get("bytes") is not None:
            binary += 1
            continue
        text = message.get("text")
        if text is None:
            continue
        frame = json.loads(text)
        json_frames.append(frame)
        if frame.get("type") == "audio_end":
            return json_frames, binary
    raise AssertionError(f"never saw audio_end; saw {[f.get('type') for f in json_frames]}")


def test_voiced_chat_turn_goes_through_the_pipeline(clear_jarvis_history: None) -> None:
    fake_tts = _ChunkedFakeTts(chunks_per_sentence=3)
    ws_router.set_orchestrator_provider(
        lambda: cast(Orchestrator, _FakeOrchestrator("Voici la météo. Il fait beau."))
    )
    ws_router.set_tts_service_provider(lambda: cast(KokoroTtsService, fake_tts))
    debug_log.clear()
    try:
        with TestClient(app) as client, client.websocket_connect("/ws/chat") as ws:
            ws.receive_json()  # session frame
            ws.send_json({"type": "user_msg", "content": "météo ?", "voice": True})
            json_frames, binary = _drain_audio(ws)
    finally:
        ws_router.reset_tts_service_provider()
        ws_router.reset_orchestrator_provider()

    types = [frame["type"] for frame in json_frames]
    # The standard turn frames still arrive, then the audio stream of the
    # pipelined say-path: one start, 2 sentences x 3 chunks of PCM, one end.
    assert types.count("assistant_msg") == 1
    assert types.count("audio_start") == 1
    assert types.count("audio_end") == 1
    assert "audio_error" not in types
    assert binary == 6
    assert fake_tts.sentences == ["Voici la météo.", "Il fait beau."]

    # Observability went batched: no per-chunk audio_chunk event from the
    # say-path, and the batch totals cover all 6 chunks.
    assert _voice_events("audio_chunk") == []
    assert sum(batch["count"] for batch in _voice_events("audio_chunk_batch")) == 6
