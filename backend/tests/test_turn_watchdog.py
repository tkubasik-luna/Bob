"""Tests for the TurnWatchdog (PRD 0018 / issue 0126).

Four layers, mirroring where the watchdog lives:

1. :class:`bob.turn_watchdog.TurnWatchdog` in isolation — TTFT vs completion
   phases, fast nominal turns, external cancellation, disabled budgets.
2. The voice say-path (:class:`bob.voice_loop.FullDuplexLoop`) with stalling
   fake drivers — ``turn_timeout`` event + verbal fallback + healthy FSM +
   the issue-0117 ``turn_metrics`` summary still emitted.
3. The text path (``/ws/chat``) with a hanging fake orchestrator — fallback
   ``assistant_msg`` delivered instead of eternal silence.
4. The degrade-and-continue guards: summary regeneration, proactive
   synthesis, TTS preload and TTS streaming.

All budgets are tiny REAL timeouts (tens of ms) so the suite stays fast.
"""

from __future__ import annotations

import asyncio
import sqlite3
import struct
import time
from collections.abc import AsyncIterator, Awaitable, Callable, Iterator
from typing import Any, cast

import pytest
from fastapi.testclient import TestClient

from bob import debug_log, turn_metrics, ws_router
from bob import orchestrator as orchestrator_module
from bob.config import Settings, get_settings
from bob.db.migrations_runner import apply_migrations, default_migrations_dir
from bob.jarvis_store import JarvisStore
from bob.llm_client import LLMClient
from bob.main import app
from bob.orchestrator import Orchestrator, OrchestratorResponse
from bob.stt_engine import MIC_FRAME_TAG, FakeSttEngine
from bob.task_store import TaskStore
from bob.tts_service import KokoroTtsService, SynthesisChunk
from bob.turn_fsm import TurnState
from bob.turn_watchdog import (
    TURN_TIMEOUT_FALLBACK_SPEECH,
    TurnTimeoutError,
    TurnWatchdog,
    note_first_token_current,
)
from bob.voice_loop import FullDuplexLoop
from bob.voice_turn import VoiceTurn

_NEVER = 3600.0


@pytest.fixture(autouse=True)
def _clear_buffer() -> None:
    debug_log.clear()
    # Fresh per-test metrics collector (issue 0117) so rolling aggregates and
    # in-flight turn entries never leak across tests.
    turn_metrics.set_default_collector(None)


@pytest.fixture()
def settings_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[Callable[..., None]]:
    """Set env overrides + rebuild the cached :func:`get_settings` instance."""

    def _set(**env: str) -> None:
        for key, value in env.items():
            monkeypatch.setenv(key, value)
        get_settings.cache_clear()

    yield _set
    get_settings.cache_clear()


def _ws_events_of_type(event_type: str) -> list[dict[str, Any]]:
    """All ``ws_event`` bodies of ``event_type`` currently in the ring buffer."""

    out: list[dict[str, Any]] = []
    for event in debug_log.snapshot():
        ws_event = (event.payload or {}).get("ws_event") or {}
        if ws_event.get("type") == event_type:
            out.append(ws_event)
    return out


# --- 1) TurnWatchdog in isolation ---------------------------------------------


async def test_guard_returns_result_on_fast_nominal_turn() -> None:
    watchdog = TurnWatchdog(ttft_timeout_s=5.0, completion_timeout_s=5.0)

    async def _body() -> str:
        return "ok"

    assert await watchdog.guard(_body(), name="t") == "ok"


async def test_guard_ttft_timeout_when_provider_never_answers() -> None:
    """A provider that never starts answering is cut at the TTFT budget."""

    watchdog = TurnWatchdog(ttft_timeout_s=0.05, completion_timeout_s=5.0)
    cancelled = asyncio.Event()

    async def _body() -> None:
        try:
            await asyncio.sleep(_NEVER)
        except asyncio.CancelledError:
            cancelled.set()
            raise

    started = time.monotonic()
    with pytest.raises(TurnTimeoutError) as excinfo:
        await watchdog.guard(_body(), name="t")
    assert excinfo.value.phase == "ttft"
    assert excinfo.value.budget_seconds == 0.05
    # Fired at the TTFT budget, well before the completion budget.
    assert time.monotonic() - started < 2.0
    assert cancelled.is_set()


async def test_guard_streaming_then_stall_cut_at_completion_not_ttft() -> None:
    """First token within TTFT then a stall → the COMPLETION budget cuts."""

    watchdog = TurnWatchdog(ttft_timeout_s=0.05, completion_timeout_s=0.3)

    async def _body() -> None:
        # The provider started answering immediately (resolved through the
        # ContextVar exactly like the orchestrator's first-chunk site)...
        note_first_token_current()
        # ... then stalled forever.
        await asyncio.sleep(_NEVER)

    started = time.monotonic()
    with pytest.raises(TurnTimeoutError) as excinfo:
        await watchdog.guard(_body(), name="t")
    elapsed = time.monotonic() - started
    assert excinfo.value.phase == "completion"
    assert excinfo.value.budget_seconds == 0.3
    # Cut at the completion budget, NOT at TTFT.
    assert elapsed >= 0.25
    assert watchdog.first_token_seen


async def test_guard_body_exception_propagates_unchanged() -> None:
    watchdog = TurnWatchdog(ttft_timeout_s=5.0, completion_timeout_s=5.0)

    async def _body() -> None:
        raise ValueError("boom")

    with pytest.raises(ValueError, match="boom"):
        await watchdog.guard(_body(), name="t")


async def test_guard_external_cancellation_propagates_and_kills_body() -> None:
    """A barge-in style outer cancel stays a CancelledError (never a timeout)."""

    watchdog = TurnWatchdog(ttft_timeout_s=5.0, completion_timeout_s=5.0)
    body_cancelled = asyncio.Event()

    async def _body() -> None:
        try:
            await asyncio.sleep(_NEVER)
        except asyncio.CancelledError:
            body_cancelled.set()
            raise

    guard_task = asyncio.create_task(watchdog.guard(_body(), name="t"))
    await asyncio.sleep(0.01)
    guard_task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await guard_task
    assert body_cancelled.is_set()


async def test_guard_disabled_budgets_never_time_out() -> None:
    watchdog = TurnWatchdog(ttft_timeout_s=0.0, completion_timeout_s=0.0)

    async def _body() -> str:
        await asyncio.sleep(0.05)
        return "slowish but fine"

    assert await watchdog.guard(_body(), name="t") == "slowish but fine"


async def test_note_first_token_current_is_noop_without_watchdog() -> None:
    note_first_token_current()  # must never raise outside a guarded turn


# --- 2) Voice say-path (FullDuplexLoop) -----------------------------------------


def _frame(amplitude: int, *, samples: int = 480) -> bytes:
    return bytes([MIC_FRAME_TAG]) + struct.pack(f"<{samples}h", *([amplitude] * samples))


_LOUD = _frame(8000)
_QUIET = _frame(0)


def _voice_settings(
    *,
    ttft_s: float,
    completion_s: float,
    fallback_s: float = 1.0,
) -> Settings:
    return Settings.model_construct(
        STT_ENGINE="fake",
        STT_SAMPLE_RATE=16_000,
        VAD_SPEECH_RMS=0.02,
        VAD_PAUSE_MS=60,  # 2 frames
        ENDPOINT_SILENCE_MS=120,  # 4 frames
        STT_DEBUG_TEXT_MAX_CHARS=64,
        BACKCHANNEL_MIN_INTERVAL_MS=0,
        VOICE_TURN_TTFT_TIMEOUT_SECONDS=ttft_s,
        VOICE_TURN_COMPLETION_TIMEOUT_SECONDS=completion_s,
        TURN_FALLBACK_TIMEOUT_SECONDS=fallback_s,
    )


class _StallingSayPath:
    """Fake driver: the FIRST call stalls forever; later calls answer instantly.

    ``audio_before_stall`` makes the first call invoke ``on_first_audio``
    before stalling (the "provider streamed then stalled" shape). The fallback
    call (``prepared_reply`` set) records and produces audio like a healthy
    driver.
    """

    def __init__(self, *, audio_before_stall: bool = False) -> None:
        self._audio_before_stall = audio_before_stall
        self.calls: list[str | None] = []  # the prepared_reply of each call

    async def __call__(
        self,
        transcript: str,
        *,
        turn_id: str,
        on_first_audio: Callable[[], Awaitable[None]],
        on_spoken_progress: Callable[[str], Awaitable[None]] | None = None,
        on_audio_chunk: Callable[[bytes, int], Awaitable[None]] | None = None,
        prepared_reply: str | None = None,
    ) -> None:
        self.calls.append(prepared_reply)
        if len(self.calls) == 1:
            if self._audio_before_stall:
                await on_first_audio()
            await asyncio.sleep(_NEVER)
            return
        await on_first_audio()
        if on_spoken_progress is not None:
            await on_spoken_progress(prepared_reply or transcript)


def _voice_loop(say_path: _StallingSayPath, settings: Settings) -> FullDuplexLoop:
    return FullDuplexLoop(
        voice_turn_factory=lambda: VoiceTurn(
            engine=FakeSttEngine(transcript="bonjour le monde", samples_per_word=160),
            session_id="s1",
            settings=settings,
        ),
        say_path=cast(Any, say_path),
        settings=settings,
        session_id="s1",
    )


async def _drive_one_turn(loop: FullDuplexLoop) -> None:
    assert await loop.start() is True
    for _ in range(8):
        await loop.feed_raw_frame(_LOUD)
    for _ in range(8):
        await loop.feed_raw_frame(_QUIET)
    await loop.join()


async def test_voice_ttft_timeout_emits_event_fallback_and_healthy_fsm() -> None:
    """Provider never answers → ``turn_timeout`` (ttft) + verbal fallback + idle FSM."""

    say = _StallingSayPath()
    loop = _voice_loop(say, _voice_settings(ttft_s=0.05, completion_s=5.0))
    await _drive_one_turn(loop)

    timeouts = _ws_events_of_type("turn_timeout")
    assert len(timeouts) == 1
    assert timeouts[0]["phase"] == "ttft"
    assert timeouts[0]["path"] == "voice"
    assert timeouts[0]["budget_seconds"] == 0.05

    # The verbal fallback rode the SAME say-path driver as a prepared reply.
    assert say.calls == [None, TURN_TIMEOUT_FALLBACK_SPEECH]

    # FSM restored to a healthy idle — the next utterance can start a turn.
    assert loop.state is TurnState.IDLE

    # Issue 0117 — the timed-out turn still emitted its turn_metrics summary,
    # carrying the ``turn_timeout`` counter.
    metrics = _ws_events_of_type("turn_metrics")
    assert len(metrics) == 1
    assert metrics[0]["counters"].get("turn_timeout") == 1


async def test_voice_stream_then_stall_cut_at_completion_budget() -> None:
    """First audio within TTFT then a stall → phase ``completion``, not ``ttft``."""

    say = _StallingSayPath(audio_before_stall=True)
    loop = _voice_loop(say, _voice_settings(ttft_s=0.05, completion_s=0.25))
    started = time.monotonic()
    await _drive_one_turn(loop)
    elapsed = time.monotonic() - started

    timeouts = _ws_events_of_type("turn_timeout")
    assert len(timeouts) == 1
    assert timeouts[0]["phase"] == "completion"
    assert timeouts[0]["budget_seconds"] == 0.25
    assert elapsed >= 0.2  # cut at the completion budget, not at TTFT
    assert say.calls == [None, TURN_TIMEOUT_FALLBACK_SPEECH]
    assert loop.state is TurnState.IDLE


async def test_voice_nominal_fast_turn_fires_no_timeout() -> None:
    """A fast turn under generous budgets emits NO ``turn_timeout``."""

    class _FastSayPath(_StallingSayPath):
        async def __call__(self, transcript: str, **kwargs: Any) -> None:
            self.calls.append(kwargs.get("prepared_reply"))
            await kwargs["on_first_audio"]()

    say = _FastSayPath()
    loop = _voice_loop(say, _voice_settings(ttft_s=5.0, completion_s=5.0))
    await _drive_one_turn(loop)

    assert _ws_events_of_type("turn_timeout") == []
    assert say.calls == [None]  # one cold call, no fallback
    assert loop.state is TurnState.IDLE


# --- 3) Text path (/ws/chat) -----------------------------------------------------


class _HangingOrchestrator:
    """Orchestrator double whose turn never returns (cancellable)."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    async def process_user_message(
        self, session_id: str, user_content: str
    ) -> OrchestratorResponse:
        self.calls.append((session_id, user_content))
        await asyncio.sleep(_NEVER)
        return OrchestratorResponse(speech="unreachable")


def test_ws_chat_turn_timeout_delivers_text_fallback(
    clear_jarvis_history: None,
    settings_env: Callable[..., None],
) -> None:
    """A hung text turn → ``turn_timeout`` event + fallback assistant_msg."""

    settings_env(
        TURN_TTFT_TIMEOUT_SECONDS="0.05",
        TURN_COMPLETION_TIMEOUT_SECONDS="1.0",
    )
    fake = _HangingOrchestrator()
    ws_router.set_orchestrator_provider(lambda: cast(Orchestrator, fake))
    try:
        with TestClient(app) as client, client.websocket_connect("/ws/chat") as ws:
            assert ws.receive_json()["type"] == "session"
            ws.send_json({"type": "user_msg", "content": "tu m'entends ?"})

            assert ws.receive_json() == {"type": "thinking", "state": "start"}
            # The ``turn_timeout`` event itself reaches the client first (the
            # unified bus fans it out to the chat socket — the explicit
            # client signal), then the fallback assistant_msg.
            frame = ws.receive_json()
            assert frame["type"] == "turn_timeout"
            assert frame["phase"] == "ttft"
            assistant = ws.receive_json()
            assert assistant["type"] == "assistant_msg"
            assert assistant["speech"] == TURN_TIMEOUT_FALLBACK_SPEECH
            assert assistant["proactive"] is False
            assert ws.receive_json() == {"type": "thinking", "state": "end"}
    finally:
        ws_router.reset_orchestrator_provider()

    assert len(fake.calls) == 1
    timeouts = _ws_events_of_type("turn_timeout")
    assert len(timeouts) == 1
    assert timeouts[0]["phase"] == "ttft"
    assert timeouts[0]["path"] == "text"


# --- 4) Degrade-and-continue guards ----------------------------------------------


class _ScriptableChatClient(LLMClient):
    """Minimal LLMClient double: ``chat`` hangs or returns a canned string."""

    def __init__(self, *, hang: bool = False, reply: str = "ok") -> None:
        self._hang = hang
        self._reply = reply

    async def chat(
        self,
        messages: list[dict[str, Any]],
        schema: dict[str, Any] | None = None,
        session_id: str | None = None,
    ) -> str:
        if self._hang:
            await asyncio.sleep(_NEVER)
        return self._reply

    async def complete(
        self,
        messages: list[dict[str, Any]],
        tools: list[Any] | None = None,
        session_id: str | None = None,
    ) -> Any:  # pragma: no cover — never exercised by these tests
        raise AssertionError("complete() is not expected in these tests")


class _NullScheduler:
    async def enqueue(self, task_id: str) -> None:  # pragma: no cover — unused
        return

    async def resume(self, task_id: str) -> None:  # pragma: no cover — unused
        return

    async def cancel(
        self, task_id: str, *, reason: str = "user_cancelled"
    ) -> None:  # pragma: no cover — unused
        return


def _make_orchestrator(client: LLMClient) -> Orchestrator:
    conn = sqlite3.connect(":memory:")
    apply_migrations(conn, default_migrations_dir())
    return Orchestrator(
        jarvis_client=client,
        jarvis_store=JarvisStore(conn),
        task_store=TaskStore(conn),
        task_scheduler=_NullScheduler(),
        jarvis_prompt="Tu es Jarvis-de-test.",
    )


async def test_hanging_summary_regeneration_degrades_and_continues(
    monkeypatch: pytest.MonkeyPatch,
    settings_env: Callable[..., None],
) -> None:
    """A hung summary regen does NOT stop the turn — degrade-and-continue, logged."""

    settings_env(SUMMARY_REGEN_TIMEOUT_SECONDS="0.05")

    async def _hanging_regen(**kwargs: Any) -> None:
        await asyncio.sleep(_NEVER)

    monkeypatch.setattr(orchestrator_module, "maybe_regenerate_rolling_summary", _hanging_regen)
    orchestrator = _make_orchestrator(_ScriptableChatClient())

    # Must come back within the budget (the wait_for is the regression net:
    # pre-0126 this await hung forever) and swallow the timeout.
    await asyncio.wait_for(orchestrator._maybe_regenerate_summary(), timeout=2.0)

    degrade_events = [
        event
        for event in debug_log.snapshot()
        if event.source == "orchestrator._maybe_regenerate_summary" and event.severity == "warn"
    ]
    assert len(degrade_events) == 1


async def test_hanging_proactive_synthesis_skips_announcement(
    settings_env: Callable[..., None],
) -> None:
    """A hung proactive synthesis returns None (announcement skipped), logged."""

    settings_env(PROACTIVE_SYNTHESIS_TIMEOUT_SECONDS="0.05")
    orchestrator = _make_orchestrator(_ScriptableChatClient(hang=True))

    text = await asyncio.wait_for(
        orchestrator._render_proactive_text("task-1", "annonce le résultat"),
        timeout=2.0,
    )
    assert text is None

    degrade_events = [
        event
        for event in debug_log.snapshot()
        if event.source == "orchestrator._render_proactive_text" and event.severity == "warn"
    ]
    assert len(degrade_events) == 1


async def test_fast_proactive_synthesis_unaffected_by_guard(
    settings_env: Callable[..., None],
) -> None:
    settings_env(PROACTIVE_SYNTHESIS_TIMEOUT_SECONDS="5.0")
    orchestrator = _make_orchestrator(_ScriptableChatClient(reply="Tâche terminée !"))
    assert await orchestrator._render_proactive_text("task-1", "annonce") == "Tâche terminée !"


class _StubWebSocket:
    """Records the say-path's outbound frames (JSON + binary), in order."""

    def __init__(self) -> None:
        self.frames: list[tuple[str, Any]] = []

    async def send_json(self, frame: dict[str, Any]) -> None:
        self.frames.append(("json", frame))

    async def send_bytes(self, data: bytes) -> None:
        self.frames.append(("bytes", data))

    def json_types(self) -> list[str]:
        return [frame["type"] for kind, frame in self.frames if kind == "json"]


class _HangingStreamTts:
    """TTS double whose synthesis stream never yields (hangs)."""

    def is_model_cached(self) -> bool:
        return True

    async def synthesize_stream(
        self,
        text: str,
        *,
        voice: str | None = None,
        speed: float | None = None,
    ) -> AsyncIterator[SynthesisChunk]:
        await asyncio.sleep(_NEVER)
        yield SynthesisChunk(pcm16=b"\x00\x00", sample_rate=24_000)  # pragma: no cover


class _SlowPreloadTts:
    """TTS double whose model preload (download) outlives the budget."""

    def is_model_cached(self) -> bool:
        return False

    def preload(self) -> None:
        time.sleep(0.4)

    async def synthesize_stream(
        self,
        text: str,
        *,
        voice: str | None = None,
        speed: float | None = None,
    ) -> AsyncIterator[SynthesisChunk]:  # pragma: no cover — never reached
        yield SynthesisChunk(pcm16=b"\x00\x00", sample_rate=24_000)


async def test_hanging_tts_stream_signals_client_instead_of_silence(
    settings_env: Callable[..., None],
) -> None:
    """A hung TTS stream → ``audio_error`` + ``audio_end``, not eternal silence."""

    settings_env(TTS_STREAM_TIMEOUT_SECONDS="0.05")
    ws = _StubWebSocket()
    ws_router.set_tts_service_provider(lambda: cast(KokoroTtsService, _HangingStreamTts()))
    try:
        await asyncio.wait_for(
            ws_router._synthesize_and_stream(cast(Any, ws), "s1", "m1", "Bonjour."),
            timeout=2.0,
        )
    finally:
        ws_router.reset_tts_service_provider()

    types = ws.json_types()
    assert "audio_error" in types
    assert types[-1] == "audio_end"
    # No PCM ever left and no audio_start was promised then broken silently.
    assert all(kind != "bytes" for kind, _ in ws.frames)


async def test_hanging_tts_preload_signals_client_instead_of_tts_ready(
    settings_env: Callable[..., None],
) -> None:
    """A hung preload → ``audio_error`` + ``audio_end`` instead of a dangling toast."""

    settings_env(TTS_PRELOAD_TIMEOUT_SECONDS="0.05")
    ws = _StubWebSocket()
    ws_router.set_tts_service_provider(lambda: cast(KokoroTtsService, _SlowPreloadTts()))
    try:
        await asyncio.wait_for(
            ws_router._synthesize_and_stream(cast(Any, ws), "s1", "m1", "Bonjour."),
            timeout=2.0,
        )
    finally:
        ws_router.reset_tts_service_provider()

    types = ws.json_types()
    assert types[0] == "tts_preparing"
    assert "tts_ready" not in types  # the preload never finished
    assert "audio_error" in types
    assert types[-1] == "audio_end"
