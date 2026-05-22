"""Error-path WS tests for /ws/chat.

Each test injects a fake Orchestrator whose ``process_user_message`` raises a
specific exception, then asserts the client receives the expected error
frame plus the closing ``thinking end``.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import cast

import pytest
from fastapi.testclient import TestClient

from bob import ws_router
from bob.main import app
from bob.orchestrator import Orchestrator, OrchestratorResponse


class _RaisingOrchestrator:
    def __init__(self, exc: BaseException) -> None:
        self._exc = exc

    async def process_user_message(
        self, session_id: str, user_content: str
    ) -> OrchestratorResponse:
        raise self._exc


def _install(exc: BaseException) -> Iterator[None]:
    fake = _RaisingOrchestrator(exc)
    ws_router.set_orchestrator_provider(lambda: cast(Orchestrator, fake))
    try:
        yield
    finally:
        ws_router.reset_orchestrator_provider()


@pytest.fixture()
def timeout_service() -> Iterator[None]:

    yield from _install(TimeoutError())


@pytest.fixture()
def unreachable_service() -> Iterator[None]:
    yield from _install(ConnectionError("nope"))


@pytest.fixture()
def boom_service() -> Iterator[None]:
    yield from _install(RuntimeError("boom"))


def _send_and_collect(client: TestClient) -> list[dict[str, object]]:
    with client.websocket_connect("/ws/chat") as ws:
        ws.receive_json()  # session frame
        ws.send_json({"type": "user_msg", "content": "hi"})
        frames = [ws.receive_json() for _ in range(3)]
    return frames


def test_ws_timeout_emits_llm_timeout(timeout_service: None) -> None:
    frames = _send_and_collect(TestClient(app))
    assert frames[0] == {"type": "thinking", "state": "start"}
    assert frames[1] == {
        "type": "error",
        "code": "LLM_TIMEOUT",
        "message": "Timeout LLM",
    }
    assert frames[2] == {"type": "thinking", "state": "end"}


def test_ws_connection_error_emits_unreachable(unreachable_service: None) -> None:
    frames = _send_and_collect(TestClient(app))
    assert frames[0] == {"type": "thinking", "state": "start"}
    assert frames[1] == {
        "type": "error",
        "code": "LLM_UNREACHABLE",
        "message": "LLM provider injoignable",
    }
    assert frames[2] == {"type": "thinking", "state": "end"}


def test_ws_internal_error_does_not_leak_message(boom_service: None) -> None:
    frames = _send_and_collect(TestClient(app))
    assert frames[0] == {"type": "thinking", "state": "start"}
    assert frames[1] == {
        "type": "error",
        "code": "INTERNAL",
        "message": "Erreur interne",
    }
    assert frames[2] == {"type": "thinking", "state": "end"}
    # Critically: the raw exception message must not leak to the client.
    assert "boom" not in str(frames[1])
