"""WebSocket router for the chat endpoint.

Wires the ``/ws/chat`` endpoint to :class:`bob.chat_service.ChatService` and
streams Kokoro TTS audio back to the client as **binary** WS frames.

TTS wire protocol (per assistant turn that opts into voice)
-----------------------------------------------------------

1. ``tts_preparing`` *(JSON, optional)* — emitted only when the Kokoro HF
   snapshot isn't cached yet; client shows a "Préparation de la voix…" toast.
2. ``tts_ready`` *(JSON, optional)* — paired with the above when the model
   finishes loading. Toast can be dismissed.
3. ``audio_start`` *(JSON)* — sent exactly once, just before the first PCM
   frame. Carries ``msg_id`` + ``sample_rate``. Frontend uses it to bind
   subsequent binary frames to the right assistant bubble.
4. Zero or more **binary** frames — raw signed 16-bit little-endian PCM,
   mono, at the rate announced in ``audio_start``. Each frame is one
   KPipeline chunk (~250 ms of audio).
5. ``audio_end`` *(JSON)* — terminator, also sent on cancellation paths
   so the client can drain its playback queue.
6. ``audio_error`` *(JSON, optional)* — emitted on synthesis failure;
   immediately followed by ``audio_end``.

Single source of truth for the sample rate is :data:`bob.tts_service.KOKORO_SAMPLE_RATE`,
re-exported by :attr:`KokoroTtsService.sample_rate`.
"""

from __future__ import annotations

import asyncio
import contextlib
import time
import uuid
from collections.abc import Callable
from typing import Any

import httpx
import openai
import structlog
from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from bob import jarvis_store as jarvis_store_module
from bob import task_store as task_store_module
from bob import text_segmenter, ws_events
from bob.orchestrator import Orchestrator, get_default_orchestrator
from bob.spoken_text_cleaner import clean_for_speech
from bob.tts_service import KokoroTtsService, get_default_tts_service

router = APIRouter()
_logger = structlog.get_logger(__name__)

# Per-session shape:
#   {"active_tts": list[tuple[str, asyncio.Task[None]]]}
_sessions: dict[str, dict[str, Any]] = {}

_orchestrator_provider: Callable[[], Orchestrator] = get_default_orchestrator
_tts_service_provider: Callable[[], KokoroTtsService] = get_default_tts_service


def set_orchestrator_provider(provider: Callable[[], Orchestrator]) -> None:
    global _orchestrator_provider
    _orchestrator_provider = provider


def reset_orchestrator_provider() -> None:
    global _orchestrator_provider
    _orchestrator_provider = get_default_orchestrator


def set_tts_service_provider(provider: Callable[[], KokoroTtsService]) -> None:
    global _tts_service_provider
    _tts_service_provider = provider


def reset_tts_service_provider() -> None:
    global _tts_service_provider
    _tts_service_provider = get_default_tts_service


@router.websocket("/ws/chat")
async def chat_ws(websocket: WebSocket) -> None:
    """Bidirectional chat WebSocket with streaming TTS.

    Interruption: a new ``user_msg`` cancels any in-flight TTS for previous
    turns of this session before the new turn starts. The cancelling path
    emits a final ``audio_end`` for each interrupted ``msg_id`` so the client
    can purge its playback queue cleanly.
    """

    await websocket.accept()
    session_id = uuid.uuid4().hex
    _sessions[session_id] = {"active_tts": []}
    orchestrator = _orchestrator_provider()

    async def _session_emit(event: dict[str, Any]) -> None:
        """Forward a task event from the orchestrator / sub-agent to this WS."""

        try:
            await websocket.send_json(event)
        except Exception:
            _logger.exception(
                "ws_chat.task_event_emit_failed",
                session_id=session_id,
                event_type=event.get("type"),
            )

    ws_events.set_emitter(_session_emit)

    try:
        await websocket.send_json({"type": "session", "session_id": session_id})
        await _replay_history(websocket)
        await _replay_active_tasks(websocket)

        while True:
            payload = await websocket.receive_json()
            await _handle_client_message(websocket, payload, session_id, orchestrator)
    except WebSocketDisconnect:
        pass
    finally:
        ws_events.set_emitter(None)
        await _cancel_active_tts(session_id, emit_audio_end=False, websocket=None)
        _sessions.pop(session_id, None)
        # NOTE: Jarvis history is persistent across disconnects (PRD 0003).


async def _replay_history(websocket: WebSocket) -> None:
    """Push persisted Jarvis history to a freshly-connected client.

    Each persisted message is sent in the same shape the live chat path uses
    (``user_msg`` / ``assistant_msg``) with an added ``replayed: true`` flag,
    so existing frontend handlers don't need new event types. Silently skips
    when the store hasn't been primed (narrow test setups that bypass the
    lifespan).
    """

    try:
        store = jarvis_store_module.get_default_store()
    except RuntimeError:
        return

    for msg in store.history():
        role = msg.get("role")
        content = msg.get("content", "")
        if role == "user":
            await websocket.send_json({"type": "user_msg", "content": content, "replayed": True})
        elif role == "assistant":
            await websocket.send_json(
                {
                    "type": "assistant_msg",
                    "msg_id": uuid.uuid4().hex,
                    "speech": content,
                    "ui": [],
                    "replayed": True,
                    "proactive": False,
                }
            )


async def _replay_active_tasks(websocket: WebSocket) -> None:
    """Replay the task store on connect so the sidebar reconstructs at reload.

    Emits one ``task_created`` per task (current state, not necessarily
    ``pending``) plus a ``task_result`` when the task already has a result.
    The ``replayed: true`` flag lets the frontend distinguish if it wants —
    the current frontend simply upserts unconditionally.

    Slice #0024 will introduce dismiss filtering; for now every known task is
    surfaced. Silently skips when the task store hasn't been primed (narrow
    test setups that bypass the lifespan).
    """

    try:
        store = task_store_module.get_default_store()
    except RuntimeError:
        return

    for task in store.list_tasks():
        await websocket.send_json(
            {
                "type": "task_created",
                "task_id": task.id,
                "title": task.title,
                "goal": task.goal,
                "state": task.state,
                "created_at": task.created_at,
                "replayed": True,
            }
        )
        if task.result is not None:
            await websocket.send_json(
                {
                    "type": "task_result",
                    "task_id": task.id,
                    "result": task.result,
                    "replayed": True,
                }
            )


async def _cancel_active_tts(
    session_id: str,
    *,
    emit_audio_end: bool,
    websocket: WebSocket | None,
) -> None:
    """Cancel every in-flight TTS task for ``session_id``.

    When ``emit_audio_end`` is true and a ``websocket`` is provided, a final
    ``audio_end`` frame is sent for each cancelled ``msg_id`` so the client
    can purge its playback queue.
    """

    session = _sessions.get(session_id)
    if not session:
        return
    active: list[tuple[str, asyncio.Task[None]]] = session.get("active_tts", [])
    if not active:
        return

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
    orchestrator: Orchestrator,
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

    # New user msg = "stop talking about the previous one", whether or not
    # this new turn wants voice itself.
    await _cancel_active_tts(session_id, emit_audio_end=True, websocket=websocket)

    await websocket.send_json({"type": "thinking", "state": "start"})
    try:
        response = await orchestrator.process_user_message(session_id, content)
    except (httpx.ConnectError, openai.APIConnectionError, ConnectionError):
        _logger.error("ws_chat.llm_unreachable", session_id=session_id)
        await websocket.send_json(
            {"type": "error", "code": "LLM_UNREACHABLE", "message": "LLM provider injoignable"}
        )
        await websocket.send_json({"type": "thinking", "state": "end"})
        return
    except (TimeoutError, openai.APITimeoutError):
        _logger.error("ws_chat.llm_timeout", session_id=session_id)
        await websocket.send_json(
            {"type": "error", "code": "LLM_TIMEOUT", "message": "Timeout LLM"}
        )
        await websocket.send_json({"type": "thinking", "state": "end"})
        return
    except Exception:
        _logger.exception("ws_chat.internal_error", session_id=session_id)
        await websocket.send_json(
            {"type": "error", "code": "INTERNAL", "message": "Erreur interne"}
        )
        await websocket.send_json({"type": "thinking", "state": "end"})
        return

    msg_id = uuid.uuid4().hex
    await websocket.send_json(
        {
            "type": "assistant_msg",
            "msg_id": msg_id,
            "speech": response.speech,
            "ui": [component.model_dump() for component in response.ui],
            "proactive": False,
        }
    )
    await websocket.send_json({"type": "thinking", "state": "end"})

    if voice_requested and response.speech.strip():
        task: asyncio.Task[None] = asyncio.create_task(
            _synthesize_and_stream(websocket, session_id, msg_id, response.speech)
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


def _kokoro_model_files_present(tts: KokoroTtsService) -> bool:
    try:
        return tts.is_model_cached()
    except Exception:
        return True


async def _synthesize_and_stream(
    websocket: WebSocket,
    session_id: str,
    msg_id: str,
    text: str,
) -> None:
    """Segment ``text``, stream PCM chunks straight from KPipeline to the WS.

    Pipeline (sequential, by design — KPipeline is not thread-safe):

        clean_for_speech(text) → text_segmenter.segment → for each sentence:
            tts.synthesize_stream(sentence) → for each PCM chunk:
                websocket.send_bytes(pcm)

    First chunk of the whole turn triggers a single ``audio_start`` JSON
    header so the client knows the sample rate. ``first_audio_ms`` is
    logged once per ``msg_id`` for latency telemetry.

    Cancellation: this coroutine runs as a background task and may be
    cancelled by :func:`_cancel_active_tts` when a new user message
    arrives. The cancelling path emits the final ``audio_end``; we bubble
    :class:`asyncio.CancelledError` here and do not emit it ourselves.
    """

    cleaned = clean_for_speech(text)
    sentences = [s for s in text_segmenter.segment(cleaned) if s.strip()]
    if not sentences:
        await websocket.send_json({"type": "audio_end", "msg_id": msg_id})
        return

    tts = _tts_service_provider()

    preparing = not _kokoro_model_files_present(tts)
    if preparing:
        await websocket.send_json({"type": "tts_preparing", "msg_id": msg_id})
        try:
            await asyncio.to_thread(tts.preload)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            _logger.exception("ws_chat.tts_download_failed", session_id=session_id, msg_id=msg_id)
            await websocket.send_json(
                {"type": "audio_error", "msg_id": msg_id, "reason": f"téléchargement modèle: {exc}"}
            )
            await websocket.send_json({"type": "audio_end", "msg_id": msg_id})
            return
        await websocket.send_json({"type": "tts_ready", "msg_id": msg_id})

    started_at = time.perf_counter()
    started_audio = False
    error_emitted = False

    try:
        for idx, sentence in enumerate(sentences):
            try:
                async for chunk in tts.synthesize_stream(sentence):
                    if not started_audio:
                        await websocket.send_json(
                            {
                                "type": "audio_start",
                                "msg_id": msg_id,
                                "sample_rate": chunk.sample_rate,
                            }
                        )
                        first_audio_ms = (time.perf_counter() - started_at) * 1000.0
                        _logger.info(
                            "ws_chat.tts_first_audio",
                            session_id=session_id,
                            msg_id=msg_id,
                            first_audio_ms=round(first_audio_ms, 1),
                        )
                        started_audio = True
                    await websocket.send_bytes(chunk.pcm16)
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

        await websocket.send_json({"type": "audio_end", "msg_id": msg_id})
    except asyncio.CancelledError:
        # The cancelling path emits the final audio_end. Bubble out.
        raise
