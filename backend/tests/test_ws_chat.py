"""WS plumbing tests for /ws/chat — chat_service wired via DI seam."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Iterator
from typing import cast

import pytest
from fastapi.testclient import TestClient

from bob import conversation as conversation_module
from bob import ws_router
from bob.chat_service import ChatService
from bob.main import app
from bob.tts_service import KokoroTtsService, SynthesisChunk
from bob.ui_registry import ComponentDescriptor, ParsedResponse
from bob.ws_router import _sessions


class _FakeChatService:
    """Stand-in for ChatService that records calls and returns a canned reply."""

    def __init__(self, parsed: ParsedResponse) -> None:
        self._parsed = parsed
        self.calls: list[tuple[str, str]] = []

    async def handle_user_message(self, session_id: str, user_content: str) -> ParsedResponse:
        self.calls.append((session_id, user_content))
        # Mimic real ChatService side-effect of growing the conversation history.
        conversation_module.get_default_store().append(session_id, "user", user_content)
        conversation_module.get_default_store().append(session_id, "assistant", self._parsed.speech)
        return self._parsed


@pytest.fixture()
def fake_chat_service() -> Iterator[_FakeChatService]:
    parsed = ParsedResponse(
        speech="echo: hi",
        ui=[ComponentDescriptor(component="Markdown", props={"content": "**bold**"})],
    )
    fake = _FakeChatService(parsed)
    ws_router.set_chat_service_provider(lambda: cast(ChatService, fake))
    try:
        yield fake
    finally:
        ws_router.reset_chat_service_provider()


def test_ws_chat_full_round_trip(fake_chat_service: _FakeChatService) -> None:
    client = TestClient(app)
    with client.websocket_connect("/ws/chat") as ws:
        session_frame = ws.receive_json()
        assert session_frame["type"] == "session"
        session_id = session_frame["session_id"]
        assert isinstance(session_id, str) and len(session_id) == 32
        assert session_id in _sessions

        ws.send_json({"type": "user_msg", "content": "hi"})

        thinking_start = ws.receive_json()
        assert thinking_start == {"type": "thinking", "state": "start"}

        assistant = ws.receive_json()
        # `msg_id` is a fresh uuid hex; assert structurally and check the rest by equality.
        assert assistant["type"] == "assistant_msg"
        assert isinstance(assistant["msg_id"], str) and len(assistant["msg_id"]) == 32
        assert assistant["speech"] == "echo: hi"
        assert assistant["ui"] == [{"component": "Markdown", "props": {"content": "**bold**"}}]

        thinking_end = ws.receive_json()
        assert thinking_end == {"type": "thinking", "state": "end"}

    # Session-local state and conversation history both cleaned up on disconnect.
    assert session_id not in _sessions
    assert conversation_module.get_default_store().get_history(session_id) == []
    assert fake_chat_service.calls == [(session_id, "hi")]


def test_ws_chat_rejects_unknown_type(fake_chat_service: _FakeChatService) -> None:
    client = TestClient(app)
    with client.websocket_connect("/ws/chat") as ws:
        ws.receive_json()  # session frame
        ws.send_json({"type": "nope"})
        err = ws.receive_json()
        assert err["type"] == "error"
        assert err["code"] == "bad_type"
    assert fake_chat_service.calls == []


class _SlowFakeTts:
    """TTS double whose ``synthesize_stream`` blocks until released or cancelled."""

    def __init__(self) -> None:
        self.release = asyncio.Event()
        self.entered = asyncio.Event()
        self.cancelled = False

    async def synthesize_stream(
        self,
        text: str,
        *,
        voice: str | None = None,
        speed: float | None = None,
    ) -> AsyncIterator[SynthesisChunk]:
        self.entered.set()
        try:
            await self.release.wait()
        except asyncio.CancelledError:
            self.cancelled = True
            raise
        # 4 bytes = 2 samples of silence; payload content doesn't matter.
        yield SynthesisChunk(pcm16=b"\x00\x00\x00\x00", sample_rate=24_000)


def test_ws_chat_interrupts_in_flight_tts(fake_chat_service: _FakeChatService) -> None:
    """A new user_msg must cancel a previous turn's TTS and emit audio_end."""

    slow_tts = _SlowFakeTts()
    ws_router.set_tts_service_provider(lambda: cast(KokoroTtsService, slow_tts))
    try:
        client = TestClient(app)
        with client.websocket_connect("/ws/chat") as ws:
            ws.receive_json()  # session
            ws.send_json({"type": "user_msg", "content": "hi", "voice": True})
            assert ws.receive_json()["type"] == "thinking"
            first_assistant = ws.receive_json()
            assert first_assistant["type"] == "assistant_msg"
            first_msg_id = first_assistant["msg_id"]
            assert ws.receive_json() == {"type": "thinking", "state": "end"}

            # Second message arrives while TTS is still blocking inside synthesize().
            ws.send_json({"type": "user_msg", "content": "stop"})

            # First frame after interruption is the audio_end emitted by the
            # cancelling path for the previous msg_id.
            frame = ws.receive_json()
            assert frame == {"type": "audio_end", "msg_id": first_msg_id}

            # Then the normal flow for the new turn resumes.
            assert ws.receive_json() == {"type": "thinking", "state": "start"}
            second = ws.receive_json()
            assert second["type"] == "assistant_msg"
            assert second["msg_id"] != first_msg_id
            assert ws.receive_json() == {"type": "thinking", "state": "end"}

        # Cancellation actually propagated into the TTS coroutine.
        assert slow_tts.cancelled is True
    finally:
        ws_router.reset_tts_service_provider()


def test_ws_chat_interrupt_cancels_even_when_voice_off(
    fake_chat_service: _FakeChatService,
) -> None:
    """Voice-off second message still cancels a voice-on first message's TTS."""

    slow_tts = _SlowFakeTts()
    ws_router.set_tts_service_provider(lambda: cast(KokoroTtsService, slow_tts))
    try:
        client = TestClient(app)
        with client.websocket_connect("/ws/chat") as ws:
            ws.receive_json()  # session
            ws.send_json({"type": "user_msg", "content": "hi", "voice": True})
            assert ws.receive_json()["type"] == "thinking"
            first_assistant = ws.receive_json()
            first_msg_id = first_assistant["msg_id"]
            assert ws.receive_json() == {"type": "thinking", "state": "end"}

            # New message without voice — must still cancel and emit audio_end.
            ws.send_json({"type": "user_msg", "content": "silence"})
            assert ws.receive_json() == {"type": "audio_end", "msg_id": first_msg_id}
            assert ws.receive_json() == {"type": "thinking", "state": "start"}
            second = ws.receive_json()
            assert second["type"] == "assistant_msg"
            assert ws.receive_json() == {"type": "thinking", "state": "end"}
            # Crucially: no audio_chunk / audio_end for the new (voiceless) turn.

        assert slow_tts.cancelled is True
    finally:
        ws_router.reset_tts_service_provider()


def test_ws_chat_rejects_bad_content(fake_chat_service: _FakeChatService) -> None:
    client = TestClient(app)
    with client.websocket_connect("/ws/chat") as ws:
        ws.receive_json()
        ws.send_json({"type": "user_msg", "content": 42})
        err = ws.receive_json()
        assert err["type"] == "error"
        assert err["code"] == "bad_content"
    assert fake_chat_service.calls == []
