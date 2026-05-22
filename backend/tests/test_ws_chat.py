"""WS plumbing tests for /ws/chat — orchestrator wired via DI seam.

These tests exercise the WebSocket app via :class:`fastapi.testclient.TestClient`
inside a ``with`` block so the FastAPI lifespan runs and primes the
:class:`bob.jarvis_store.JarvisStore` singleton (backed by the test SQLite
file under ``BOB_DATA_DIR``, set up in :mod:`conftest`).
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Iterator
from typing import cast

import pytest
from fastapi.testclient import TestClient

from bob import jarvis_store as jarvis_store_module
from bob import task_store as task_store_module
from bob import ws_events, ws_router
from bob.main import app
from bob.orchestrator import Orchestrator, OrchestratorResponse
from bob.tts_service import KokoroTtsService, SynthesisChunk
from bob.ui_registry import ComponentDescriptor
from bob.ws_router import _sessions


class _FakeOrchestrator:
    """Stand-in for Orchestrator that records calls and returns a canned reply."""

    def __init__(self, response: OrchestratorResponse) -> None:
        self._response = response
        self.calls: list[tuple[str, str]] = []

    async def process_user_message(
        self, session_id: str, user_content: str
    ) -> OrchestratorResponse:
        self.calls.append((session_id, user_content))
        store = jarvis_store_module.get_default_store()
        store.append("user", user_content)
        store.append("assistant", self._response.speech)
        return self._response


@pytest.fixture()
def fake_chat_service(clear_jarvis_history: None) -> Iterator[_FakeOrchestrator]:
    response = OrchestratorResponse(
        speech="echo: hi",
        ui=[ComponentDescriptor(component="Markdown", props={"content": "**bold**"})],
    )
    fake = _FakeOrchestrator(response)
    ws_router.set_orchestrator_provider(lambda: cast(Orchestrator, fake))
    try:
        yield fake
    finally:
        ws_router.reset_orchestrator_provider()


def test_ws_chat_full_round_trip(fake_chat_service: _FakeOrchestrator) -> None:
    with TestClient(app) as client, client.websocket_connect("/ws/chat") as ws:
        session_frame = ws.receive_json()
        assert session_frame["type"] == "session"
        session_id = session_frame["session_id"]
        assert isinstance(session_id, str) and len(session_id) == 32
        assert session_id in _sessions

        ws.send_json({"type": "user_msg", "content": "hi"})

        thinking_start = ws.receive_json()
        assert thinking_start == {"type": "thinking", "state": "start"}

        assistant = ws.receive_json()
        assert assistant["type"] == "assistant_msg"
        assert isinstance(assistant["msg_id"], str) and len(assistant["msg_id"]) == 32
        assert assistant["speech"] == "echo: hi"
        assert assistant["ui"] == [{"component": "Markdown", "props": {"content": "**bold**"}}]
        # Standard turn → proactive must be ``False``.
        assert assistant["proactive"] is False

        thinking_end = ws.receive_json()
        assert thinking_end == {"type": "thinking", "state": "end"}

    assert session_id not in _sessions
    assert fake_chat_service.calls == [(session_id, "hi")]


def test_ws_chat_replays_history_on_connect(clear_jarvis_history: None) -> None:
    """A fresh WS connection re-emits previously persisted Jarvis messages."""

    with TestClient(app) as client:
        store = jarvis_store_module.get_default_store()
        store.append("user", "previous question")
        store.append("assistant", "previous answer")

        with client.websocket_connect("/ws/chat") as ws:
            session = ws.receive_json()
            assert session["type"] == "session"

            replay_user = ws.receive_json()
            assert replay_user["type"] == "user_msg"
            assert replay_user["content"] == "previous question"
            assert replay_user["replayed"] is True

            replay_assistant = ws.receive_json()
            assert replay_assistant["type"] == "assistant_msg"
            assert replay_assistant["speech"] == "previous answer"
            assert replay_assistant["ui"] == []
            assert replay_assistant["replayed"] is True


def test_ws_chat_rejects_unknown_type(fake_chat_service: _FakeOrchestrator) -> None:
    with TestClient(app) as client, client.websocket_connect("/ws/chat") as ws:
        ws.receive_json()  # session frame
        ws.send_json({"type": "nope"})
        err = ws.receive_json()
        assert err["type"] == "error"
        assert err["code"] == "bad_type"
    assert fake_chat_service.calls == []


class _SlowFakeTts:
    """TTS double whose ``synthesize_stream`` blocks until released or cancelled."""

    def __init__(self) -> None:
        self.release = asyncio.Event()
        self.entered = asyncio.Event()
        self.cancelled = False

    async def synthesize_stream(
        self,
        text: str,
        *,
        voice: str | None = None,
        speed: float | None = None,
    ) -> AsyncIterator[SynthesisChunk]:
        self.entered.set()
        try:
            await self.release.wait()
        except asyncio.CancelledError:
            self.cancelled = True
            raise
        yield SynthesisChunk(pcm16=b"\x00\x00\x00\x00", sample_rate=24_000)


def test_ws_chat_interrupts_in_flight_tts(fake_chat_service: _FakeOrchestrator) -> None:
    """A new user_msg must cancel a previous turn's TTS and emit audio_end."""

    slow_tts = _SlowFakeTts()
    ws_router.set_tts_service_provider(lambda: cast(KokoroTtsService, slow_tts))
    try:
        with TestClient(app) as client, client.websocket_connect("/ws/chat") as ws:
            ws.receive_json()  # session
            ws.send_json({"type": "user_msg", "content": "hi", "voice": True})
            assert ws.receive_json()["type"] == "thinking"
            first_assistant = ws.receive_json()
            assert first_assistant["type"] == "assistant_msg"
            first_msg_id = first_assistant["msg_id"]
            assert ws.receive_json() == {"type": "thinking", "state": "end"}

            ws.send_json({"type": "user_msg", "content": "stop"})

            frame = ws.receive_json()
            assert frame == {"type": "audio_end", "msg_id": first_msg_id}

            assert ws.receive_json() == {"type": "thinking", "state": "start"}
            second = ws.receive_json()
            assert second["type"] == "assistant_msg"
            assert second["msg_id"] != first_msg_id
            assert ws.receive_json() == {"type": "thinking", "state": "end"}

        assert slow_tts.cancelled is True
    finally:
        ws_router.reset_tts_service_provider()


def test_ws_chat_interrupt_cancels_even_when_voice_off(
    fake_chat_service: _FakeOrchestrator,
) -> None:
    """Voice-off second message still cancels a voice-on first message's TTS."""

    slow_tts = _SlowFakeTts()
    ws_router.set_tts_service_provider(lambda: cast(KokoroTtsService, slow_tts))
    try:
        with TestClient(app) as client, client.websocket_connect("/ws/chat") as ws:
            ws.receive_json()  # session
            ws.send_json({"type": "user_msg", "content": "hi", "voice": True})
            assert ws.receive_json()["type"] == "thinking"
            first_assistant = ws.receive_json()
            first_msg_id = first_assistant["msg_id"]
            assert ws.receive_json() == {"type": "thinking", "state": "end"}

            ws.send_json({"type": "user_msg", "content": "silence"})
            assert ws.receive_json() == {"type": "audio_end", "msg_id": first_msg_id}
            assert ws.receive_json() == {"type": "thinking", "state": "start"}
            second = ws.receive_json()
            assert second["type"] == "assistant_msg"
            assert ws.receive_json() == {"type": "thinking", "state": "end"}

        assert slow_tts.cancelled is True
    finally:
        ws_router.reset_tts_service_provider()


def test_ws_chat_rejects_bad_content(fake_chat_service: _FakeOrchestrator) -> None:
    with TestClient(app) as client, client.websocket_connect("/ws/chat") as ws:
        ws.receive_json()
        ws.send_json({"type": "user_msg", "content": 42})
        err = ws.receive_json()
        assert err["type"] == "error"
        assert err["code"] == "bad_content"
    assert fake_chat_service.calls == []


# ---------------------------------------------------------------------------
# Task events: emitted on the live connection + replayed on reconnect.
# ---------------------------------------------------------------------------


class _SpawningOrchestrator:
    """Orchestrator double that creates a task and emits the matching events.

    Mirrors the real :meth:`Orchestrator._dispatch_spawns` flow without going
    through the LLM: useful to verify the WS handler installs an emitter that
    routes events back to the connected client.
    """

    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    async def process_user_message(
        self, session_id: str, user_content: str
    ) -> OrchestratorResponse:
        self.calls.append((session_id, user_content))
        store = task_store_module.get_default_store()
        task_id = store.create_task(title="Drafts", goal="Draft 3 emails")
        created = store.get_task(task_id)
        await ws_events.emit(
            {
                "type": "task_created",
                "task_id": task_id,
                "title": created.title,
                "goal": created.goal,
                "state": created.state,
                "created_at": created.created_at,
            }
        )
        store.update_state(task_id, "running")
        running = store.get_task(task_id)
        await ws_events.emit(
            {
                "type": "task_updated",
                "task_id": task_id,
                "state": running.state,
                "needs_attention": running.needs_attention,
                "updated_at": running.updated_at,
            }
        )
        return OrchestratorResponse(speech="ok", ui=[], spawned_task_ids=[task_id])


def test_ws_chat_emits_task_events_on_spawn(clear_jarvis_history: None) -> None:
    """A user_msg that triggers a spawn must surface task_created+task_updated."""

    fake = _SpawningOrchestrator()
    ws_router.set_orchestrator_provider(lambda: cast(Orchestrator, fake))
    try:
        with TestClient(app) as client, client.websocket_connect("/ws/chat") as ws:
            session = ws.receive_json()
            assert session["type"] == "session"

            ws.send_json({"type": "user_msg", "content": "spawn please"})
            assert ws.receive_json() == {"type": "thinking", "state": "start"}

            created = ws.receive_json()
            assert created["type"] == "task_created"
            assert created["title"] == "Drafts"
            assert created["state"] == "pending"
            task_id = created["task_id"]

            updated = ws.receive_json()
            assert updated["type"] == "task_updated"
            assert updated["task_id"] == task_id
            assert updated["state"] == "running"

            assistant = ws.receive_json()
            assert assistant["type"] == "assistant_msg"
            assert ws.receive_json() == {"type": "thinking", "state": "end"}
    finally:
        ws_router.reset_orchestrator_provider()


class _ProactiveOrchestrator:
    """Orchestrator double that emits a proactive ``assistant_msg`` ad hoc.

    Simulates the slice #0021 path where the ProactivityHandler triggers a
    Jarvis paraphrase pushing a message back through the WS emitter
    *without* the user having spoken first. The handler runs in the bus'
    background task — in this test we drive the emit ourselves to assert
    the WS layer forwards the proactive flag faithfully.
    """

    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    async def process_user_message(
        self, session_id: str, user_content: str
    ) -> OrchestratorResponse:
        self.calls.append((session_id, user_content))
        store = jarvis_store_module.get_default_store()
        store.append("user", user_content)
        # Simulate a sub-agent emitting ask_user mid-turn → orchestrator
        # pushes a proactive assistant_msg through the ws_events emitter.
        await ws_events.emit(
            {
                "type": "assistant_msg",
                "msg_id": "aaaaaaaa" * 4,
                "speech": "Tu veux un ton plutôt formel ou amical ?",
                "ui": [],
                "proactive": True,
            }
        )
        store.append("assistant", "ok")
        return OrchestratorResponse(speech="ok", ui=[], spawned_task_ids=[])


def test_ws_chat_forwards_proactive_assistant_msg(clear_jarvis_history: None) -> None:
    """An emit({type: assistant_msg, proactive: True}) lands intact on the client."""

    fake = _ProactiveOrchestrator()
    ws_router.set_orchestrator_provider(lambda: cast(Orchestrator, fake))
    try:
        with TestClient(app) as client, client.websocket_connect("/ws/chat") as ws:
            assert ws.receive_json()["type"] == "session"
            ws.send_json({"type": "user_msg", "content": "Draft un email"})
            assert ws.receive_json() == {"type": "thinking", "state": "start"}

            proactive = ws.receive_json()
            assert proactive["type"] == "assistant_msg"
            assert proactive["proactive"] is True
            assert proactive["speech"] == "Tu veux un ton plutôt formel ou amical ?"

            final = ws.receive_json()
            assert final["type"] == "assistant_msg"
            assert final["proactive"] is False
            assert final["speech"] == "ok"

            assert ws.receive_json() == {"type": "thinking", "state": "end"}
    finally:
        ws_router.reset_orchestrator_provider()


def test_ws_chat_replays_active_tasks_on_connect(clear_jarvis_history: None) -> None:
    """Pre-existing tasks in the store are pushed as task_created on a fresh WS."""

    with TestClient(app) as client:
        store = task_store_module.get_default_store()
        pending_id = store.create_task(title="Pending one", goal="P")
        running_id = store.create_task(title="Running one", goal="R")
        store.update_state(running_id, "running")
        done_id = store.create_task(title="Done one", goal="D")
        store.update_state(done_id, "running")
        store.set_result(done_id, "result text")
        store.update_state(done_id, "done")

        with client.websocket_connect("/ws/chat") as ws:
            assert ws.receive_json()["type"] == "session"

            # task_created in creation order.
            evt1 = ws.receive_json()
            assert evt1["type"] == "task_created"
            assert evt1["task_id"] == pending_id
            assert evt1["state"] == "pending"
            assert evt1["replayed"] is True

            evt2 = ws.receive_json()
            assert evt2["type"] == "task_created"
            assert evt2["task_id"] == running_id
            assert evt2["state"] == "running"
            assert evt2["replayed"] is True

            evt3 = ws.receive_json()
            assert evt3["type"] == "task_created"
            assert evt3["task_id"] == done_id
            assert evt3["state"] == "done"
            assert evt3["replayed"] is True

            # The done task also has a result, so task_result is replayed too.
            evt4 = ws.receive_json()
            assert evt4 == {
                "type": "task_result",
                "task_id": done_id,
                "result": "result text",
                "replayed": True,
            }


# ---------------------------------------------------------------------------
# Slice #0024 — dismiss_task + request_task_messages WS client→server events.
# ---------------------------------------------------------------------------


def test_ws_chat_dismiss_task_hides_from_replay(clear_jarvis_history: None) -> None:
    """Dismissed tasks are not re-emitted on a fresh connection."""

    with TestClient(app) as client:
        store = task_store_module.get_default_store()
        keep_id = store.create_task(title="Keep me", goal="K")
        dismiss_id = store.create_task(title="Hide me", goal="H")
        store.update_state(dismiss_id, "running")
        store.set_result(dismiss_id, "done text")
        store.update_state(dismiss_id, "done")

        with client.websocket_connect("/ws/chat") as ws:
            assert ws.receive_json()["type"] == "session"
            # Drain initial replay (both tasks visible).
            ws.receive_json()  # task_created keep
            ws.receive_json()  # task_created dismiss
            ws.receive_json()  # task_result dismiss

            ws.send_json({"type": "dismiss_task", "task_id": dismiss_id})
            # Backend does not echo a confirmation for dismiss; assert by reconnecting.

        # Reconnect: dismissed task must NOT be replayed.
        with client.websocket_connect("/ws/chat") as ws2:
            assert ws2.receive_json()["type"] == "session"
            evt = ws2.receive_json()
            assert evt["type"] == "task_created"
            assert evt["task_id"] == keep_id

            # No further task events expected. We assert by sending a quick
            # request_task_messages for the dismissed task and verifying the
            # backend still has the row (dismissed != deleted).
            ws2.send_json({"type": "request_task_messages", "task_id": dismiss_id})
            snapshot = ws2.receive_json()
            assert snapshot["type"] == "task_messages_snapshot"
            assert snapshot["task_id"] == dismiss_id

        # The row is still in SQLite; the flag persists.
        assert store.get_task(dismiss_id).dismissed is True


def test_ws_chat_dismiss_task_rejects_bad_payload(clear_jarvis_history: None) -> None:
    """Missing / non-string task_id surfaces a ``bad_dismiss`` error code."""

    with TestClient(app) as client, client.websocket_connect("/ws/chat") as ws:
        assert ws.receive_json()["type"] == "session"
        ws.send_json({"type": "dismiss_task", "task_id": 42})
        err = ws.receive_json()
        assert err["type"] == "error"
        assert err["code"] == "bad_dismiss"


def test_ws_chat_dismiss_task_unknown_id_emits_error(clear_jarvis_history: None) -> None:
    """An unknown task id is reported as ``unknown_task`` without crashing the WS."""

    with TestClient(app) as client, client.websocket_connect("/ws/chat") as ws:
        assert ws.receive_json()["type"] == "session"
        ws.send_json({"type": "dismiss_task", "task_id": "nope"})
        err = ws.receive_json()
        assert err["type"] == "error"
        assert err["code"] == "unknown_task"


def test_ws_chat_request_task_messages_returns_snapshot(
    clear_jarvis_history: None,
) -> None:
    """The drawer can fetch the full transcript via a snapshot reply."""

    with TestClient(app) as client:
        store = task_store_module.get_default_store()
        task_id = store.create_task(title="T", goal="g")
        store.append_message(task_id, role="user", content="hello")
        store.append_message(task_id, role="assistant", content="hi", action="progress")

        with client.websocket_connect("/ws/chat") as ws:
            assert ws.receive_json()["type"] == "session"
            ws.receive_json()  # task_created replay

            ws.send_json({"type": "request_task_messages", "task_id": task_id})
            snapshot = ws.receive_json()
            assert snapshot["type"] == "task_messages_snapshot"
            assert snapshot["task_id"] == task_id
            assert [m["content"] for m in snapshot["messages"]] == ["hello", "hi"]
            assert [m["role"] for m in snapshot["messages"]] == ["user", "assistant"]
            assert [m["action"] for m in snapshot["messages"]] == [None, "progress"]


def test_ws_chat_request_task_messages_unknown_id_emits_error(
    clear_jarvis_history: None,
) -> None:
    with TestClient(app) as client, client.websocket_connect("/ws/chat") as ws:
        assert ws.receive_json()["type"] == "session"
        ws.send_json({"type": "request_task_messages", "task_id": "nope"})
        err = ws.receive_json()
        assert err["type"] == "error"
        assert err["code"] == "unknown_task"


def test_ws_chat_request_task_messages_rejects_bad_payload(
    clear_jarvis_history: None,
) -> None:
    with TestClient(app) as client, client.websocket_connect("/ws/chat") as ws:
        assert ws.receive_json()["type"] == "session"
        ws.send_json({"type": "request_task_messages", "task_id": 7})
        err = ws.receive_json()
        assert err["type"] == "error"
        assert err["code"] == "bad_request_messages"


# ---------------------------------------------------------------------------
# Slice #0023 — cancel_task WS client→server event.
# ---------------------------------------------------------------------------


def test_ws_chat_cancel_task_routes_to_scheduler(clear_jarvis_history: None) -> None:
    """A ``cancel_task`` WS event must reach ``TaskScheduler.cancel``.

    We swap the singleton scheduler for a recording fake so this test
    asserts WS dispatch in isolation from the asyncio cancellation logic
    (covered in :mod:`test_task_scheduler`).
    """

    from bob import task_scheduler as task_scheduler_module

    recorded: list[tuple[str, str]] = []

    class _FakeScheduler:
        async def cancel(self, task_id: str, *, reason: str = "user_cancelled") -> None:
            recorded.append((task_id, reason))

        async def enqueue(self, task_id: str) -> None:  # pragma: no cover — unused
            return None

        async def resume(self, task_id: str) -> None:  # pragma: no cover — unused
            return None

    with TestClient(app) as client:
        # The lifespan primed a real scheduler; install our fake on top.
        previous = task_scheduler_module._DEFAULT_SCHEDULER
        task_scheduler_module.set_default_scheduler(cast(object, _FakeScheduler()))  # type: ignore[arg-type]
        try:
            with client.websocket_connect("/ws/chat") as ws:
                assert ws.receive_json()["type"] == "session"
                ws.send_json({"type": "cancel_task", "task_id": "abc"})
                # No echo expected — the backend just calls scheduler.cancel.
                # Send a follow-up to flush + assert no error came back.
                ws.send_json({"type": "request_task_messages", "task_id": "abc"})
                err = ws.receive_json()
                # request_task_messages will fail with unknown_task since
                # we never created the row — that's fine, we just needed
                # something to round-trip the socket.
                assert err["type"] == "error"
                assert err["code"] == "unknown_task"
        finally:
            task_scheduler_module.set_default_scheduler(previous)

    assert recorded == [("abc", "user_cancelled")]


def test_ws_chat_cancel_task_rejects_bad_payload(clear_jarvis_history: None) -> None:
    """Non-string / empty task_id surfaces a ``bad_cancel`` error code."""

    with TestClient(app) as client, client.websocket_connect("/ws/chat") as ws:
        assert ws.receive_json()["type"] == "session"
        ws.send_json({"type": "cancel_task", "task_id": 42})
        err = ws.receive_json()
        assert err["type"] == "error"
        assert err["code"] == "bad_cancel"


def test_ws_chat_cancel_task_empty_id_rejected(clear_jarvis_history: None) -> None:
    """Empty-string task_id is also rejected (must be non-empty)."""

    with TestClient(app) as client, client.websocket_connect("/ws/chat") as ws:
        assert ws.receive_json()["type"] == "session"
        ws.send_json({"type": "cancel_task", "task_id": ""})
        err = ws.receive_json()
        assert err["type"] == "error"
        assert err["code"] == "bad_cancel"


# ---------------------------------------------------------------------------
# Slice #0025 — client_typing WS event.
# ---------------------------------------------------------------------------


class _TypingRecordingOrchestrator:
    """Orchestrator double recording ``set_user_typing`` calls."""

    def __init__(self) -> None:
        self.typing_calls: list[bool] = []

    async def process_user_message(
        self, session_id: str, user_content: str
    ) -> OrchestratorResponse:
        return OrchestratorResponse(speech="ok", ui=[])

    def set_user_typing(self, value: bool) -> None:
        self.typing_calls.append(value)


def test_ws_chat_client_typing_updates_orchestrator(clear_jarvis_history: None) -> None:
    """A ``client_typing`` event must call ``Orchestrator.set_user_typing``."""

    fake = _TypingRecordingOrchestrator()
    ws_router.set_orchestrator_provider(lambda: cast(Orchestrator, fake))
    try:
        with TestClient(app) as client, client.websocket_connect("/ws/chat") as ws:
            assert ws.receive_json()["type"] == "session"
            ws.send_json({"type": "client_typing", "typing": True})
            ws.send_json({"type": "client_typing", "typing": False})
            # No echo — flush via a round-trip on an unknown task that
            # returns an error frame.
            ws.send_json({"type": "request_task_messages", "task_id": "nope"})
            err = ws.receive_json()
            assert err["type"] == "error"
            assert err["code"] == "unknown_task"
    finally:
        ws_router.reset_orchestrator_provider()

    assert fake.typing_calls == [True, False]


def test_ws_chat_client_typing_rejects_bad_payload(clear_jarvis_history: None) -> None:
    """A non-bool ``typing`` field must surface a ``bad_typing`` error code."""

    fake = _TypingRecordingOrchestrator()
    ws_router.set_orchestrator_provider(lambda: cast(Orchestrator, fake))
    try:
        with TestClient(app) as client, client.websocket_connect("/ws/chat") as ws:
            assert ws.receive_json()["type"] == "session"
            ws.send_json({"type": "client_typing", "typing": "yes"})
            err = ws.receive_json()
            assert err["type"] == "error"
            assert err["code"] == "bad_typing"
    finally:
        ws_router.reset_orchestrator_provider()

    assert fake.typing_calls == []


def test_ws_chat_client_typing_missing_field_rejected(clear_jarvis_history: None) -> None:
    """Missing ``typing`` field must also fail with ``bad_typing``."""

    fake = _TypingRecordingOrchestrator()
    ws_router.set_orchestrator_provider(lambda: cast(Orchestrator, fake))
    try:
        with TestClient(app) as client, client.websocket_connect("/ws/chat") as ws:
            assert ws.receive_json()["type"] == "session"
            ws.send_json({"type": "client_typing"})
            err = ws.receive_json()
            assert err["type"] == "error"
            assert err["code"] == "bad_typing"
    finally:
        ws_router.reset_orchestrator_provider()

    assert fake.typing_calls == []
