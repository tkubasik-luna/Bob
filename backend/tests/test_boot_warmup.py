"""Tests for :mod:`bob.boot_warmup` (PRD 0018 / issue 0129).

External behavior only:

- :class:`BootWarmup` runs its steps as background tasks — starting never
  blocks the caller; a failing step lands in ``errors`` AND produces the
  issue-0124 supervisor debug event; a cancelled step records nothing.
- The step factories drive the engines through their public preload surfaces
  (fakes injected via the existing seams).
- Full-app boots (the real lifespan): the boot yields while a slow fake STT
  engine is still warming — a WS connection succeeds, a ``voice_start``
  mid-warmup emits the existing ``stt_preparing`` / ``stt_ready`` toasts then
  the turn succeeds; a warmup failure surfaces on ``/health`` as
  ``warmup_errors`` (boot continues); the skip-preload setting disables the
  warmup entirely.
"""

from __future__ import annotations

import asyncio
import struct
import threading
import time
from collections.abc import Iterator
from typing import Any, cast

import pytest
from fastapi.testclient import TestClient
from starlette.testclient import WebSocketTestSession

from bob import debug_log, ws_router
from bob import stt_engine as stt_engine_module
from bob import tts_service as tts_service_module
from bob.boot_warmup import (
    BootWarmup,
    WarmupStep,
    llm_roles_warmup_step,
    stt_warmup_step,
    tts_warmup_step,
)
from bob.config import Settings, get_settings
from bob.llm_selection_store import LLMSelection, RoleSelection
from bob.llm_swap import RoleManagerRegistry
from bob.lm_studio_manager import LMStudioManager, RoleLoadResult, host_from_base_url
from bob.main import app
from bob.stt_engine import MIC_FRAME_TAG, FakeSttEngine, SttEngineUnavailableError
from bob.tts_service import FakeTtsService

# --- BootWarmup core ----------------------------------------------------------


@pytest.fixture(autouse=True)
def _clear_debug_buffer() -> None:
    debug_log.clear()


def _supervisor_events() -> list[debug_log.DebugEvent]:
    return [e for e in debug_log.snapshot() if e.source == "bob.task_supervisor"]


async def test_start_returns_before_steps_complete_then_wait_drains() -> None:
    gate = asyncio.Event()
    done: list[str] = []

    async def _slow() -> None:
        await gate.wait()
        done.append("slow")

    warmup = BootWarmup([WarmupStep(name="slow", run=_slow)])
    warmup.start()
    # start() never awaited the step — it is still parked on the gate.
    assert warmup.started
    assert warmup.running
    assert done == []

    gate.set()
    await warmup.wait()
    assert done == ["slow"]
    assert not warmup.running
    assert warmup.errors == {}


async def test_failing_step_records_error_and_emits_supervisor_event() -> None:
    async def _boom() -> None:
        raise RuntimeError("kokoro exploded")

    async def _fine() -> None:
        return None

    warmup = BootWarmup([WarmupStep(name="tts", run=_boom), WarmupStep(name="stt", run=_fine)])
    warmup.start()
    await warmup.wait()

    # The failure is in the health surface AND loudly reported via 0124; the
    # sibling step still completed (one bad step never aborts its peers).
    assert warmup.errors == {"tts": "RuntimeError: kokoro exploded"}
    [event] = _supervisor_events()
    assert event.severity == "error"
    assert event.payload["task_name"] == "boot.warmup.tts"
    assert event.payload["error"] == "RuntimeError: kokoro exploded"
    assert event.payload["step"] == "tts"


async def test_stop_cancels_pending_steps_without_recording_errors() -> None:
    async def _forever() -> None:
        await asyncio.Event().wait()

    warmup = BootWarmup([WarmupStep(name="stt", run=_forever)])
    warmup.start()
    assert warmup.running
    await warmup.stop()
    assert not warmup.running
    # Cancellation is the normal shutdown outcome — not a failure.
    assert warmup.errors == {}
    assert _supervisor_events() == []


async def test_start_is_idempotent() -> None:
    runs: list[int] = []

    async def _once() -> None:
        runs.append(1)

    warmup = BootWarmup([WarmupStep(name="stt", run=_once)])
    warmup.start()
    warmup.start()
    await warmup.wait()
    assert runs == [1]


# --- step factories -----------------------------------------------------------


class _RecordingSttEngine(FakeSttEngine):
    def __init__(self) -> None:
        super().__init__(transcript="")
        self.preload_calls = 0

    def preload(self) -> None:
        self.preload_calls += 1


class _UnavailableSttEngine(FakeSttEngine):
    def preload(self) -> None:
        raise SttEngineUnavailableError("pywhispercpp is not installed")


class _RecordingTts(FakeTtsService):
    def __init__(self) -> None:
        super().__init__()
        self.calls: list[str] = []

    def preload(self) -> None:
        self.calls.append("preload")

    def warmup(self) -> None:
        self.calls.append("warmup")


async def test_stt_step_preloads_the_default_engine() -> None:
    engine = _RecordingSttEngine()
    stt_engine_module.set_default_stt_engine(engine)
    try:
        await stt_warmup_step(Settings()).run()
    finally:
        stt_engine_module.set_default_stt_engine(None)
    assert engine.preload_calls == 1


async def test_stt_step_skips_when_stt_disabled() -> None:
    engine = _RecordingSttEngine()
    stt_engine_module.set_default_stt_engine(engine)
    try:
        await stt_warmup_step(Settings(STT_ENABLED=False)).run()
    finally:
        stt_engine_module.set_default_stt_engine(None)
    assert engine.preload_calls == 0


async def test_stt_step_treats_missing_native_dep_as_skip_not_failure() -> None:
    """The optional pywhispercpp extra being absent must not degrade /health."""

    stt_engine_module.set_default_stt_engine(_UnavailableSttEngine())
    warmup = BootWarmup([stt_warmup_step(Settings())])
    try:
        warmup.start()
        await warmup.wait()
    finally:
        stt_engine_module.set_default_stt_engine(None)
    assert warmup.errors == {}
    assert _supervisor_events() == []


async def test_tts_step_preloads_then_warms(monkeypatch: pytest.MonkeyPatch) -> None:
    tts = _RecordingTts()
    monkeypatch.setattr(tts_service_module, "get_default_tts_service", lambda: tts)
    await tts_warmup_step(Settings()).run()
    assert tts.calls == ["preload", "warmup"]


class _FakeManager:
    """Stands in for :class:`LMStudioManager` — records reconcile() calls."""

    def __init__(self, host: str, results: list[RoleLoadResult]) -> None:
        self.host = host
        self.calls: list[dict[str, tuple[str | None, int | None]]] = []
        self._results = results

    def reconcile(
        self, role_models: dict[str, tuple[str | None, int | None]]
    ) -> list[RoleLoadResult]:
        self.calls.append(dict(role_models))
        return self._results


def _role_selection(roles: dict[str, LLMSelection]) -> RoleSelection:
    base = {
        "jarvis": LLMSelection(provider="lm_studio", lm_model=None),
        "thinker": LLMSelection(provider="lm_studio", lm_model=None),
        "draft": LLMSelection(provider="lm_studio", lm_model=None),
        "subagent": LLMSelection(provider="lm_studio", lm_model=None),
    }
    base.update(roles)
    return RoleSelection(roles=base)


async def test_llm_step_reconciles_lm_studio_roles_grouped_by_host() -> None:
    host = host_from_base_url("http://gpu-box:1234/v1")
    manager = _FakeManager(
        host,
        [
            RoleLoadResult(role="jarvis", model_id="big-model", ready=True),
            RoleLoadResult(role="thinker", model_id="mini-model", ready=False, detail="OOM"),
        ],
    )
    registry = RoleManagerRegistry({host: cast(LMStudioManager, manager)})
    selection = _role_selection(
        {
            "jarvis": LLMSelection(
                provider="lm_studio",
                lm_model="big-model",
                context_length={"big-model": 8192},
                base_url="http://gpu-box:1234/v1",
            ),
            "thinker": LLMSelection(
                provider="lm_studio",
                lm_model="mini-model",
                base_url="http://gpu-box:1234/v1",
            ),
            # No LM Studio model to make resident for these two:
            "draft": LLMSelection(provider="claude_cli", lm_model=None),
            "subagent": LLMSelection(
                provider="lm_studio", lm_model="never-warmed", base_url="http://gpu-box:1234/v1"
            ),
        }
    )

    step = llm_roles_warmup_step(Settings(), selection, registry)
    warmup = BootWarmup([step])
    warmup.start()
    await warmup.wait()

    # One reconcile per host, voice roles only (subagent excluded), with the
    # model-scoped context length threaded through. An offline role (OOM /
    # unreachable) is a loud log, not a health failure.
    assert manager.calls == [{"jarvis": ("big-model", 8192), "thinker": ("mini-model", None)}]
    assert warmup.errors == {}


def _no_manager_expected(host: str) -> LMStudioManager:
    raise AssertionError(f"no manager expected for {host}")


async def test_llm_step_with_no_lm_studio_roles_touches_no_manager() -> None:
    registry = RoleManagerRegistry(factory=_no_manager_expected)
    selection = _role_selection(
        {
            "jarvis": LLMSelection(provider="claude_cli", lm_model=None),
            "thinker": LLMSelection(provider="fake", lm_model=None),
            "draft": LLMSelection(provider="lm_studio", lm_model=None),  # unpinned
        }
    )
    await llm_roles_warmup_step(Settings(), selection, registry).run()


# --- full-app lifespan integration ---------------------------------------------


class _GatedSttEngine(FakeSttEngine):
    """A slow fake: ``preload`` blocks until :attr:`release` is set.

    Mirrors the real engine's contract — ``is_model_cached`` is False until
    the (gated) load completed, and the load is serialized under an internal
    lock so a per-turn lazy preload queues behind the warmup thread exactly
    like the whisper/Kokoro load locks do.
    """

    def __init__(self, transcript: str) -> None:
        super().__init__(transcript=transcript, samples_per_word=160)
        self.release = threading.Event()
        self.preload_calls = 0
        self._loaded = False
        self._load_lock = threading.Lock()

    def is_model_cached(self) -> bool:
        return self._loaded

    def preload(self) -> None:
        self.preload_calls += 1
        with self._load_lock:
            if self._loaded:
                return
            if not self.release.wait(timeout=15.0):
                raise RuntimeError("warmup gate never released")
            self._loaded = True


class _ExplodingTts(FakeTtsService):
    def preload(self) -> None:
        raise RuntimeError("kokoro exploded")


def _mic_frame(n_samples: int = 160) -> bytes:
    return bytes([MIC_FRAME_TAG]) + struct.pack(f"<{n_samples}h", *([0] * n_samples))


def _drain_until(
    ws: WebSocketTestSession, want_type: str, *, budget: int = 200
) -> list[dict[str, Any]]:
    seen: list[dict[str, Any]] = []
    for _ in range(budget):
        frame = ws.receive_json()
        seen.append(frame)
        if frame.get("type") == want_type:
            return seen
    raise AssertionError(f"never saw {want_type!r}; saw {[f.get('type') for f in seen]}")


@pytest.fixture()
def _warmup_app_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Any) -> Iterator[None]:
    """Hermetic full-app boot: warmup ON, fake LLM roles, isolated data dir.

    ``get_settings`` is an ``lru_cache`` — rebuild it around the env overrides
    (and again on teardown so later tests see the restored env). ``MCP_SERVERS``
    is silenced so a developer's real ``.env`` manifest never spawns MCP
    subprocesses inside these boots.
    """

    monkeypatch.setenv("BOB_SKIP_TTS_PRELOAD", "false")
    monkeypatch.setenv("LLM_PROVIDER", "fake")
    monkeypatch.setenv("BOB_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("MCP_SERVERS", "[]")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def test_boot_yields_during_warmup_and_voice_start_gets_preparing_toasts(
    _warmup_app_env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Acceptance 1 + 2: WS connects while warmup runs; a ``voice_start``
    mid-warmup emits ``stt_preparing`` then ``stt_ready`` and the turn lands."""

    engine = _GatedSttEngine(transcript="quel temps à paris")
    stt_engine_module.set_default_stt_engine(engine)
    monkeypatch.setattr(tts_service_module, "get_default_tts_service", FakeTtsService)
    # The say-path that fires after the endpoint resolves its TTS through the
    # ws_router seam (captured at import) — keep it off the real Kokoro too.
    ws_router.set_tts_service_provider(FakeTtsService)
    try:
        with TestClient(app) as client:
            warmup = app.state.boot_warmup
            assert isinstance(warmup, BootWarmup)
            # The lifespan yielded while the STT step is still parked on the
            # gate — the boot did NOT await the warmup.
            assert warmup.started
            assert warmup.running

            with client.websocket_connect("/ws/chat") as ws:
                assert ws.receive_json()["type"] == "session"

                # A turn beating the warmup: the lazy path emits the existing
                # toast, then queues behind the warmup's load.
                ws.send_json({"type": "voice_start", "window": "new", "ts_client": 0})
                seen = _drain_until(ws, "stt_preparing")
                assert seen[-1]["type"] == "stt_preparing"

                # Let the (shared) load finish: warmup completes, the turn's
                # queued preload sees the loaded model and the session opens.
                engine.release.set()
                _drain_until(ws, "stt_ready")

                for _ in range(8):
                    ws.send_bytes(_mic_frame(160))
                ws.send_json({"type": "voice_stop", "ts_client": 1})
                seen = _drain_until(ws, "stt_final")
                finals = [f for f in seen if f.get("type") == "stt_final"]
                assert finals[0]["text"] == "quel temps à paris"

            # Both the warmup AND the turn went through the gated load.
            assert engine.preload_calls >= 2
            response = client.get("/health")
            assert response.json() == {"status": "ok"}
    finally:
        engine.release.set()
        stt_engine_module.set_default_stt_engine(None)
        ws_router.reset_tts_service_provider()


def test_warmup_failure_is_loud_and_visible_on_health_boot_continues(
    _warmup_app_env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Acceptance 4: a raising engine degrades /health; the app still serves."""

    stt_engine_module.set_default_stt_engine(FakeSttEngine())
    monkeypatch.setattr(tts_service_module, "get_default_tts_service", _ExplodingTts)
    try:
        with TestClient(app) as client:
            # The failure happens in a background task — poll until recorded.
            deadline = time.monotonic() + 5.0
            body: dict[str, Any] = {}
            while time.monotonic() < deadline:
                body = client.get("/health").json()
                if body.get("status") == "degraded":
                    break
                time.sleep(0.02)
            assert body == {
                "status": "degraded",
                "warmup_errors": {"tts": "RuntimeError: kokoro exploded"},
            }
            # Loud: the 0124 supervisor reported the failed warmup task.
            failed = [
                e for e in _supervisor_events() if e.payload.get("task_name") == "boot.warmup.tts"
            ]
            assert failed and failed[0].severity == "error"
            # Boot continued — the WS path is fully alive.
            with client.websocket_connect("/ws/chat") as ws:
                assert ws.receive_json()["type"] == "session"
    finally:
        stt_engine_module.set_default_stt_engine(None)


def test_skip_preload_setting_disables_the_warmup(
    _warmup_app_env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Acceptance 5: ``BOB_SKIP_TTS_PRELOAD`` keeps the warmup a no-op."""

    monkeypatch.setenv("BOB_SKIP_TTS_PRELOAD", "true")
    get_settings.cache_clear()
    engine = _RecordingSttEngine()
    tts = _RecordingTts()
    stt_engine_module.set_default_stt_engine(engine)
    monkeypatch.setattr(tts_service_module, "get_default_tts_service", lambda: tts)
    try:
        with TestClient(app) as client:
            warmup = app.state.boot_warmup
            assert isinstance(warmup, BootWarmup)
            assert warmup.started is False
            assert client.get("/health").json() == {"status": "ok"}
        assert engine.preload_calls == 0
        assert tts.calls == []
    finally:
        stt_engine_module.set_default_stt_engine(None)
