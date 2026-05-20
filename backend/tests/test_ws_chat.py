"""WS echo plumbing tests for /ws/chat."""

from fastapi.testclient import TestClient

from bob.main import app
from bob.ws_router import _sessions


def test_ws_chat_echo_full_round_trip() -> None:
    client = TestClient(app)
    with client.websocket_connect("/ws/chat") as ws:
        session_frame = ws.receive_json()
        assert session_frame["type"] == "session"
        session_id = session_frame["session_id"]
        assert isinstance(session_id, str) and len(session_id) == 32
        assert session_id in _sessions

        ws.send_json({"type": "user_msg", "content": "hello bob"})

        thinking_start = ws.receive_json()
        assert thinking_start == {"type": "thinking", "state": "start"}

        assistant = ws.receive_json()
        assert assistant == {"type": "assistant_msg", "speech": "hello bob", "ui": []}

        thinking_end = ws.receive_json()
        assert thinking_end == {"type": "thinking", "state": "end"}

    # Session cleaned up on disconnect.
    assert session_id not in _sessions


def test_ws_chat_rejects_unknown_type() -> None:
    client = TestClient(app)
    with client.websocket_connect("/ws/chat") as ws:
        ws.receive_json()  # session frame
        ws.send_json({"type": "nope"})
        err = ws.receive_json()
        assert err["type"] == "error"
        assert err["code"] == "bad_type"


def test_ws_chat_rejects_bad_content() -> None:
    client = TestClient(app)
    with client.websocket_connect("/ws/chat") as ws:
        ws.receive_json()
        ws.send_json({"type": "user_msg", "content": 42})
        err = ws.receive_json()
        assert err["type"] == "error"
        assert err["code"] == "bad_content"
