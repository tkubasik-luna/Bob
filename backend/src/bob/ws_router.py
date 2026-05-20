"""WebSocket router for the chat endpoint (V0: echo only, no LLM)."""

from __future__ import annotations

import uuid
from typing import Any

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

router = APIRouter()

# In-memory per-session state. Keyed by session_id (uuid hex).
# Kept module-level so it can be inspected from tests if needed.
_sessions: dict[str, dict[str, Any]] = {}


@router.websocket("/ws/chat")
async def chat_ws(websocket: WebSocket) -> None:
    """Bidirectional chat WebSocket.

    Protocol V0:
      - On connect, server sends {"type": "session", "session_id": <uuid hex>}.
      - For each client message {"type": "user_msg", "content": str}, server replies with:
          {"type": "thinking", "state": "start"}
          {"type": "assistant_msg", "speech": <echo>, "ui": []}
          {"type": "thinking", "state": "end"}
      - Any other / malformed payload yields an error frame.
    """
    await websocket.accept()
    session_id = uuid.uuid4().hex
    _sessions[session_id] = {}

    try:
        await websocket.send_json({"type": "session", "session_id": session_id})

        while True:
            payload = await websocket.receive_json()
            await _handle_client_message(websocket, payload)
    except WebSocketDisconnect:
        # Normal disconnect; just fall through to cleanup.
        pass
    finally:
        _sessions.pop(session_id, None)


async def _handle_client_message(websocket: WebSocket, payload: Any) -> None:
    if not isinstance(payload, dict):
        await websocket.send_json(
            {"type": "error", "message": "payload must be a JSON object", "code": "bad_payload"}
        )
        return

    msg_type = payload.get("type")
    if msg_type != "user_msg":
        await websocket.send_json(
            {
                "type": "error",
                "message": f"unsupported message type: {msg_type!r}",
                "code": "bad_type",
            }
        )
        return

    content = payload.get("content")
    if not isinstance(content, str):
        await websocket.send_json(
            {
                "type": "error",
                "message": "user_msg.content must be a string",
                "code": "bad_content",
            }
        )
        return

    await websocket.send_json({"type": "thinking", "state": "start"})
    await websocket.send_json({"type": "assistant_msg", "speech": content, "ui": []})
    await websocket.send_json({"type": "thinking", "state": "end"})
