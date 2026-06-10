"""WebSocket router for the chat endpoint.

Wires the ``/ws/chat`` endpoint to :class:`bob.orchestrator.Orchestrator`
(via :func:`bob.orchestrator.get_default_orchestrator`) and streams Kokoro TTS
audio back to the client as **binary** WS frames.

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
import json
import time
import traceback
import uuid
from collections.abc import Awaitable, Callable, Coroutine
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import httpx
import openai
import structlog
from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from bob import jarvis_store as jarvis_store_module
from bob import task_scheduler as task_scheduler_module
from bob import task_store as task_store_module
from bob import text_segmenter, turn_metrics, voice_retention_policy, ws_events
from bob import voice_store as voice_store_module
from bob.config import Settings, get_settings
from bob.debug_log import emit_debug
from bob.event_bus_v2 import emit_event, get_snapshot_for_task, subscribe_for_task
from bob.live_transcript_state import LiveTranscriptState
from bob.orchestrator import Orchestrator, OrchestratorResponse, get_default_orchestrator
from bob.speculative_draft import SpeculativeDraft, ToolIntentPredicate
from bob.speech_pipeline import ChunkBatchSummary, SpeechStreamPipeline
from bob.spoken_text_cleaner import clean_for_speech
from bob.stt_engine import SttEngine, WhisperCppSttEngine, get_default_stt_engine
from bob.task_store import TaskStoreError
from bob.task_supervisor import create_supervised_task
from bob.thinker_loop import ThinkerLoop
from bob.tts_service import KokoroTtsService, SynthesisChunk, get_default_tts_service
from bob.turn_watchdog import TURN_TIMEOUT_FALLBACK_SPEECH, TurnTimeoutError, TurnWatchdog
from bob.voice_loop import FullDuplexLoop, PersistedTurn, SayPathDriver
from bob.voice_turn import VoiceTurn
from bob.wake_word import WakeWordDetector

router = APIRouter()
_logger = structlog.get_logger(__name__)


def _now_iso() -> str:
    """ISO-8601 UTC timestamp for the voice persistence rows (issue 0109)."""

    return datetime.now(UTC).isoformat()


# Per-session shape:
#   {"active_tts": list[tuple[str, asyncio.Task[None]]]}
_sessions: dict[str, dict[str, Any]] = {}

_orchestrator_provider: Callable[[], Orchestrator] = get_default_orchestrator
_tts_service_provider: Callable[[], KokoroTtsService] = get_default_tts_service
_stt_engine_provider: Callable[[], SttEngine] = get_default_stt_engine


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


def set_stt_engine_provider(provider: Callable[[], SttEngine]) -> None:
    """Override the STT engine factory (tests / attest scenarios)."""

    global _stt_engine_provider
    _stt_engine_provider = provider


def reset_stt_engine_provider() -> None:
    global _stt_engine_provider
    _stt_engine_provider = get_default_stt_engine


def _default_tool_intent_provider() -> ToolIntentPredicate | None:
    """No predicate by default — the bare loop always speculates (issue 0104)."""

    return None


# PRD 0016 / issue 0104 — the tool-intent gate. The lifespan wires a predicate
# built over the LIVE sub-agent registry (gmail + web + MCP fleet) via
# :func:`bob.tool_intent.build_tool_intent_predicate`, so a turn that would
# dispatch a tool produces NO draft and stays COLD. ``None`` (tests / bare
# boot) keeps the pre-wiring behaviour: every turn speculates.
_tool_intent_provider: Callable[[], ToolIntentPredicate | None] = _default_tool_intent_provider


def set_tool_intent_provider(provider: Callable[[], ToolIntentPredicate | None]) -> None:
    """Override the Draft's tool-intent predicate factory (lifespan / tests)."""

    global _tool_intent_provider
    _tool_intent_provider = provider


def reset_tool_intent_provider() -> None:
    global _tool_intent_provider
    _tool_intent_provider = _default_tool_intent_provider


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
    # ``voice_loop`` holds the active :class:`bob.voice_loop.FullDuplexLoop` for
    # the « Listen » → say-path full-duplex loop (issue 0100, building on the
    # 0099 STT spine): created on ``voice_start``, torn down on ``voice_stop`` /
    # socket close. ``None`` when the mic is not armed. The loop owns the
    # per-utterance :class:`bob.voice_turn.VoiceTurn` STT sessions internally.
    # ``half_duplex_gate`` (issue 0101 / Annexe G): sticky per-session flag set
    # when the client reports runtime AEC failure via ``voice_aec_degraded``.
    # The mic muting itself is client-side; the flag + the emitted warn event are
    # the observable backend half of the net.
    # ``active_speech_pipeline`` (PRD 0018 / issue 0119): the in-flight
    # :class:`bob.speech_pipeline.SpeechStreamPipeline` registered by
    # ``_synthesize_and_stream`` for the duration of one outbound TTS stream —
    # the barge-in zero-grace path cuts it with the single synchronous
    # ``cancel()``. ``None`` when nothing is streaming.
    _sessions[session_id] = {
        "active_tts": [],
        "voice_mode": False,
        "voice_loop": None,
        "half_duplex_gate": False,
        "active_speech_pipeline": None,
    }
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
        # Issue 0124 — supervised: a synthesis that dies before/around the
        # streaming pipeline would otherwise be a "ghost audio" (the backend
        # believes it spoke; the user heard nothing, and the task exception
        # was never retrieved). The supervisor logs it + emits a debug event.
        task: asyncio.Task[None] = create_supervised_task(
            _synthesize_and_stream(websocket, session_id, msg_id, speech),
            name="tts.proactive_synthesis",
            session_id=session_id,
            msg_id=msg_id,
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
            # ``receive()`` (not ``receive_json()``) so the SAME socket carries
            # JSON text frames AND binary mic frames (issue 0099 / Annexe A.1).
            # Binary frames are PCM 16 kHz mono s16le with a 1-byte type tag;
            # text frames are the existing JSON protocol.
            message = await websocket.receive()
            if message.get("type") == "websocket.disconnect":
                break
            data_bytes = message.get("bytes")
            if data_bytes is not None:
                await _handle_binary_frame(data_bytes, session_id)
                continue
            text = message.get("text")
            if text is None:
                continue
            try:
                payload = json.loads(text)
            except json.JSONDecodeError:
                # Malformed text frame — report and keep the socket alive
                # rather than tearing the connection down.
                await websocket.send_json(
                    {"type": "error", "code": "bad_payload", "message": "frame is not valid JSON"}
                )
                continue
            await _handle_client_message(websocket, payload, session_id, orchestrator)
    except WebSocketDisconnect:
        pass
    finally:
        ws_events.remove_emitter(_session_emit)
        # Socket closed mid-turn (Annexe G: WS binaire coupé en plein tour) —
        # finalize the active voice turn so the partial transcript is frozen
        # and persisted rather than abandoned.
        await _finalize_active_voice_turn(session_id)
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
            # PRD 0008 / issue 0064; PRD 0010 / issue 0066 — carry the structured
            # deliverable section LIST on the replayed completion event so a
            # reconnecting / reloading client rebuilds the SectionsOverlay
            # instead of treating the stored result purely as Markdown. This
            # frame is sent straight to the chat socket (not via the unified
            # bus), so it never lands in the debug ring buffer — no redaction is
            # needed here. Omitted (empty list) for summary-only tasks so older
            # clients keep rendering off ``result``.
            task_result_frame: dict[str, Any] = {
                "type": "task_result",
                "task_id": task.id,
                "result": task.result,
                "replayed": True,
            }
            if task.result_payload:
                task_result_frame["result_payload"] = task.result_payload
            await websocket.send_json(task_result_frame)


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


def _make_wake_detector(settings: Settings) -> WakeWordDetector | None:
    """Build the « Yo Bob » wake-word detector when the deployment opted in.

    Only on the real whisper.cpp path: the fake STT engine (tests / ``bob
    attest``) keeps the 0099/0100 contract — every speech opens a turn — so
    the deterministic scenarios are untouched by the wake gate. The detector
    gets its OWN small-model engine (``WAKE_WORD_MODEL``, default ``tiny``)
    so standby passes stay cheap; it lazy-loads on the first pass.
    """

    if not settings.WAKE_WORD_ENABLED or settings.STT_ENGINE != "whisper_cpp":
        return None
    if not isinstance(_stt_engine_provider(), WhisperCppSttEngine):
        # A seam-injected engine (tests / attest harness) means the deployment
        # is NOT on the real whisper path even if the setting says so — keep
        # the 0099 contract (every frame transcribes, no wake gate).
        return None
    wake_settings = settings.model_copy(update={"STT_MODEL": settings.WAKE_WORD_MODEL})
    engine = WhisperCppSttEngine(wake_settings)
    return WakeWordDetector(
        transcriber=engine.transcribe_pcm,
        phrase=settings.WAKE_WORD_PHRASE,
        sample_rate=settings.STT_SAMPLE_RATE,
        window_seconds=settings.WAKE_WORD_WINDOW_SECONDS,
        interval_seconds=settings.WAKE_WORD_INTERVAL_SECONDS,
        threshold=settings.WAKE_WORD_MATCH_THRESHOLD,
    )


async def _handle_voice_start(
    websocket: WebSocket,
    payload: dict[str, Any],
    session_id: str,
    orchestrator: Orchestrator,
) -> None:
    """Client → ``voice_start`` arms the mic for the full-duplex loop (0100).

    Annexe A.1: ``{type, window, ts_client}``. The HUD ``new`` window sends this
    when the voice toggle is ON; the binary mic frames that follow (tag
    ``0x01``) drive the :class:`bob.voice_loop.FullDuplexLoop` — VAD detects
    speech, the silence-floor endpoint freezes the transcript and runs the
    EXISTING Jarvis say-path, and the FSM emits ``turn_state`` throughout —
    until a matching ``voice_stop`` (or socket close) tears the loop down.

    Gating: when ``STT_ENABLED`` is false we refuse with ``stt_disabled`` and
    arm nothing (the server stays up). A ``voice_start`` while a loop is already
    armed tears the previous one down first (defensive — the client should pair
    start/stop, but a dropped ``voice_stop`` must not wedge the session).
    """

    settings = get_settings()
    if not settings.STT_ENABLED:
        await websocket.send_json(
            {"type": "error", "code": "stt_disabled", "message": "STT is disabled (STT_ENABLED)"}
        )
        return

    session = _sessions.get(session_id)
    if session is None:
        return

    # Defensive: a lingering loop (missed voice_stop) is torn down before the
    # new one arms so we never run two loops for one socket. Issue 0125 — the
    # slot is cleared BEFORE the stop (a frame racing the teardown can never
    # route to the dying loop) and the stop runs under exception suppression:
    # a failing stop must never abort the handler and leave the slot pointing
    # at a dead loop — two rapid ``voice_start``s always converge on exactly
    # one live loop, the new one.
    existing = session.get("voice_loop")
    session["voice_loop"] = None
    if isinstance(existing, FullDuplexLoop):
        try:
            await existing.stop()
        except Exception:
            _logger.exception("ws_chat.voice_start_stale_loop_stop_failed", session_id=session_id)
            emit_debug(
                category="voice",
                severity="warn",
                source="bob.ws_router._handle_voice_start",
                summary=f"stale voice loop stop failed (session={session_id})",
                payload={"session_id": session_id},
            )

    # PRD 0016 / issue 0102 — the « Penser en parallèle » étage. Build the
    # per-session live-transcript store + ThinkerLoop (mini ``thinker`` role
    # client), install the store on the orchestrator so the Speaker consults the
    # SAME snapshot the loop writes, and wire the loop's hooks behind the FSM's
    # symbolic start/feed/stop Thinker actions. Degrades cleanly: if the thinker
    # client cannot be built the loop is omitted and the bare full-duplex loop
    # runs exactly as in 0101.
    thinker = _make_thinker_loop(session_id, settings)
    if thinker is not None:
        orchestrator.set_live_transcript_state(thinker.live_state)

    # PRD 0016 / issue 0104 — the anticipation capstone. Build the per-session
    # SpeculativeDraft on the mini ``draft`` role client; it pre-writes the
    # conversational reply on the partial transcript IN PARALLEL with the Thinker
    # (distinct role clients). Degrades cleanly (Annexe G "Draft model indispo →
    # désactive l'anticipation, toujours froid"): if the draft client cannot be
    # built the drafter is omitted and every turn regenerates COLD.
    drafter = _make_speculative_draft(session_id, settings)

    loop = FullDuplexLoop(
        voice_turn_factory=lambda: VoiceTurn(
            engine=_stt_engine_provider(), session_id=session_id, settings=settings
        ),
        say_path=_make_say_path_driver(websocket, session_id, orchestrator),
        settings=settings,
        session_id=session_id,
        # Barge-in (issue 0101): persist what Bob actually played before the cut
        # into the Jarvis history, and restart the Thinker (issue 0102) so the
        # resumed turn re-plans from the partial.
        commit_spoken=_make_commit_spoken(session_id),
        # Voice persistence (issue 0109): write the voice_turns row + mic/tts WAV
        # blobs at every finalize, link the transcript into Jarvis history, emit
        # the persistence + retention events. ``None`` when persistence is off
        # (master switch) so capture + writes never happen.
        persist_turn=(_make_persist_turn(session_id) if settings.VOICE_PERSIST_ENABLED else None),
        on_thinker_restart=(thinker.restart if thinker is not None else None),
        on_thinker_start=(thinker.loop.start if thinker is not None else None),
        on_thinker_feed=(thinker.loop.feed_partial if thinker is not None else None),
        on_thinker_stop=(thinker.loop.stop if thinker is not None else None),
        # Barge-in zero-grace (PRD 0018 / issue 0119): the confirmed interrupt
        # hard-cancels the in-flight Thinker pass SYNCHRONOUSLY (no cooperative
        # grace — unlike the endpoint freeze's capped ``stop``, 0118).
        on_thinker_cancel=(thinker.loop.hard_cancel if thinker is not None else None),
        # Barge-in zero-grace (issue 0119): ONE synchronous call cuts the
        # in-flight TTS pipeline (synthesis + drain — issue 0121) the instant
        # the interruption is confirmed.
        cancel_speech=_make_cancel_speech(session_id),
        # Semantic endpoint (issue 0103): a pure read of the Thinker's latest
        # ``user_turn_complete`` from the SAME live-transcript store the loop
        # writes. The Endpointer fires the endpoint early only once a stable
        # partial confirms it (Annexe H); ``None`` keeps the silence-floor net.
        thinker_complete=(thinker.user_turn_complete if thinker is not None else None),
        # Backchannels (issue 0105): on a ``vad_pause`` the loop reads the
        # Thinker's latest ``backchannel`` trigger (relevance) and, gated by the
        # proactivity refractory, plays a SHORT token via Kokoro WITHOUT a floor
        # transition. Both ``None`` when the Thinker is unavailable (bare loop) so
        # no backchannel ever fires.
        backchannel_trigger=(thinker.backchannel if thinker is not None else None),
        backchannel_tts=(
            _make_backchannel_tts(websocket, session_id) if thinker is not None else None
        ),
        # SpeculativeDraft (issue 0104): arm/feed/stop mirror the Thinker hooks; the
        # commit gate (PURE) runs at the endpoint on the FINAL transcript and the
        # decision event is emitted via the drafter. All ``None`` when the draft
        # model is unavailable (Annexe G) → anticipation off, every turn cold.
        on_draft_start=(drafter.start if drafter is not None else None),
        on_draft_feed=(drafter.feed_partial if drafter is not None else None),
        on_draft_stop=(drafter.stop if drafter is not None else None),
        # Barge-in zero-grace (issue 0119): mirror of ``on_thinker_cancel``.
        on_draft_cancel=(drafter.hard_cancel if drafter is not None else None),
        draft_commit_gate=(drafter.commit_gate if drafter is not None else None),
        draft_emit_decision=(drafter.emit_decision if drafter is not None else None),
        # Wake word (« Yo Bob »): when enabled the armed window starts in
        # standby — no turn opens until the small-model detector hears the
        # phrase; the orb flips to « écoute » on the wake-opened turn.
        wake_detector=_make_wake_detector(settings),
    )
    # PRD 0018 / issue 0120 — semantic-endpoint fast path: each concluding
    # Thinker pass PUSHES its ``user_turn_complete`` bit straight into the
    # loop's endpoint logic, bypassing the inference-cadence debounce. The
    # ``thinker_complete`` per-frame poll above stays as the net (it re-arms a
    # pending endpoint a speech frame disarmed, and covers an unwired push).
    if thinker is not None:
        thinker.loop.on_turn_complete = loop.note_thinker_complete
    session["thinker_loop"] = thinker.loop if thinker is not None else None
    session["speculative_draft"] = drafter
    started = await loop.start()
    if not started:
        # The first STT session failed to open; the VoiceTurn already emitted
        # its abort event (Annexe G). Leave the slot empty so binary frames are
        # dropped until the next voice_start.
        session["voice_loop"] = None
        return
    session["voice_loop"] = loop
    emit_debug(
        category="voice",
        severity="info",
        source="bob.ws_router._handle_voice_start",
        summary=f"voice_start (session={session_id})",
        payload={"session_id": session_id},
    )


def _make_say_path_driver(
    websocket: WebSocket, session_id: str, orchestrator: Orchestrator
) -> SayPathDriver:
    """Build the :class:`bob.voice_loop.SayPathDriver` bound to this session.

    At ``endpoint`` the loop hands us the frozen transcript; we run the EXISTING
    Jarvis say-path (``process_user_message`` → ``assistant_msg``) and stream the
    reply's TTS audio out on ``websocket`` — the SAME path a ``user_msg`` /
    ``client_text`` turn takes (the two converge here). ``on_first_audio`` fires
    just before the first outbound chunk so the loop marks
    ``t_first_audio_chunk`` + flips the FSM into ``bob_speaking``. An empty
    transcript (no speech captured) is skipped so we never run a turn on
    nothing.

    ``prepared_reply`` (PRD 0016 / issue 0104): a COMMITTED speculative draft. The
    endpoint's commit gate adopted a pre-written reply, so we BYPASS the cold
    Speaker generation (``process_user_message``) and speak the draft verbatim —
    the trivial-validation adoption that makes the reply near-instant. We still
    persist it as the assistant turn in the Jarvis history (so conversational
    continuity holds) and emit the SAME ``assistant_msg`` + TTS stream the cold
    path emits, so the client + the latency marks are identical apart from the
    skipped generation. ``None`` keeps the cold path unchanged.
    """

    async def _drive(
        transcript: str,
        *,
        turn_id: str,
        on_first_audio: Callable[[], Awaitable[None]],
        on_spoken_progress: Callable[[str], Awaitable[None]] | None = None,
        on_audio_chunk: Callable[[bytes, int], Awaitable[None]] | None = None,
        prepared_reply: str | None = None,
    ) -> None:
        if not transcript.strip():
            return
        if prepared_reply is not None and prepared_reply.strip():
            # Committed speculative draft (issue 0104): adopt the pre-written text
            # verbatim. Persist it as the assistant turn (the orchestrator's ``say``
            # handler would have done this on the cold path) so the conversation
            # history reflects what Bob actually said, then stream it like any reply.
            speech = prepared_reply.strip()
            msg_id = uuid.uuid4().hex
            with contextlib.suppress(RuntimeError):
                jarvis_store_module.get_default_store().append("assistant", speech)
            ui: list[Any] = []
            # Emit the SAME ``category="output"`` reply event the cold say-path
            # emits from ``process_user_message`` — so a committed draft is
            # observably a reply (the deliverable projection + the Debug View read
            # it identically whether Bob spoke a draft or a cold generation).
            emit_debug(
                category="output",
                severity="info",
                source="bob.ws_router.say_path_committed_draft",
                summary=f'Bob répond (draft): "{speech[:80]}"',
                payload={
                    "speech": speech,
                    "ui": [],
                    "proactive": False,
                    "draft_committed": True,
                },
            )
        else:
            response = await orchestrator.process_user_message(session_id, transcript)
            speech = response.speech
            msg_id = response.msg_id or uuid.uuid4().hex
            ui = [component.model_dump() for component in response.ui]
        await websocket.send_json(
            {
                "type": "assistant_msg",
                "msg_id": msg_id,
                "speech": speech,
                "ui": ui,
                "proactive": False,
            }
        )
        if not speech.strip():
            return
        await _synthesize_and_stream(
            websocket,
            session_id,
            msg_id,
            speech,
            on_first_audio=on_first_audio,
            # Barge-in needs to know what Bob actually played (issue 0101).
            on_spoken_progress=on_spoken_progress,
            # Voice persistence captures Bob's outbound PCM (issue 0109).
            on_audio_chunk=on_audio_chunk,
        )

    return _drive


def _make_cancel_speech(session_id: str) -> Callable[[], None]:
    """Build the barge-in TTS kill-switch hook (PRD 0018 / issue 0119).

    Returns a SYNCHRONOUS callable the full-duplex loop invokes on a confirmed
    interruption, BEFORE any await: it cuts the session's in-flight
    :class:`bob.speech_pipeline.SpeechStreamPipeline` (registered by
    ``_synthesize_and_stream`` for the duration of one outbound stream) with the
    pipeline's single idempotent ``cancel()`` — synthesis AND drain stop
    together, so no further audio chunk reaches the client even while the
    cancelled say-path task is still unwinding. A session with nothing streaming
    (or already torn down) is a silent no-op.
    """

    def _cancel() -> None:
        session = _sessions.get(session_id)
        if session is None:
            return
        pipeline = session.get("active_speech_pipeline")
        if isinstance(pipeline, SpeechStreamPipeline):
            pipeline.cancel()

    return _cancel


def _make_backchannel_tts(
    websocket: WebSocket, session_id: str
) -> Callable[[str, str], Awaitable[None]]:
    """Build the backchannel short-token synthesis hook (PRD 0016 / issue 0105).

    On a gated ``vad_pause`` the full-duplex loop hands us the (live) ``turn_id``
    + a brief token ("mm", "ok je vois"); we synthesise it via the SAME Kokoro
    engine (fake TTS under attest) and stream the PCM out on ``websocket`` — but
    deliberately NOT through the say-path machinery: a backchannel is an action in
    the user's pause, NOT a floor turn, so it carries its OWN synthetic
    ``msg_id`` (``backchannel:<turn>:<n>``), never flips the FSM, and never runs
    the orchestrator. The audio rides as ``audio_start`` → raw PCM bytes →
    ``audio_end`` (the same wire contract the say-path uses, so the client plays
    it identically) and surfaces one ``audio_chunk`` voice event per block for the
    debug stream / harness. The loop already wraps this in ``contextlib.suppress``
    so any synthesis error is swallowed — a backchannel hiccup must never perturb
    the user's live turn (they keep the floor).
    """

    counter = 0

    async def _backchannel(turn_id: str, token: str) -> None:
        nonlocal counter
        cleaned = clean_for_speech(token).strip()
        if not cleaned:
            return
        counter += 1
        msg_id = f"backchannel:{turn_id}:{counter}"
        tts = _tts_service_provider()
        started_audio = False
        audio_chunks = 0
        try:
            async for chunk in tts.synthesize_stream(cleaned):
                if not started_audio:
                    await websocket.send_json(
                        {"type": "audio_start", "msg_id": msg_id, "sample_rate": chunk.sample_rate}
                    )
                    started_audio = True
                await websocket.send_bytes(chunk.pcm16)
                # One ``audio_chunk`` voice event per block (same observable proxy
                # for "Bob made a sound" the say-path emits) so the harness can
                # count the backchannel audio off ``/ws/debug``.
                await emit_event(
                    {
                        "type": "audio_chunk",
                        "msg_id": msg_id,
                        "chunk_index": audio_chunks,
                        "bytes": len(chunk.pcm16),
                        "sample_rate": chunk.sample_rate,
                        "backchannel": True,
                    },
                    category="voice",
                    severity="debug",
                    source="bob.ws_router.audio_chunk",
                    summary=f"audio_chunk #{audio_chunks} (backchannel msg={msg_id})",
                )
                audio_chunks += 1
        finally:
            # Always terminate the synthetic stream so the client's audio queue
            # for this ``msg_id`` is closed even if synthesis yielded nothing.
            if started_audio:
                await websocket.send_json({"type": "audio_end", "msg_id": msg_id})

    return _backchannel


def _make_commit_spoken(session_id: str) -> Callable[[str, str], Awaitable[None]]:
    """Build the barge-in ``commit_spoken_partial`` hook (PRD 0016 / issue 0101).

    On a confirmed barge-in the loop hands us the (interrupted) ``turn_id`` and
    the ``committed_spoken_text`` — the text Bob actually played before the cut.
    We append it to the persistent Jarvis history as the assistant turn, marked
    truncated, so the resumed turn (and any later context build) sees what Bob
    *did* say rather than the full un-played reply. Persisting to the
    ``voice_turns`` table (Annexe E.1 ``spoken_text``) is a later slice; the
    Jarvis-history commit is the load-bearing one for conversational continuity.
    Silently skips when the store is not primed (narrow test setups).
    """

    async def _commit(turn_id: str, committed_spoken_text: str) -> None:
        try:
            store = jarvis_store_module.get_default_store()
        except RuntimeError:
            return
        # Mark the truncation so the history reflects that Bob was cut off — the
        # ellipsis is a cheap, human-readable signal (no schema change needed).
        store.append("assistant", f"{committed_spoken_text}…")
        emit_debug(
            category="voice",
            severity="debug",
            source="bob.ws_router._commit_spoken",
            summary=f"barge-in committed spoken partial (turn={turn_id})",
            payload={"session_id": session_id, "turn_id": turn_id},
        )

    return _commit


def _draft_outcome_from_latency(marks: dict[str, float], derived: dict[str, Any]) -> str:
    """Map the Annexe F latency body to the ``voice_turns.draft_outcome`` enum (0104).

    - ``committed`` — ``draft_hit`` is True (Bob spoke a committed speculative
      draft this turn).
    - ``discarded`` — a draft was produced at the gate (``t_draft_ready`` stamped)
      but the commit gate rejected it (divergence) → Bob regenerated COLD.
    - ``none`` — no draft this turn (the drafter was unwired / suppressed / never
      fired). The single source is the same latency body the event + the
      ``latency_json`` carry, so the persisted outcome can never drift from the
      marks.
    """

    if bool(derived.get("draft_hit")):
        return "committed"
    if "t_draft_ready" in marks:
        return "discarded"
    return "none"


def _make_persist_turn(session_id: str) -> Callable[[PersistedTurn], Awaitable[None]]:
    """Build the voice-turn persistence hook (PRD 0016 / issue 0109, Annexe E).

    On every finalized voice turn the full-duplex loop hands us a
    :class:`bob.voice_loop.PersistedTurn` snapshot. We:

    1. write the ``voice_turns`` row (transcript, spoken text, end reason,
       latency JSON; ``draft_outcome`` derived from the Annexe F ``draft_hit`` —
       issue 0104 — committed / discarded / none);
    2. write the ``mic_in`` + ``tts_out`` audio as WAV files on disk (paths in
       ``voice_audio_blobs``) — an empty recording is simply skipped;
    3. link the final transcript into the persistent Jarvis history and record
       the resulting message id in ``voice_turns.jarvis_msg_id`` (skipped on a
       barge-in turn, where issue 0101 already appended the played prefix, and
       when there is no transcript);
    4. emit a ``voice_turn_persisted`` voice event the attest harness asserts on;
    5. enforce :class:`bob.voice_retention_policy.VoiceRetentionPolicy` and, when
       it evicted anything, emit ``voice_retention_purged``.

    Silently degrades when the stores are not primed (narrow test setups) and
    never raises — the loop already wraps the hook, but we keep the boundary
    clean so a persistence hiccup cannot perturb the live turn.
    """

    async def _persist(turn: PersistedTurn) -> None:
        try:
            store = voice_store_module.get_default_store()
        except RuntimeError:
            return

        started_at = _now_iso()
        latency_json: str | None = None
        if turn.marks or turn.derived:
            latency_json = json.dumps({"marks": turn.marks, "derived": turn.derived})

        # 3) Link the final transcript into Jarvis history (Annexe E.1). A
        #    barge-in turn already had its played prefix appended by issue 0101's
        #    commit_spoken, so we don't double-append there; a turn with no
        #    transcript (Bob never heard anything) has nothing to link.
        jarvis_msg_id: str | None = None
        if turn.end_reason != "bargein" and turn.final_transcript.strip():
            try:
                jarvis_store = jarvis_store_module.get_default_store()
                jarvis_msg_id = jarvis_store.append_returning_id("user", turn.final_transcript)
            except RuntimeError:
                jarvis_msg_id = None

        # 1) The voice_turns row. ``draft_outcome`` (issue 0104) is derived from
        #    the Annexe F latency body: ``draft_hit`` True ⇒ Bob spoke a committed
        #    speculative draft; else if a draft was produced at the gate
        #    (``t_draft_ready`` stamped) but not adopted ⇒ ``discarded``; else
        #    ``none`` (no draft this turn — cold path / drafter unwired).
        draft_outcome = _draft_outcome_from_latency(turn.marks, turn.derived)
        store.write_turn(
            turn_id=turn.turn_id,
            started_at=started_at,
            ended_at=_now_iso(),
            final_transcript=turn.final_transcript or None,
            spoken_text=turn.spoken_text or None,
            end_reason=turn.end_reason,
            draft_outcome=draft_outcome,
            latency_json=latency_json,
            jarvis_msg_id=jarvis_msg_id,
        )

        # 2) The audio blobs (WAV on disk, path in DB). Empty recordings skipped.
        blob_count = 0
        mic_blob = store.write_audio_blob(
            turn_id=turn.turn_id,
            kind="mic_in",
            pcm16=turn.mic_pcm,
            sample_rate=turn.mic_sample_rate,
        )
        if mic_blob is not None:
            blob_count += 1
        if turn.tts_pcm and turn.tts_sample_rate > 0:
            tts_blob = store.write_audio_blob(
                turn_id=turn.turn_id,
                kind="tts_out",
                pcm16=turn.tts_pcm,
                sample_rate=turn.tts_sample_rate,
            )
            if tts_blob is not None:
                blob_count += 1

        # 4) The persistence event the harness asserts on (black-box contract).
        await emit_event(
            {
                "type": "voice_turn_persisted",
                "turn_id": turn.turn_id,
                "end_reason": turn.end_reason,
                "blob_count": blob_count,
                "has_transcript": bool(turn.final_transcript.strip()),
                "jarvis_msg_id": jarvis_msg_id,
            },
            category="voice",
            severity="info",
            source="bob.ws_router.voice_turn_persisted",
            summary=f"voice_turn_persisted (turn={turn.turn_id}, blobs={blob_count})",
        )

        # 5) Retention sweep (Annexe E.3) — separate size/age caps. Emit a purge
        #    event only when something was actually evicted so the harness can
        #    assert it with a forced-tiny cap. Best-effort: never break persist.
        try:
            outcome = voice_retention_policy.enforce(store)
        except Exception:
            _logger.exception("ws_router.voice_retention_failed", session_id=session_id)
            return
        if outcome.anything:
            await emit_event(
                {
                    "type": "voice_retention_purged",
                    "blobs_deleted": outcome.blobs_deleted,
                    "turns_deleted": outcome.turns_deleted,
                    "audio_bytes_freed": outcome.audio_bytes_freed,
                },
                category="voice",
                severity="info",
                source="bob.ws_router.voice_retention_purged",
                summary=(
                    f"voice_retention_purged (blobs={outcome.blobs_deleted}, "
                    f"turns={outcome.turns_deleted})"
                ),
            )

    return _persist


@dataclass
class _ThinkerHandle:
    """Bundle the per-session ThinkerLoop + its store + the barge-in restart hook.

    PRD 0016 / issue 0102. Returned by :func:`_make_thinker_loop` so
    :func:`_handle_voice_start` can wire the loop's hooks onto the
    :class:`bob.voice_loop.FullDuplexLoop` and install the shared store on the
    orchestrator.
    """

    loop: ThinkerLoop
    live_state: LiveTranscriptState

    async def restart(self, turn_id: str) -> None:
        """Barge-in ``start_thinker`` — cancel the in-flight pass then re-arm.

        On a confirmed barge-in the resumed turn re-plans from the user's new
        utterance: stop the current pass then arm the loop for the same
        ``turn_id`` so the next partial triggers a fresh pass. Since issue 0119
        the loop has ALREADY hard-cancelled the in-flight pass synchronously via
        ``on_thinker_cancel`` (zero grace) before this hook fires, so the
        cooperative ``stop`` here is normally an instant no-op — kept as a
        defensive net for a pass racing the cut.
        """

        await self.loop.stop()
        self.loop.start(turn_id)

    def user_turn_complete(self) -> bool:
        """Latest ``user_turn_complete`` from the Thinker's snapshot (issue 0103).

        Pure, cheap read of the shared :class:`LiveTranscriptState` the loop
        polls each frame to drive the SEMANTIC endpoint (Annexe B + H). ``False``
        when no snapshot has landed yet (the silence floor stays the net) — so a
        turn the Thinker never flags simply ends on silence as before.
        """

        snapshot = self.live_state.latest()
        return bool(snapshot.user_turn_complete) if snapshot is not None else False

    def backchannel(self) -> str | None:
        """Latest ``backchannel`` trigger from the Thinker's snapshot (issue 0105).

        Pure, cheap read of the shared :class:`LiveTranscriptState` the loop
        consults on each ``vad_pause`` to decide whether to drop a brief
        acknowledgement (Annexe B ``maybe_backchannel``). ``None`` when no snapshot
        has landed yet or the Thinker had nothing to interject — so a pause the
        Thinker never flagged stays silent (the proactivity gate, not systematic).
        """

        snapshot = self.live_state.latest()
        return snapshot.backchannel if snapshot is not None else None


def _make_thinker_loop(session_id: str, settings: Settings) -> _ThinkerHandle | None:
    """Build the per-session :class:`ThinkerLoop` on the ``thinker`` role client.

    PRD 0016 / issue 0102 (Annexe D + the « Penser en parallèle » étage). Reads
    the per-role selection (:class:`bob.llm_selection_store.RoleSelectionStore`,
    default a mini local model) and builds the ``thinker`` role client via
    :func:`bob.llm.factory.build_thinker_role_client`, then constructs the loop
    over a fresh per-session :class:`LiveTranscriptState`. The inference is
    spawned through the scheduler's shared :class:`asyncio.TaskGroup` so a passing
    pass cannot leak past an orchestrator crash (structured concurrency); when
    the scheduler has not been started we fall back to a bare task.

    Returns ``None`` (the bare full-duplex loop runs, no Thinker — Annexe G
    "Draft model indispo" style degrade) when the role store is not primed or the
    client cannot be built, so a misconfigured thinker role never wedges voice.
    """

    from bob import llm_selection_store
    from bob.llm.factory import build_thinker_role_client

    try:
        role_selection = llm_selection_store.get_default_role_store().read()
    except RuntimeError:
        role_selection = None
    if role_selection is None:
        role_selection = llm_selection_store.RoleSelection(
            roles={
                role: llm_selection_store.LLMSelection(
                    provider=settings.LLM_PROVIDER,
                    lm_model=settings.LLM_MODEL,
                    base_url=settings.LLM_BASE_URL or None,
                )
                for role in llm_selection_store.ROLES
            }
        )
    try:
        client = build_thinker_role_client(role_selection, settings)
    except Exception:
        _logger.warning("ws_router.thinker_client_build_failed", session_id=session_id)
        return None

    live_state = LiveTranscriptState()
    loop = ThinkerLoop(
        client=client,
        live_state=live_state,
        settings=settings,
        session_id=session_id,
        spawn=_thinker_spawn,
    )
    return _ThinkerHandle(loop=loop, live_state=live_state)


def _thinker_spawn(coro: Coroutine[Any, Any, None]) -> asyncio.Task[None]:
    """Spawn the ThinkerLoop's inference onto the scheduler's TaskGroup if live.

    Routes through :meth:`bob.task_scheduler.TaskScheduler` shared
    :class:`asyncio.TaskGroup` (structured concurrency) when the scheduler has
    been started; otherwise a bare :func:`asyncio.create_task`. The scheduler
    exposes the group via its private ``_task_group`` — the same handle its own
    sub-agent runners use; reading it here keeps the Thinker under the one
    cancellation umbrella the lifespan owns.
    """

    try:
        scheduler = task_scheduler_module.get_default_scheduler()
        group = scheduler._task_group  # shared TaskGroup handle (structured concurrency).
    except RuntimeError:
        group = None
    if group is not None:
        return group.create_task(coro)
    return asyncio.create_task(coro)


def _make_speculative_draft(session_id: str, settings: Settings) -> SpeculativeDraft | None:
    """Build the per-session :class:`SpeculativeDraft` on the ``draft`` role client.

    PRD 0016 / issue 0104 (Annexe D + G). Reads the per-role selection
    (:class:`bob.llm_selection_store.RoleSelectionStore`, default a mini fast
    model) and builds the ``draft`` role client via
    :func:`bob.llm.factory.build_draft_role_client`, then constructs the drafter.
    Its inference is spawned through the SAME scheduler TaskGroup the Thinker uses
    (:func:`_thinker_spawn`) so a passing pass cannot leak past an orchestrator
    crash (structured concurrency).

    Returns ``None`` (anticipation OFF — every turn COLD, Annexe G "Draft model
    indispo") when the role store is not primed or the client cannot be built, so
    a misconfigured ``draft`` role never wedges voice. The full-duplex loop then
    runs exactly as 0100/0103 (no draft hooks wired).
    """

    from bob import llm_selection_store
    from bob.llm.factory import build_draft_role_client

    try:
        role_selection = llm_selection_store.get_default_role_store().read()
    except RuntimeError:
        role_selection = None
    if role_selection is None:
        role_selection = llm_selection_store.RoleSelection(
            roles={
                role: llm_selection_store.LLMSelection(
                    provider=settings.LLM_PROVIDER,
                    lm_model=settings.LLM_MODEL,
                    base_url=settings.LLM_BASE_URL or None,
                )
                for role in llm_selection_store.ROLES
            }
        )
    try:
        client = build_draft_role_client(role_selection, settings)
    except Exception:
        _logger.warning("ws_router.draft_client_build_failed", session_id=session_id)
        return None

    # Tool-intent gate (issue 0104): a turn that would dispatch a tool must
    # stay COLD — a committed draft would bypass the dispatch and speak a
    # hallucinated answer. A failing provider degrades to no gate (the draft
    # still speculates) rather than taking voice_start down.
    try:
        is_tool_intent = _tool_intent_provider()
    except Exception:
        _logger.exception("ws_router.tool_intent_provider_failed", session_id=session_id)
        is_tool_intent = None

    return SpeculativeDraft(
        client=client,
        settings=settings,
        session_id=session_id,
        spawn=_thinker_spawn,
        is_tool_intent=is_tool_intent,
    )


async def _handle_voice_stop(
    websocket: WebSocket, payload: dict[str, Any], session_id: str, orchestrator: Orchestrator
) -> None:
    """Client → ``voice_stop`` disarms the mic and tears the loop down (0100).

    Annexe A.1 ``{type, ts_client}`` / Annexe B ``* + voice_stop -> idle``.
    Stops the active loop (finalizing its open STT turn → ``stt_final`` and
    driving the FSM to idle) and clears the slot. A ``voice_stop`` with no armed
    loop is a silent no-op (idempotent).

    Issue 0102: stopping the full-duplex loop already cooperatively cancels the
    in-flight ThinkerLoop (via ``on_thinker_stop``); we additionally reset the
    orchestrator's live-transcript store to a fresh empty one so a later text
    turn never consults a stale snapshot from the closed voice session.
    """

    session = _sessions.get(session_id)
    if session is None:
        return
    loop = session.get("voice_loop")
    if isinstance(loop, FullDuplexLoop):
        await loop.stop()
        # The loop teardown already cooperatively cancelled the ThinkerLoop;
        # reset the orchestrator's live-transcript store to a fresh empty one so
        # a later text turn never consults a stale snapshot. Only when a loop was
        # actually armed (a bare ``voice_stop`` with no prior ``voice_start`` has
        # nothing to reset).
        orchestrator.set_live_transcript_state(LiveTranscriptState())
    session["voice_loop"] = None
    session["thinker_loop"] = None
    session["speculative_draft"] = None


async def _handle_voice_aec_degraded(
    websocket: WebSocket, payload: dict[str, Any], session_id: str
) -> None:
    """Client → ``voice_aec_degraded`` engages the half-duplex gate (Annexe G).

    PRD 0016 / issue 0101. When the webview detects that AEC is unavailable at
    runtime (echo re-detected, or a manual operator toggle) it sends
    ``{type: "voice_aec_degraded", engaged: true/false}``. We persist the sticky
    session flag and emit the ``aec_degraded_half_duplex`` ``voice`` warn event —
    the exact ``type`` the frontend handoff (``HALF_DUPLEX_GATE_SPEC``) names — so
    the Debug View and the attestation harness can assert the degradation. The
    actual mic muting is done client-side (the gate mutes outbound frames during
    ``bob_speaking``); this is the observable backend half of the net.

    Returns ``bad_aec_degraded`` when ``engaged`` is not a bool.
    """

    engaged = payload.get("engaged", True)
    if not isinstance(engaged, bool):
        await websocket.send_json(
            {
                "type": "error",
                "code": "bad_aec_degraded",
                "message": "voice_aec_degraded.engaged must be a boolean",
            }
        )
        return
    session = _sessions.get(session_id)
    if session is not None:
        session["half_duplex_gate"] = engaged
    await emit_event(
        {
            "type": "aec_degraded_half_duplex",
            "engaged": engaged,
            "session_id": session_id,
        },
        category="voice",
        severity="warn",
        source="bob.ws_router.aec_degraded_half_duplex",
        summary=(
            "AEC indisponible — half-duplex gate "
            f"{'engagé' if engaged else 'relâché'} (session={session_id})"
        ),
    )


async def _handle_binary_frame(data: bytes, session_id: str) -> None:
    """Route a binary mic frame (tag ``0x01``) to the armed full-duplex loop.

    A frame that arrives with no armed loop (race: frames in flight after a
    ``voice_stop``, or before ``voice_start`` landed) is silently dropped.
    Decoding + STT failures are owned downstream by the loop /
    :class:`bob.voice_turn.VoiceTurn` (bad frame → drop; transcription failure →
    clean turn abort).
    """

    session = _sessions.get(session_id)
    if session is None:
        return
    loop = session.get("voice_loop")
    if not isinstance(loop, FullDuplexLoop):
        return
    await loop.feed_raw_frame(data)


async def _finalize_active_voice_turn(session_id: str) -> None:
    """Tear down any armed full-duplex loop on socket close (Annexe G)."""

    session = _sessions.get(session_id)
    if session is None:
        return
    loop = session.get("voice_loop")
    if isinstance(loop, FullDuplexLoop):
        with contextlib.suppress(Exception):
            await loop.stop()
    session["voice_loop"] = None


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

    if msg_type == "voice_start":
        await _handle_voice_start(websocket, payload, session_id, orchestrator)
        return

    if msg_type == "voice_stop":
        await _handle_voice_stop(websocket, payload, session_id, orchestrator)
        return

    if msg_type == "voice_aec_degraded":
        await _handle_voice_aec_degraded(websocket, payload, session_id)
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
    # PRD 0018 / issue 0126 — TurnWatchdog: the text turn runs under a TTFT +
    # completion wall-clock budget. The orchestrator's streaming loop disarms
    # the TTFT timer on the first provider chunk; expiry cancels the turn and
    # delivers the short text fallback below instead of eternal silence.
    turn_settings = get_settings()
    watchdog = TurnWatchdog(
        ttft_timeout_s=turn_settings.TURN_TTFT_TIMEOUT_SECONDS,
        completion_timeout_s=turn_settings.TURN_COMPLETION_TIMEOUT_SECONDS,
    )
    try:
        response = await watchdog.guard(
            orchestrator.process_user_message(session_id, content),
            name="turn.text",
            session_id=session_id,
        )
    except TurnTimeoutError as exc:
        # Issue 0126 — expiry: emit the ``turn_timeout`` event (debug feed +
        # WS fan-out), persist the fallback so the conversation history stays
        # coherent, then fall through to the NORMAL emission path with the
        # fallback as the reply (so a voice-requested turn still voices it).
        _logger.error(
            "ws_chat.turn_timeout",
            session_id=session_id,
            phase=exc.phase,
            budget_seconds=exc.budget_seconds,
        )
        await emit_event(
            {
                "type": "turn_timeout",
                "path": "text",
                "phase": exc.phase,
                "budget_seconds": exc.budget_seconds,
                "session_id": session_id,
            },
            category="system",
            severity="error",
            source="bob.ws_router.turn_timeout",
            summary=(f"turn_timeout {exc.phase} ({exc.budget_seconds:g}s) — fallback texte"),
        )
        with contextlib.suppress(RuntimeError):
            jarvis_store_module.get_default_store().append(
                "assistant", TURN_TIMEOUT_FALLBACK_SPEECH
            )
        response = OrchestratorResponse(speech=TURN_TIMEOUT_FALLBACK_SPEECH)
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
        # Issue 0124 — supervised like the proactive spawn above: a failed
        # main-turn synthesis must be observable (log + debug event), never a
        # silently-dropped task exception.
        task: asyncio.Task[None] = create_supervised_task(
            _synthesize_and_stream(websocket, session_id, msg_id, response.speech),
            name="tts.turn_synthesis",
            session_id=session_id,
            msg_id=msg_id,
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
    *,
    on_first_audio: Callable[[], Awaitable[None]] | None = None,
    on_spoken_progress: Callable[[str], Awaitable[None]] | None = None,
    on_audio_chunk: Callable[[bytes, int], Awaitable[None]] | None = None,
) -> None:
    """Segment ``text``, stream PCM chunks to the WS via the speech pipeline.

    PRD 0018 / issue 0121: the path is owned by
    :class:`bob.speech_pipeline.SpeechStreamPipeline` —

        clean_for_speech(text) → text_segmenter.segment → pipeline.run:
            producer: tts.synthesize_stream(sentence N+1) overlaps
            consumer: websocket.send_bytes(sentence N's chunks)

    with a bounded queue between the two (a slow client parks synthesis at the
    bound, never unbounded memory). The pipeline places ``tts_first_chunk``
    (0117) at the first synthesized block; this function places
    ``audio_first_byte`` after the first ``send_bytes``. Per-chunk debug events
    are replaced by one batched ``audio_chunk_batch`` voice event per window
    (``SPEECH_PIPELINE_BATCH_WINDOW_MS``).

    First chunk of the whole turn triggers a single ``audio_start`` JSON
    header so the client knows the sample rate. ``first_audio_ms`` is
    logged once per ``msg_id`` for latency telemetry.

    ``on_spoken_progress`` (PRD 0016 / issue 0101): invoked after each sentence's
    chunks have fully left the socket, with the cumulative cleaned text spoken so
    far. The full-duplex loop uses it to know exactly what Bob *played* — so a
    barge-in cut commits that prefix (``committed_spoken_text``) and never the
    un-played tail. ``None`` on the text path (no barge-in there).

    Cancellation: this coroutine runs as a background task and may be
    cancelled by :func:`_cancel_active_tts` when a new user message
    arrives, OR by the full-duplex loop on a confirmed barge-in (issue 0119
    additionally cuts via the pipeline's single ``cancel()``). The cancelling
    path emits the final ``audio_end``; we bubble :class:`asyncio.CancelledError`
    here and do not emit it ourselves. Because the cut lands between/within
    sentences, the last ``on_spoken_progress`` value is exactly the played
    prefix.
    """

    cleaned = clean_for_speech(text)
    sentences = [s for s in text_segmenter.segment(cleaned) if s.strip()]
    if not sentences:
        await websocket.send_json({"type": "audio_end", "msg_id": msg_id})
        return

    tts = _tts_service_provider()
    settings = get_settings()

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
        # PRD 0018 / issue 0126 — bounded preload: a hung snapshot download /
        # model load degrades to an explicit ``audio_error`` + ``audio_end``
        # instead of a ``tts_preparing`` toast that never resolves. (The
        # worker thread itself cannot be killed — it is abandoned; the client
        # signal is the point.)
        preload_timeout_s = settings.TTS_PRELOAD_TIMEOUT_SECONDS
        try:
            if preload_timeout_s > 0:
                await asyncio.wait_for(asyncio.to_thread(tts.preload), preload_timeout_s)
            else:
                await asyncio.to_thread(tts.preload)
        except asyncio.CancelledError:
            raise
        except TimeoutError:
            _logger.error(
                "ws_chat.tts_preload_timeout",
                session_id=session_id,
                msg_id=msg_id,
                timeout_seconds=preload_timeout_s,
            )
            emit_debug(
                category="voice",
                severity="warn",
                source="bob.ws_router._synthesize_and_stream",
                summary=f"Audio erreur: préchargement TTS > {preload_timeout_s:g}s",
                payload={
                    "session_id": session_id,
                    "msg_id": msg_id,
                    "timeout_seconds": preload_timeout_s,
                },
            )
            await websocket.send_json(
                {
                    "type": "audio_error",
                    "msg_id": msg_id,
                    "reason": f"préchargement modèle TTS > {preload_timeout_s:g}s",
                }
            )
            await websocket.send_json({"type": "audio_end", "msg_id": msg_id})
            return
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
    chunks_sent = 0

    async def _send_chunk(chunk: SynthesisChunk) -> None:
        """The pipeline's sink: one PCM block → the chat WebSocket."""

        nonlocal started_audio, chunks_sent, on_first_audio
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
            # Full-duplex loop hook (issue 0100): mark ``t_first_audio_chunk``
            # + flip the turn FSM into ``bob_speaking`` exactly once, just
            # before the very first outbound chunk. ``None`` on the text path.
            if on_first_audio is not None:
                await on_first_audio()
                on_first_audio = None
        await websocket.send_bytes(chunk.pcm16)
        if chunks_sent == 0:
            # PRD 0018 / issue 0117 — ``audio_first_byte``: the turn's first
            # PCM bytes actually LEFT the socket (vs ``tts_first_chunk`` =
            # synthesis ready, placed by the pipeline's producer). The gap
            # between the two is the network/write cost.
            turn_metrics.mark_current("audio_first_byte")
        chunks_sent += 1
        # PRD 0016 / issue 0109 — hand the raw PCM block to the full-duplex
        # loop so it accumulates Bob's ``tts_out`` recording for persistence.
        # ``None`` on the text path (no voice turn is persisted there).
        if on_audio_chunk is not None:
            await on_audio_chunk(chunk.pcm16, chunk.sample_rate)

    async def _on_sentence_drained(index: int) -> None:
        # This sentence's chunks have all left the socket — report the
        # cumulative text Bob has actually played (issue 0101). A barge-in
        # cancel between sentences lands here with the last fully-played
        # prefix already reported. Reconstructed from the cleaned sentences so
        # it matches what TTS spoke (not the raw reply): the
        # committed_spoken_text basis.
        if on_spoken_progress is not None:
            await on_spoken_progress(" ".join(sentences[: index + 1]))

    async def _on_sentence_error(index: int, exc: Exception) -> None:
        # One bad sentence (phonemizer hiccup) never kills the reply — the
        # pipeline already skipped to the next one; surface the first failure
        # to the client as a single ``audio_error``.
        nonlocal error_emitted
        _logger.error(
            "ws_chat.tts_failed",
            session_id=session_id,
            msg_id=msg_id,
            sentence_index=index,
            exc_info=exc,
        )
        if error_emitted:
            return
        emit_debug(
            category="voice",
            severity="warn",
            source="bob.ws_router._synthesize_and_stream",
            summary=f"Audio erreur: {exc or exc.__class__.__name__}",
            payload={
                "session_id": session_id,
                "msg_id": msg_id,
                "sentence_index": index,
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

    async def _on_chunk_batch(summary: ChunkBatchSummary) -> None:
        # PRD 0018 / issue 0121 (supersedes the 0016 Annexe A.2 per-chunk
        # event): surface the outbound PCM as ONE batched voice event per
        # window so the attestation harness can still count audio-out on
        # ``/ws/debug`` (``audio_chunks_gte`` sums ``count``) without one
        # event per ~250 ms block. The raw PCM rides the chat socket and never
        # lands in the ring buffer; no transcript text on this event.
        await emit_event(
            {
                "type": "audio_chunk_batch",
                "msg_id": msg_id,
                "count": summary.count,
                "bytes": summary.pcm_bytes,
                "first_chunk_index": summary.first_chunk_index,
                "sample_rate": summary.sample_rate,
            },
            category="voice",
            severity="debug",
            source="bob.ws_router.audio_chunk_batch",
            summary=f"audio_chunk_batch x{summary.count} ({summary.pcm_bytes} B, msg={msg_id})",
        )

    # PRD 0018 / issue 0121 — the pipelined say-path: sentence N+1 synthesizes
    # while sentence N drains; a bounded queue decouples the two; ONE
    # ``cancel()`` (or cancelling this task) cuts everything at once.
    pipeline = SpeechStreamPipeline(
        synthesize=tts.synthesize_stream,
        send_chunk=_send_chunk,
        on_sentence_drained=_on_sentence_drained,
        on_sentence_error=_on_sentence_error,
        on_chunk_batch=_on_chunk_batch,
        queue_max_chunks=settings.SPEECH_PIPELINE_QUEUE_MAX_CHUNKS,
        batch_window_ms=settings.SPEECH_PIPELINE_BATCH_WINDOW_MS,
    )
    # PRD 0018 / issue 0119 — expose the in-flight pipeline so the barge-in
    # zero-grace path can cut it with the single SYNCHRONOUS ``cancel()``
    # (no further audio chunk reaches the client) the instant the
    # interruption is confirmed, without waiting for this task's unwind.
    session = _sessions.get(session_id)
    if session is not None:
        session["active_speech_pipeline"] = pipeline
    # PRD 0018 / issue 0126 — bounded streaming: a synthesis (or drain) that
    # hangs mid-stream is cut at the budget and surfaced to the client as an
    # explicit ``audio_error`` + ``audio_end`` instead of an ``audio_start``
    # whose audio never finishes (or never starts).
    stream_timeout_s = settings.TTS_STREAM_TIMEOUT_SECONDS
    try:
        async with asyncio.timeout(stream_timeout_s if stream_timeout_s > 0 else None):
            await pipeline.run(sentences)
        await websocket.send_json({"type": "audio_end", "msg_id": msg_id})
        emit_debug(
            category="voice",
            severity="debug",
            source="bob.ws_router._synthesize_and_stream",
            summary="Audio stream terminé",
            payload={"session_id": session_id, "msg_id": msg_id},
        )
    except TimeoutError:
        _logger.error(
            "ws_chat.tts_stream_timeout",
            session_id=session_id,
            msg_id=msg_id,
            timeout_seconds=stream_timeout_s,
        )
        emit_debug(
            category="voice",
            severity="warn",
            source="bob.ws_router._synthesize_and_stream",
            summary=f"Audio erreur: flux TTS > {stream_timeout_s:g}s — coupé",
            payload={
                "session_id": session_id,
                "msg_id": msg_id,
                "timeout_seconds": stream_timeout_s,
                "chunks_sent": chunks_sent,
            },
        )
        with contextlib.suppress(Exception):
            await websocket.send_json(
                {
                    "type": "audio_error",
                    "msg_id": msg_id,
                    "reason": f"flux TTS interrompu après {stream_timeout_s:g}s",
                }
            )
            await websocket.send_json({"type": "audio_end", "msg_id": msg_id})
    except asyncio.CancelledError:
        # The cancelling path emits the final audio_end. Bubble out.
        raise
    finally:
        # De-register only OUR pipeline — a newer stream for this session may
        # already have replaced the slot (interruption + immediate next turn).
        sess = _sessions.get(session_id)
        if sess is not None and sess.get("active_speech_pipeline") is pipeline:
            sess["active_speech_pipeline"] = None


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
