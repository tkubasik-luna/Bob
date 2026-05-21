"""WebSocket router for the chat endpoint.

Wires the ``/ws/chat`` endpoint to :class:`bob.chat_service.ChatService`. The
service is obtained via a module-level provider so tests can substitute a fake
without monkey-patching the FastAPI app or relying on dependency overrides.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import os
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx
import openai
import structlog
from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from bob import conversation as conversation_module
from bob import text_segmenter
from bob.chat_service import ChatService, get_default_chat_service
from bob.config import get_settings
from bob.model_downloader import ensure_kokoro_ready
from bob.tts_service import KokoroTtsService, get_default_tts_service

# Max base64-encoded PCM bytes per `audio_chunk` frame. ~256 KB keeps single
# WS frames well within typical limits even after JSON encoding overhead.
_AUDIO_CHUNK_MAX_B64 = 256 * 1024

router = APIRouter()
_logger = structlog.get_logger(__name__)

# Per-session shape:
#   {
#       "active_tts": list[tuple[str, asyncio.Task[None]]],
#           # (msg_id, task) pairs for in-flight TTS synthesis/streaming.
#   }
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

    Interruption: when a new ``user_msg`` arrives while a previous reply is
    still being synthesized/streamed as audio, any in-flight TTS task for that
    session is cancelled before the new turn starts. The cancelling code path
    emits a final ``audio_end`` for the interrupted ``msg_id`` so the client
    can purge its playback queue cleanly.
    """
    await websocket.accept()
    session_id = uuid.uuid4().hex
    _sessions[session_id] = {"active_tts": []}
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
        # Socket is going away — cancel any in-flight TTS without emitting frames.
        await _cancel_active_tts(session_id, emit_audio_end=False, websocket=None)
        _sessions.pop(session_id, None)
        conversation_module.get_default_store().clear(session_id)


async def _cancel_active_tts(
    session_id: str,
    *,
    emit_audio_end: bool,
    websocket: WebSocket | None,
) -> None:
    """Cancel every in-flight TTS task for ``session_id``.

    When ``emit_audio_end`` is true and a ``websocket`` is provided, a final
    ``audio_end`` frame is sent for each cancelled ``msg_id`` so the client can
    purge its playback queue.
    """

    session = _sessions.get(session_id)
    if not session:
        return
    active: list[tuple[str, asyncio.Task[None]]] = session.get("active_tts", [])
    if not active:
        return

    # Detach the list first so any done-callback that tries to mutate it during
    # cancellation doesn't race.
    session["active_tts"] = []

    for msg_id, task in active:
        if task.done():
            continue
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await task
        if emit_audio_end and websocket is not None:
            with contextlib.suppress(Exception):
                await websocket.send_json({"type": "audio_end", "msg_id": msg_id})


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

    # Interruption: cancel any in-flight TTS for previous assistant turns of
    # this session before handling the new user message. We do this regardless
    # of whether the new request opts into voice — a fresh message always means
    # "stop talking about the previous one".
    await _cancel_active_tts(session_id, emit_audio_end=True, websocket=websocket)

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
        # Run synthesis as a background task so the WS read loop can observe
        # the next user message (and cancel us) without waiting for audio to
        # finish streaming.
        task: asyncio.Task[None] = asyncio.create_task(
            _synthesize_and_stream(websocket, session_id, msg_id, parsed.speech)
        )
        session = _sessions.get(session_id)
        if session is not None:
            session.setdefault("active_tts", []).append((msg_id, task))

        def _remove_when_done(t: asyncio.Task[None], _msg_id: str = msg_id) -> None:
            sess = _sessions.get(session_id)
            if not sess:
                return
            active = sess.get("active_tts", [])
            for i, (mid, tt) in enumerate(active):
                if mid == _msg_id and tt is t:
                    active.pop(i)
                    break

        task.add_done_callback(_remove_when_done)


def _kokoro_model_files_present() -> bool:
    """Return True iff both the Kokoro model + voices artifacts exist on disk."""

    try:
        settings = get_settings()
    except Exception:
        # If settings can't be read, fall back to "present" so we don't lie
        # about a download that may not actually happen.
        return True
    model_dir = Path(settings.KOKORO_MODEL_DIR)
    model_path = model_dir / settings.KOKORO_MODEL_FILENAME
    voices_path = model_dir / settings.KOKORO_VOICES_FILENAME
    return os.path.exists(model_path) and os.path.exists(voices_path)


async def _synthesize_and_stream(
    websocket: WebSocket,
    session_id: str,
    msg_id: str,
    text: str,
) -> None:
    """Segment ``text`` into sentences and stream their PCM frames in order.

    Synthesis is pipelined: every sentence is dispatched to the TTS service
    immediately via ``asyncio.create_task`` so sentence ``N+1`` can be
    synthesized while sentence ``N`` is still being sent over the wire.

    Cancellation: this coroutine runs as a background task and may be cancelled
    by :func:`_cancel_active_tts` when a new user message arrives. The
    cancelling path emits the final ``audio_end`` for the cancelled msg_id; we
    bubble :class:`asyncio.CancelledError` here and do not emit it ourselves.

    Errors are surfaced via an ``audio_error`` event so the frontend can show a
    toast; the text response has already been sent so the user still sees the
    reply.
    """

    sentences = [s for s in text_segmenter.segment(text) if s.strip()]
    if not sentences:
        await websocket.send_json({"type": "audio_end", "msg_id": msg_id})
        return

    # Best-effort: detect a missing local model so the client can show a
    # "Préparation de la voix…" toast while the download runs.
    preparing = not _kokoro_model_files_present()
    if preparing:
        await websocket.send_json({"type": "tts_preparing", "msg_id": msg_id})
        try:
            await asyncio.to_thread(ensure_kokoro_ready)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            _logger.exception(
                "ws_chat.tts_download_failed", session_id=session_id, msg_id=msg_id
            )
            await websocket.send_json(
                {
                    "type": "audio_error",
                    "msg_id": msg_id,
                    "reason": f"téléchargement modèle: {exc}",
                }
            )
            await websocket.send_json({"type": "audio_end", "msg_id": msg_id})
            return

    tts = _tts_service_provider()
    tasks = [asyncio.create_task(tts.synthesize(sentence)) for sentence in sentences]

    step = _AUDIO_CHUNK_MAX_B64 - (_AUDIO_CHUNK_MAX_B64 % 4)
    seq = 0
    error_emitted = False
    try:
        if preparing:
            # Signal the frontend that the prep toast can be dismissed; the
            # first audio_chunk arrives immediately after.
            await websocket.send_json({"type": "tts_ready", "msg_id": msg_id})

        for idx, task in enumerate(tasks):
            try:
                result = await task
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                _logger.exception(
                    "ws_chat.tts_failed",
                    session_id=session_id,
                    msg_id=msg_id,
                    sentence_index=idx,
                )
                if not error_emitted:
                    await websocket.send_json(
                        {
                            "type": "audio_error",
                            "msg_id": msg_id,
                            "reason": str(exc) or exc.__class__.__name__,
                        }
                    )
                    error_emitted = True
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

        await websocket.send_json({"type": "audio_end", "msg_id": msg_id})
    except asyncio.CancelledError:
        # Cancellation: the cancelling path emits the final audio_end. Cancel
        # any still-pending synth tasks to free CPU.
        for task in tasks:
            if not task.done():
                task.cancel()
        raise
    finally:
        for task in tasks:
            if not task.done():
                task.cancel()
