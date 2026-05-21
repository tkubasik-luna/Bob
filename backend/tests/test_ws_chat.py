"""WS plumbing tests for /ws/chat — chat_service wired via DI seam."""

from __future__ import annotations

from collections.abc import Iterator
from typing import cast

import pytest
from fastapi.testclient import TestClient

from bob import conversation as conversation_module
from bob import ws_router
from bob.chat_service import ChatService
from bob.main import app
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


def test_ws_chat_rejects_bad_content(fake_chat_service: _FakeChatService) -> None:
    client = TestClient(app)
    with client.websocket_connect("/ws/chat") as ws:
        ws.receive_json()
        ws.send_json({"type": "user_msg", "content": 42})
        err = ws.receive_json()
        assert err["type"] == "error"
        assert err["code"] == "bad_content"
    assert fake_chat_service.calls == []
