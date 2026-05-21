"""WebSocket router for the chat endpoint.

Wires the ``/ws/chat`` endpoint to :class:`bob.chat_service.ChatService`. The
service is obtained via a module-level provider so tests can substitute a fake
without monkey-patching the FastAPI app or relying on dependency overrides.
"""

from __future__ import annotations

import asyncio
import base64
import uuid
from collections.abc import Callable
from typing import Any

import httpx
import openai
import structlog
from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from bob import conversation as conversation_module
from bob import text_segmenter
from bob.chat_service import ChatService, get_default_chat_service
from bob.tts_service import KokoroTtsService, get_default_tts_service

# Max base64-encoded PCM bytes per `audio_chunk` frame. ~256 KB keeps single
# WS frames well within typical limits even after JSON encoding overhead.
_AUDIO_CHUNK_MAX_B64 = 256 * 1024

router = APIRouter()
_logger = structlog.get_logger(__name__)

# In-memory per-session state. Keyed by session_id (uuid hex).
# Kept module-level so it can be inspected from tests if needed.
_sessions: dict[str, dict[str, Any]] = {}

# DI seam: tests may rebind this to a callable returning a fake ChatService.
_chat_service_provider: Callable[[], ChatService] = get_default_chat_service

# DI seam: tests may rebind this to a callable returning a fake TTS service.
_tts_service_provider: Callable[[], KokoroTtsService] = get_default_tts_service


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


def set_tts_service_provider(provider: Callable[[], KokoroTtsService]) -> None:
    """Override the TTS-service factory used by the WS handler (tests only)."""

    global _tts_service_provider
    _tts_service_provider = provider


def reset_tts_service_provider() -> None:
    """Restore the default TTS-service factory."""

    global _tts_service_provider
    _tts_service_provider = get_default_tts_service


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
      - LLM-level failures yield ``{"type": "error", "code": ..., "message": ...}``
        followed by a ``thinking end`` frame; the connection stays open.
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

    voice_requested = bool(payload.get("voice"))

    await websocket.send_json({"type": "thinking", "state": "start"})
    try:
        parsed = await chat_service.handle_user_message(session_id, content)
    except (httpx.ConnectError, openai.APIConnectionError, ConnectionError):
        _logger.error("ws_chat.llm_unreachable", session_id=session_id)
        await websocket.send_json(
            {
                "type": "error",
                "code": "LLM_UNREACHABLE",
                "message": "LLM provider injoignable",
            }
        )
        await websocket.send_json({"type": "thinking", "state": "end"})
        return
    except (TimeoutError, openai.APITimeoutError):
        _logger.error("ws_chat.llm_timeout", session_id=session_id)
        await websocket.send_json(
            {
                "type": "error",
                "code": "LLM_TIMEOUT",
                "message": "Timeout LLM",
            }
        )
        await websocket.send_json({"type": "thinking", "state": "end"})
        return
    except Exception:
        _logger.exception("ws_chat.internal_error", session_id=session_id)
        await websocket.send_json(
            {
                "type": "error",
                "code": "INTERNAL",
                "message": "Erreur interne",
            }
        )
        await websocket.send_json({"type": "thinking", "state": "end"})
        return

    msg_id = uuid.uuid4().hex
    await websocket.send_json(
        {
            "type": "assistant_msg",
            "msg_id": msg_id,
            "speech": parsed.speech,
            "ui": [component.model_dump() for component in parsed.ui],
        }
    )
    await websocket.send_json({"type": "thinking", "state": "end"})

    if voice_requested and parsed.speech.strip():
        await _synthesize_and_stream(websocket, session_id, msg_id, parsed.speech)


async def _synthesize_and_stream(
    websocket: WebSocket,
    session_id: str,
    msg_id: str,
    text: str,
) -> None:
    """Segment ``text`` into sentences and stream their PCM frames in order.

    Synthesis is pipelined: every sentence is dispatched to the TTS service
    immediately via ``asyncio.create_task`` so sentence ``N+1`` can be
    synthesized while sentence ``N`` is still being sent over the wire (and
    played by the frontend). We await tasks in order and emit ``audio_chunk``
    frames sequentially so the client can chain playback gaplessly.

    Errors during synthesis of a given sentence are logged and skipped; the
    overall best-effort contract is preserved and an ``audio_end`` is always
    emitted at the very end.
    """

    sentences = [s for s in text_segmenter.segment(text) if s.strip()]
    if not sentences:
        await websocket.send_json({"type": "audio_end", "msg_id": msg_id})
        return

    tts = _tts_service_provider()
    # Kick off all syntheses concurrently; await them in order below.
    tasks = [asyncio.create_task(tts.synthesize(sentence)) for sentence in sentences]

    step = _AUDIO_CHUNK_MAX_B64 - (_AUDIO_CHUNK_MAX_B64 % 4)
    seq = 0
    try:
        for idx, task in enumerate(tasks):
            try:
                result = await task
            except Exception:
                _logger.exception(
                    "ws_chat.tts_failed",
                    session_id=session_id,
                    msg_id=msg_id,
                    sentence_index=idx,
                )
                continue

            pcm_b64 = base64.b64encode(result.pcm16).decode("ascii")
            sample_rate = result.sample_rate
            for start in range(0, len(pcm_b64), step):
                chunk = pcm_b64[start : start + step]
                await websocket.send_json(
                    {
                        "type": "audio_chunk",
                        "msg_id": msg_id,
                        "seq": seq,
                        "pcm_b64": chunk,
                        "sample_rate": sample_rate,
                    }
                )
                seq += 1
    finally:
        # Make sure no pending task is left dangling if we exit early.
        for task in tasks:
            if not task.done():
                task.cancel()
        await websocket.send_json({"type": "audio_end", "msg_id": msg_id})
