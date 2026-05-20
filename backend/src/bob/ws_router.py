"""WebSocket router for the chat endpoint.

Wires the ``/ws/chat`` endpoint to :class:`bob.chat_service.ChatService`. The
service is obtained via a module-level provider so tests can substitute a fake
without monkey-patching the FastAPI app or relying on dependency overrides.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable
from typing import Any

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from bob import conversation as conversation_module
from bob.chat_service import ChatService, get_default_chat_service

router = APIRouter()

# In-memory per-session state. Keyed by session_id (uuid hex).
# Kept module-level so it can be inspected from tests if needed.
_sessions: dict[str, dict[str, Any]] = {}

# DI seam: tests may rebind this to a callable returning a fake ChatService.
_chat_service_provider: Callable[[], ChatService] = get_default_chat_service


def set_chat_service_provider(provider: Callable[[], ChatService]) -> None:
    """Override the chat-service factory used by the WS handler.

    Intended for tests; production code should not call this.
    """

    global _chat_service_provider
    _chat_service_provider = provider


def reset_chat_service_provider() -> None:
    """Restore the default chat-service factory."""

    global _chat_service_provider
    _chat_service_provider = get_default_chat_service


@router.websocket("/ws/chat")
async def chat_ws(websocket: WebSocket) -> None:
    """Bidirectional chat WebSocket.

    Protocol:
      - On connect, server sends ``{"type": "session", "session_id": <uuid hex>}``.
      - For each client message ``{"type": "user_msg", "content": str}`` the
        server replies with:
          ``{"type": "thinking", "state": "start"}``
          ``{"type": "assistant_msg", "speech": str, "ui": [...]}``
          ``{"type": "thinking", "state": "end"}``
      - Any other / malformed payload yields an error frame.
      - On disconnect, the session's conversation history is cleared.
    """
    await websocket.accept()
    session_id = uuid.uuid4().hex
    _sessions[session_id] = {}
    chat_service = _chat_service_provider()

    try:
        await websocket.send_json({"type": "session", "session_id": session_id})

        while True:
            payload = await websocket.receive_json()
            await _handle_client_message(websocket, payload, session_id, chat_service)
    except WebSocketDisconnect:
        # Normal disconnect; just fall through to cleanup.
        pass
    finally:
        _sessions.pop(session_id, None)
        conversation_module.get_default_store().clear(session_id)


async def _handle_client_message(
    websocket: WebSocket,
    payload: Any,
    session_id: str,
    chat_service: ChatService,
) -> None:
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
    parsed = await chat_service.handle_user_message(session_id, content)
    await websocket.send_json(
        {
            "type": "assistant_msg",
            "speech": parsed.speech,
            "ui": [component.model_dump() for component in parsed.ui],
        }
    )
    await websocket.send_json({"type": "thinking", "state": "end"})
