"""End-to-end WS binary channel test for the « Listen » pipeline (issue 0099).

Mounts ONLY :mod:`bob.ws_router` on a bare FastAPI app (no app lifespan, so the
Kokoro/espeak boot that the full ``bob.main`` app runs is avoided — that boot
is unrelated to this slice and is environment-fragile). A deterministic fake
STT engine is injected via the router's provider seam, so the test drives the
REAL binary-frame path: ``voice_start`` → binary mic frames (tag ``0x01``) →
``voice_stop`` → ``stt_partial`` / ``stt_final`` on the socket.
"""

from __future__ import annotations

import struct
from collections.abc import Iterator
from typing import Any, cast

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette.testclient import WebSocketTestSession

from bob import ws_router
from bob.orchestrator import Orchestrator
from bob.stt_engine import MIC_FRAME_TAG, FakeSttEngine


class _NoopOrchestrator:
    """Stand-in so the WS handler can connect without a primed JarvisStore.

    The voice path never calls ``process_user_message`` (no ``user_msg`` is
    sent), so a stub that raises if called is enough to satisfy the connect-
    time ``_orchestrator_provider()`` without the app lifespan.
    """

    async def process_user_message(self, session_id: str, user_content: str) -> Any:
        raise AssertionError("orchestrator must not be called in the voice path")

    def set_user_typing(self, typing: bool) -> None:
        return None

    def set_live_transcript_state(self, live_state: Any) -> None:
        # PRD 0016 / issue 0102 — the voice path installs the Thinker's live
        # store on voice_start and resets it on voice_stop. The double accepts
        # it (the consult only matters when ``process_user_message`` runs, which
        # this path never does).
        return None


@pytest.fixture()
def voice_client() -> Iterator[TestClient]:
    """A TestClient over a bare app that mounts only the chat WS router.

    Injects a fake STT engine (transcript fixed) so the run is deterministic
    and native-free, and a no-op orchestrator so the handler can connect
    without the app lifespan. No lifespan is registered, so no Kokoro preload
    runs (that boot is unrelated to this slice and environment-fragile).
    """

    app = FastAPI()
    app.include_router(ws_router.router)
    ws_router.set_orchestrator_provider(lambda: cast(Orchestrator, _NoopOrchestrator()))
    ws_router.set_stt_engine_provider(
        lambda: FakeSttEngine(transcript="quel temps à paris", samples_per_word=160)
    )
    try:
        with TestClient(app) as client:
            yield client
    finally:
        ws_router.reset_stt_engine_provider()
        ws_router.reset_orchestrator_provider()


def _mic_frame(n_samples: int = 160) -> bytes:
    return bytes([MIC_FRAME_TAG]) + struct.pack(f"<{n_samples}h", *([0] * n_samples))


def _drain_until(
    ws: WebSocketTestSession, want_type: str, *, budget: int = 200
) -> list[dict[str, Any]]:
    """Receive JSON frames until one of ``want_type`` arrives. Returns all seen."""

    seen: list[dict[str, Any]] = []
    for _ in range(budget):
        frame = ws.receive_json()
        seen.append(frame)
        if frame.get("type") == want_type:
            return seen
    raise AssertionError(f"never saw {want_type!r}; saw {[f.get('type') for f in seen]}")


def test_binary_voice_turn_round_trip(voice_client: TestClient) -> None:
    with voice_client.websocket_connect("/ws/chat") as ws:
        session = ws.receive_json()
        assert session["type"] == "session"

        ws.send_json({"type": "voice_start", "window": "new", "ts_client": 0})
        # Stream enough audio to reveal the whole transcript (4 words *
        # samples_per_word=160 == 640 samples; send 8 frames of 160 samples).
        for _ in range(8):
            ws.send_bytes(_mic_frame(160))
        ws.send_json({"type": "voice_stop", "ts_client": 1})

        seen = _drain_until(ws, "stt_final")
        partials = [f for f in seen if f.get("type") == "stt_partial"]
        finals = [f for f in seen if f.get("type") == "stt_final"]

        assert partials, "expected at least one stt_partial on the socket"
        assert all("turn_id" in p and "stable_prefix_len" in p for p in partials)
        assert len(finals) == 1
        assert finals[0]["text"] == "quel temps à paris"
        # Partials + final share the same turn id.
        assert {p["turn_id"] for p in partials} == {finals[0]["turn_id"]}


def test_binary_frame_without_active_turn_is_dropped(voice_client: TestClient) -> None:
    with voice_client.websocket_connect("/ws/chat") as ws:
        ws.receive_json()  # session frame
        # No voice_start yet: a stray binary frame must be silently dropped and
        # the socket must remain usable for normal JSON traffic.
        ws.send_bytes(_mic_frame(160))
        ws.send_json({"type": "client_typing", "typing": True})
        ws.send_json({"type": "bogus_type"})
        err = ws.receive_json()
        assert err["type"] == "error"
        assert err["code"] == "bad_type"


def test_voice_stop_without_start_is_noop(voice_client: TestClient) -> None:
    with voice_client.websocket_connect("/ws/chat") as ws:
        ws.receive_json()
        ws.send_json({"type": "voice_stop", "ts_client": 0})
        # Socket still alive: a follow-up bad frame yields the usual error.
        ws.send_json({"type": "nope"})
        assert ws.receive_json()["type"] == "error"


def test_malformed_text_frame_keeps_socket_alive(voice_client: TestClient) -> None:
    with voice_client.websocket_connect("/ws/chat") as ws:
        ws.receive_json()
        # A non-JSON text frame must not tear the socket down (the receive loop
        # switched from receive_json() to a manual json.loads).
        ws.send_text("not json {{{")
        err = ws.receive_json()
        assert err["type"] == "error"
        assert err["code"] == "bad_payload"
        # Still usable afterwards.
        ws.send_json({"type": "client_typing", "typing": True})
        ws.send_json({"type": "x"})
        assert ws.receive_json()["type"] == "error"
