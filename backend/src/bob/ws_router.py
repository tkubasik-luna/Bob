"""WebSocket router for the chat endpoint.

Wires the ``/ws/chat`` endpoint to :class:`bob.chat_service.ChatService` and
streams Kokoro TTS audio back to the client as **binary** WS frames.

Per-turn streamed text protocol (PRD 0006 / issue 0049)
-------------------------------------------------------

Before the ``assistant_msg`` final frame, the orchestrator emits zero
or more ``speech_delta`` JSON frames and at most one ``ui_payload``
frame via :func:`bob.event_bus_v2.emit_event`. All three frame types
share the ``msg_id`` field so the frontend can correlate them with the
eventual bubble:

- ``speech_delta`` — emitted as ``say.speech`` accumulates. Frame shape:
  ``{type: "speech_delta", msg_id: "<hex>", delta: "<new suffix>"}``.
  The frontend pipes ``delta`` directly into its TTS engine for
  progressive synthesis and into the sphere text component for
  progressive on-screen text.
- ``ui_payload`` — emitted exactly once on argument-object close, only
  when ``say.ui`` is a non-null object. Frame shape:
  ``{type: "ui_payload", msg_id: "<hex>", ui: {...}}``. The frontend
  opens the markdown overlay with this payload once the corresponding
  spoken phrase has finished streaming.
- ``assistant_msg`` — still emitted at the end of every turn (PRD
  compatibility shim). Carries the full final speech + ui + the same
  ``msg_id`` the streamed deltas were tagged with. Used by:

  - history replay on reconnect (the streamed deltas do NOT survive a
    process restart — only the persisted assistant turn does, replayed
    as a single ``assistant_msg``);
  - the proactive path (sub-task done synthesis, paraphrased
    ``ask_user``) which is a single-shot non-streaming chat call —
    proactive frames carry ``proactive=true`` and bypass the streaming
    pipeline entirely;
  - the degrade path (validation budget exhausted → hardcoded "Désolé,
    peux-tu reformuler ?") which mints a fresh ``msg_id`` because no
    streaming happened.

  Kept for those three reasons rather than removed; cleaning up after
  the stabilisation window would require a frontend redesign of the
  reconnect-replay path and the proactive bubble surface, which is
  out of scope for issue 0049.

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
import traceback
import uuid
from collections.abc import Callable
from typing import Any

import httpx
import openai
import structlog
from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from bob import jarvis_store as jarvis_store_module
from bob import task_scheduler as task_scheduler_module
from bob import task_store as task_store_module
from bob import text_segmenter, ws_events
from bob.debug_log import emit_debug
from bob.event_bus_v2 import get_snapshot_for_task, subscribe_for_task
from bob.orchestrator import Orchestrator, get_default_orchestrator
from bob.spoken_text_cleaner import clean_for_speech
from bob.task_store import TaskStoreError
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
    # `voice_mode` is sticky session state (PRD 0004): the client sends a
    # `voice_mode` frame on every toggle (and on connect/reconnect for
    # re-sync). When true, proactive pushes are also voiced via TTS, not
    # only direct replies to `user_msg` carrying their own `voice: true`.
    _sessions[session_id] = {"active_tts": [], "voice_mode": False}
    orchestrator = _orchestrator_provider()

    # Slice 0039: connect-time debug event. Lives outside any user turn so
    # ``turn_id`` is intentionally None here.
    emit_debug(
        category="system",
        severity="info",
        source="bob.ws_router.chat_ws",
        summary=f"Client WS connecté (session={session_id})",
        payload={"session_id": session_id},
    )

    async def _session_emit(event: dict[str, Any]) -> None:
        """Forward a task event from the orchestrator / sub-agent to this WS.

        PRD 0004: when the event is a proactive ``assistant_msg`` and the
        session has voice mode enabled, also kick off TTS synthesis for the
        spoken line so the user hears the sub-task done synthesis (and other
        proactive pushes) without having to re-prompt.
        """

        try:
            await websocket.send_json(event)
        except Exception:
            _logger.exception(
                "ws_chat.task_event_emit_failed",
                session_id=session_id,
                event_type=event.get("type"),
            )
            return

        if event.get("type") != "assistant_msg":
            return
        if not event.get("proactive"):
            return
        session = _sessions.get(session_id)
        if session is None or not session.get("voice_mode"):
            return
        speech = event.get("speech")
        msg_id = event.get("msg_id")
        if not isinstance(speech, str) or not speech.strip():
            return
        if not isinstance(msg_id, str) or not msg_id:
            return
        task: asyncio.Task[None] = asyncio.create_task(
            _synthesize_and_stream(websocket, session_id, msg_id, speech)
        )
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

    # Register THIS session's emitter (fan-out). Pre-fix this used
    # ``set_emitter`` which replaced the single global slot, so opening a
    # second window stole the channel from the first — task events, streamed
    # ui_payload/speech_delta and proactive pushes only reached the
    # last-connected window. ``add_emitter`` + ``remove_emitter`` (finally)
    # make every open window receive them.
    ws_events.add_emitter(_session_emit)

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
        ws_events.remove_emitter(_session_emit)
        await _cancel_active_tts(session_id, emit_audio_end=False, websocket=None)
        _sessions.pop(session_id, None)
        # Slice 0039: disconnect-time debug event. Lives outside any user turn.
        emit_debug(
            category="system",
            severity="info",
            source="bob.ws_router.chat_ws",
            summary="Client WS déconnecté",
            payload={"session_id": session_id},
        )
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

    Slice #0024 introduced ``dismissed`` filtering — :meth:`TaskStore.list_tasks`
    defaults to ``include_dismissed=False`` so user-hidden tasks stay out of
    the replay (their rows are still in SQLite for the drawer). Silently
    skips when the task store hasn't been primed (narrow test setups that
    bypass the lifespan).
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


async def _handle_cancel_task(websocket: WebSocket, payload: dict[str, Any]) -> None:
    """Client → ``cancel_task`` cancels a non-terminal task (slice #0023).

    Sidebar cancel button on a ``pending`` / ``running`` / ``waiting_input``
    card posts this event. The scheduler:

    - is permissive: cancelling a ``done`` / ``failed`` task is a silent
      no-op (UI may race a natural completion);
    - owns the asyncio cancellation of any in-flight runner;
    - transitions the row to ``failed`` with ``result="user_cancelled"``
      and emits ``task_updated`` + ``task_result``.

    The frontend repopulates the card from those events — no echo from
    this WS handler is needed beyond error reporting.
    """

    task_id = payload.get("task_id")
    if not isinstance(task_id, str) or not task_id:
        await websocket.send_json(
            {
                "type": "error",
                "code": "bad_cancel",
                "message": "cancel_task.task_id must be a non-empty string",
            }
        )
        return

    try:
        scheduler = task_scheduler_module.get_default_scheduler()
    except RuntimeError:
        # Scheduler not primed — narrow test setups bypass the lifespan.
        await websocket.send_json(
            {
                "type": "error",
                "code": "scheduler_unavailable",
                "message": "task scheduler unavailable",
            }
        )
        return

    await scheduler.cancel(task_id, reason="user_cancelled")


async def _handle_dismiss_task(websocket: WebSocket, payload: dict[str, Any]) -> None:
    """Client → ``dismiss_task`` hides a done/failed task from the sidebar.

    The data layer is permissive (any state can be dismissed). The UI only
    surfaces the button on terminal states; the WS layer trusts that and
    just flips the flag. No response event is needed — the frontend already
    drops the card from its in-memory map on click.
    """

    task_id = payload.get("task_id")
    if not isinstance(task_id, str) or not task_id:
        await websocket.send_json(
            {
                "type": "error",
                "code": "bad_dismiss",
                "message": "dismiss_task.task_id must be a non-empty string",
            }
        )
        return

    try:
        store = task_store_module.get_default_store()
    except RuntimeError:
        # Store not primed — narrow test setups. Don't leak a 500.
        await websocket.send_json(
            {"type": "error", "code": "store_unavailable", "message": "task store unavailable"}
        )
        return

    from bob.task_store import TaskStoreError

    try:
        store.dismiss_task(task_id)
    except TaskStoreError:
        await websocket.send_json(
            {"type": "error", "code": "unknown_task", "message": "task not found"}
        )


async def _handle_client_typing(
    websocket: WebSocket, payload: dict[str, Any], orchestrator: Orchestrator
) -> None:
    """Client → ``client_typing`` heartbeat (slice #0025).

    Frontend sends ``{"type": "client_typing", "typing": true/false}`` on
    keystroke (debounced 500 ms) and on submit. We update the orchestrator's
    typing flag so any proactive push currently queued (e.g. a paraphrased
    ``ask_user`` question or a ``done`` synthesis) is held back while the
    user is composing. The flag also auto-resets after 2 s server-side, so a
    missed trailing ``false`` cannot starve the queue forever.

    Returns a ``bad_typing`` error code when ``typing`` is not a bool — the
    contract is strict to avoid silently swallowing typos.
    """

    typing = payload.get("typing")
    if not isinstance(typing, bool):
        await websocket.send_json(
            {
                "type": "error",
                "code": "bad_typing",
                "message": "client_typing.typing must be a boolean",
            }
        )
        return
    orchestrator.set_user_typing(typing)


async def _handle_voice_mode(
    websocket: WebSocket, payload: dict[str, Any], session_id: str
) -> None:
    """Client → ``voice_mode`` toggle (PRD 0004).

    Frontend sends ``{"type": "voice_mode", "enabled": true/false}`` on every
    mute toggle AND on connect/reconnect (re-sync). We persist the flag in
    the session dict so ``_session_emit`` can synthesise TTS for proactive
    pushes (sub-task done synthesis, paraphrased ``ask_user``) when voice is
    on — not only direct replies to a ``user_msg`` carrying ``voice: true``.

    Returns a ``bad_voice_mode`` error code when ``enabled`` is not a bool.
    """

    enabled = payload.get("enabled")
    if not isinstance(enabled, bool):
        await websocket.send_json(
            {
                "type": "error",
                "code": "bad_voice_mode",
                "message": "voice_mode.enabled must be a boolean",
            }
        )
        return
    session = _sessions.get(session_id)
    if session is not None:
        session["voice_mode"] = enabled


async def _handle_request_task_messages(websocket: WebSocket, payload: dict[str, Any]) -> None:
    """Client → return the full ``task_messages`` log for a task.

    Used by the drawer to render the transcript on open. Live updates after
    open arrive via the ``task_message`` push (emitted from sub-agent /
    orchestrator code paths that call :meth:`TaskStore.append_message`).
    """

    task_id = payload.get("task_id")
    if not isinstance(task_id, str) or not task_id:
        await websocket.send_json(
            {
                "type": "error",
                "code": "bad_request_messages",
                "message": "request_task_messages.task_id must be a non-empty string",
            }
        )
        return

    try:
        store = task_store_module.get_default_store()
    except RuntimeError:
        await websocket.send_json(
            {"type": "error", "code": "store_unavailable", "message": "task store unavailable"}
        )
        return

    from bob.task_store import TaskStoreError

    try:
        # ``get_task`` validates the id and surfaces a clean error to the
        # client. The message log itself does not enforce existence so we
        # check the task row up front.
        store.get_task(task_id)
    except TaskStoreError:
        await websocket.send_json(
            {"type": "error", "code": "unknown_task", "message": "task not found"}
        )
        return

    messages = store.get_task_messages(task_id)
    await websocket.send_json(
        {
            "type": "task_messages_snapshot",
            "task_id": task_id,
            "messages": [
                {
                    "id": msg.id,
                    "role": msg.role,
                    "content": msg.content,
                    "action": msg.action,
                    "created_at": msg.created_at,
                }
                for msg in messages
            ],
        }
    )


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

    if msg_type == "cancel_task":
        await _handle_cancel_task(websocket, payload)
        return

    if msg_type == "dismiss_task":
        await _handle_dismiss_task(websocket, payload)
        return

    if msg_type == "request_task_messages":
        await _handle_request_task_messages(websocket, payload)
        return

    if msg_type == "client_typing":
        await _handle_client_typing(websocket, payload, orchestrator)
        return

    if msg_type == "voice_mode":
        await _handle_voice_mode(websocket, payload, session_id)
        return

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

    # TTS invariant: an output speech must ALWAYS reach TTS once the user is
    # interacting by voice — including proactive pushes (sub-task done / failed
    # synthesis), which voice on the sticky session ``voice_mode`` flag. Before
    # this, per-message ``voice`` and session ``voice_mode`` were disjoint, so a
    # client that only set per-message ``voice`` got silent failure / done
    # announcements. A voice-carrying turn now marks the session voice-on.
    if voice_requested:
        voice_session = _sessions.get(session_id)
        if voice_session is not None:
            voice_session["voice_mode"] = True

    # New user msg = "stop talking about the previous one", whether or not
    # this new turn wants voice itself.
    await _cancel_active_tts(session_id, emit_audio_end=True, websocket=websocket)

    await websocket.send_json({"type": "thinking", "state": "start"})
    try:
        response = await orchestrator.process_user_message(session_id, content)
    except (httpx.ConnectError, openai.APIConnectionError, ConnectionError) as exc:
        _logger.error("ws_chat.llm_unreachable", session_id=session_id)
        # Issue: structlog uses ``PrintLoggerFactory`` which bypasses stdlib
        # logging, so the ``_DebugBridgeHandler`` never forwards these records
        # to the debug feed. Emit explicitly so turn failures are visible in
        # ``orchestration.jsonl`` (this is what hid the v2-spawn crash).
        emit_debug(
            category="system",
            severity="error",
            source="bob.ws_router.chat_ws",
            summary="LLM injoignable pendant le turn",
            payload={"session_id": session_id, "error": repr(exc), "code": "LLM_UNREACHABLE"},
        )
        await websocket.send_json(
            {"type": "error", "code": "LLM_UNREACHABLE", "message": "LLM provider injoignable"}
        )
        await websocket.send_json({"type": "thinking", "state": "end"})
        return
    except (TimeoutError, openai.APITimeoutError) as exc:
        _logger.error("ws_chat.llm_timeout", session_id=session_id)
        emit_debug(
            category="system",
            severity="error",
            source="bob.ws_router.chat_ws",
            summary="Timeout LLM pendant le turn",
            payload={"session_id": session_id, "error": repr(exc), "code": "LLM_TIMEOUT"},
        )
        await websocket.send_json(
            {"type": "error", "code": "LLM_TIMEOUT", "message": "Timeout LLM"}
        )
        await websocket.send_json({"type": "thinking", "state": "end"})
        return
    except Exception as exc:
        _logger.exception("ws_chat.internal_error", session_id=session_id)
        # The catch-all is the chokepoint that silently swallowed the v2
        # ``spawn_task`` AssertionError. Surface the full traceback into the
        # observable debug log so the next failure is diagnosable offline.
        emit_debug(
            category="system",
            severity="error",
            source="bob.ws_router.chat_ws",
            summary=f"Erreur interne pendant le turn: {type(exc).__name__}: {exc}",
            payload={
                "session_id": session_id,
                "error": repr(exc),
                "traceback": traceback.format_exc(),
                "code": "INTERNAL",
            },
        )
        await websocket.send_json(
            {"type": "error", "code": "INTERNAL", "message": "Erreur interne"}
        )
        await websocket.send_json({"type": "thinking", "state": "end"})
        return

    # PRD 0006 / issue 0049 — the orchestrator's streaming pipeline mints
    # ``msg_id`` and emits ``speech_delta`` frames under it during the
    # turn. The final ``assistant_msg`` reuses the same id so the frontend
    # correlates the streamed deltas with the bubble. Degrade paths leave
    # ``msg_id`` empty — fall back to a generated id so the frame still
    # carries one (matches the pre-0049 contract).
    msg_id = response.msg_id or uuid.uuid4().hex
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
        emit_debug(
            category="voice",
            severity="info",
            source="bob.ws_router._synthesize_and_stream",
            summary="Kokoro download...",
            payload={"session_id": session_id, "msg_id": msg_id},
        )
        try:
            await asyncio.to_thread(tts.preload)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            _logger.exception("ws_chat.tts_download_failed", session_id=session_id, msg_id=msg_id)
            emit_debug(
                category="voice",
                severity="warn",
                source="bob.ws_router._synthesize_and_stream",
                summary=f"Audio erreur: téléchargement modèle: {exc}",
                payload={
                    "session_id": session_id,
                    "msg_id": msg_id,
                    "exception": str(exc),
                    "exception_type": exc.__class__.__name__,
                },
            )
            await websocket.send_json(
                {"type": "audio_error", "msg_id": msg_id, "reason": f"téléchargement modèle: {exc}"}
            )
            await websocket.send_json({"type": "audio_end", "msg_id": msg_id})
            return
        await websocket.send_json({"type": "tts_ready", "msg_id": msg_id})
        emit_debug(
            category="voice",
            severity="debug",
            source="bob.ws_router._synthesize_and_stream",
            summary="Kokoro prêt",
            payload={"session_id": session_id, "msg_id": msg_id},
        )

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
                        emit_debug(
                            category="voice",
                            severity="debug",
                            source="bob.ws_router._synthesize_and_stream",
                            summary=f"Audio stream démarré (msg={msg_id})",
                            payload={
                                "session_id": session_id,
                                "msg_id": msg_id,
                                "sample_rate": chunk.sample_rate,
                                "first_audio_ms": round(first_audio_ms, 1),
                            },
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
                    emit_debug(
                        category="voice",
                        severity="warn",
                        source="bob.ws_router._synthesize_and_stream",
                        summary=f"Audio erreur: {exc or exc.__class__.__name__}",
                        payload={
                            "session_id": session_id,
                            "msg_id": msg_id,
                            "sentence_index": idx,
                            "exception": str(exc),
                            "exception_type": exc.__class__.__name__,
                        },
                    )
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
        emit_debug(
            category="voice",
            severity="debug",
            source="bob.ws_router._synthesize_and_stream",
            summary="Audio stream terminé",
            payload={"session_id": session_id, "msg_id": msg_id},
        )
    except asyncio.CancelledError:
        # The cancelling path emits the final audio_end. Bubble out.
        raise


# --- Per-task overlay WS (issue 0052) ---------------------------------------
#
# Single-session "snapshot then tail" subscription scoped to a single
# ``task_id``. The frontend overlay opens this when the user clicks a
# running task; the first frame carries every currently-buffered event
# that matched the id (``replayed=true``), subsequent frames carry live
# events as the sub-agent runs. No HTTP-then-WS race — both phases are
# served from the same socket.
#
# The producer side is :mod:`bob.event_bus_v2`: every emit lands in the
# debug ring buffer with a ``task_id`` field (populated by the
# ``current_task_id`` ContextVar inside :class:`SubAgentRunner.run`).
# :func:`subscribe_for_task` walks that buffer for the snapshot and tails
# the live producer with a per-event filter — no new topic, no new
# persistent store.
#
# Finished tasks: the route still works for a task that has already
# completed. The snapshot replays whatever ring-buffer events are still
# in retention (sub-agent reflections may have aged out — that's
# expected; the ``task_completed`` row survives in SQLite and the
# frontend uses ``task.result`` + ``ui_payload`` for the dead-task
# overlay). The frame format is identical so the same client hook can
# render both phases.


@router.websocket("/ws/task/{task_id}")
async def task_ws(websocket: WebSocket, task_id: str) -> None:
    """Snapshot-then-tail per-task event subscription (issue 0052).

    Wire protocol (frame types):

    - ``snapshot``: first frame, carries a list of every currently
      buffered :class:`bob.debug_log.DebugEvent` whose ``task_id``
      matches. Each item is the event's ``to_dict()`` shape so the
      frontend can reuse the same renderer as the debug feed.
    - ``tail``: every subsequent frame, one event per frame, same shape.

    Wrapping each phase in its own envelope lets the consumer treat the
    snapshot as a single transactional render and stream tail events
    one-by-one without re-reading the snapshot wrapper. The two phases
    share the same WS session — no HTTP-then-WS upgrade race.

    Implementation: phase 1 reads the ring-buffer snapshot via
    :func:`bob.event_bus_v2.get_snapshot_for_task`; phase 2 subscribes
    via :func:`bob.event_bus_v2.subscribe_for_task` and forwards only
    NON-replayed events (the replayed ones from the producer's snapshot
    pass overlap with what we just sent; we drop them to avoid double
    delivery). A tiny duplicate window exists between the snapshot copy
    and the subscription start — events emitted in that microsecond gap
    will appear in both phase 1 and phase 2's snapshot pass. We
    deduplicate by ``(ts, source, summary)`` tuple.
    """

    await websocket.accept()
    emit_debug(
        category="system",
        severity="info",
        source="bob.ws_router.task_ws",
        summary=f"Overlay WS connecté (task={task_id})",
        payload={"task_id": task_id},
    )

    # Verify the task exists when the store is primed. We don't reject
    # the connection on unknown ids — the frontend may open the overlay
    # on a task whose row is not yet in the store snapshot we read here
    # (race). An unknown id simply yields an empty snapshot + a tail
    # that never matches; the client renders the empty-state.
    with contextlib.suppress(RuntimeError, TaskStoreError):
        task_store_module.get_default_store().get_task(task_id)

    snapshot_events = get_snapshot_for_task(task_id)
    sent_keys: set[tuple[str, str, str]] = {
        (event.ts, event.source, event.summary) for event in snapshot_events
    }
    try:
        await websocket.send_json(
            {
                "type": "snapshot",
                "task_id": task_id,
                "events": [event.to_dict() for event in snapshot_events],
            }
        )

        async for event in subscribe_for_task(task_id):
            key = (event.ts, event.source, event.summary)
            # Producer-side snapshot pass overlaps with our phase 1
            # snapshot — skip events we already sent (whether replayed
            # or not). Anything not already in ``sent_keys`` is a fresh
            # event and goes out as a ``tail`` frame.
            if key in sent_keys:
                continue
            sent_keys.add(key)
            await websocket.send_json({"type": "tail", "event": event.to_dict()})
    except WebSocketDisconnect:
        pass
    except Exception:
        _logger.exception("ws_task.stream_failed", task_id=task_id)
        return
    finally:
        emit_debug(
            category="system",
            severity="info",
            source="bob.ws_router.task_ws",
            summary=f"Overlay WS déconnecté (task={task_id})",
            payload={"task_id": task_id},
        )
