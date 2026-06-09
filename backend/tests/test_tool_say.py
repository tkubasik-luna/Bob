"""Behavior-preservation tests for the unified ``say`` tool (v1, issue 0047).

The dispatcher-level happy path is covered in ``test_tool_dispatcher``;
here we pin the side-effect contract of the ``say`` handler itself:

- validates ``speech`` (non-empty after strip),
- persists the assistant turn to :class:`bob.jarvis_store.JarvisStore`,
- returns ``ToolHandlerOutcome(status="ok", speech=…, ui=…)`` so the
  orchestrator can lift the payload into :class:`OrchestratorResponse`,
- does NOT emit ``assistant_msg`` via the WS emitter (the WS router
  emits exactly one frame after ``process_user_message`` returns —
  double-emission would trigger double-TTS in voice mode).

Validation drift between the JSON schema declared for the LLM and the
Pydantic model is caught by the parity test in
``test_tool_registry::test_default_registry_required_field_parity``.
"""

from __future__ import annotations

import sqlite3
from typing import Any

import pytest

from bob.db.migrations_runner import apply_migrations, default_migrations_dir
from bob.jarvis_store import JarvisStore
from bob.llm.types import ToolCall
from bob.task_store import TaskStore
from bob.tools.definitions.say import build_say_tool
from bob.tools.dispatcher import ToolDispatcher, ToolHandlerContext
from bob.tools.registry import ToolRegistry


class _StubScheduler:
    async def enqueue(self, task_id: str) -> None:
        raise AssertionError("enqueue not expected on say path")

    async def resume(self, task_id: str) -> None:
        raise AssertionError("resume not expected on say path")

    async def cancel(self, task_id: str, *, reason: str = "user_cancelled") -> None:
        raise AssertionError("cancel not expected on say path")


def _make_say_dispatcher() -> tuple[ToolDispatcher, JarvisStore, list[dict[str, Any]]]:
    """Build a dispatcher pre-wired with the ``say`` tool + a fresh store."""

    conn = sqlite3.connect(":memory:")
    apply_migrations(conn, default_migrations_dir())
    jarvis_store = JarvisStore(conn)
    task_store = TaskStore(conn)
    emitted: list[dict[str, Any]] = []

    async def _emit(event: dict[str, Any]) -> None:
        emitted.append(event)

    dispatcher = ToolDispatcher(
        registry=ToolRegistry([build_say_tool()]),
        context=ToolHandlerContext(
            task_store=task_store,
            task_scheduler=_StubScheduler(),
            ws_emit=_emit,
            jarvis_store=jarvis_store,
        ),
    )
    return dispatcher, jarvis_store, emitted


@pytest.mark.asyncio
async def test_say_persists_assistant_turn_and_threads_speech() -> None:
    """A successful ``say`` call persists the reply and returns ``speech``."""

    dispatcher, jarvis_store, emitted = _make_say_dispatcher()

    result = await dispatcher.dispatch(
        ToolCall(
            id="call_1",
            name="say",
            arguments={"speech": "Bonjour Tom"},
        )
    )

    assert result.outcome == "ok"
    assert result.tool_name == "say"
    assert result.tool_version == "v1"
    # Issue 0047: ``say`` carries no task_id; the orchestrator collects
    # speech + ui from the dispatch result directly.
    assert result.task_id is None
    assert result.speech == "Bonjour Tom"
    assert result.ui is None

    # The assistant turn landed in JarvisStore so the next user turn's
    # context assembly sees it as history.
    history = jarvis_store.history()
    assert history == [{"role": "assistant", "content": "Bonjour Tom"}]

    # The handler MUST NOT emit ``assistant_msg`` via the WS emitter —
    # the WS router emits exactly one frame after the orchestrator
    # returns. Emitting here would double-fire the frame.
    assert emitted == []


@pytest.mark.asyncio
async def test_say_threads_ui_payload_verbatim() -> None:
    """``say(speech, ui)`` returns the ``ui`` object unchanged for the orchestrator."""

    dispatcher, _store, _emitted = _make_say_dispatcher()

    ui_payload = {"component": "Markdown", "props": {"content": "# Hi"}}
    result = await dispatcher.dispatch(
        ToolCall(
            id="call_ui",
            name="say",
            arguments={"speech": "Voilà", "ui": ui_payload},
        )
    )

    assert result.outcome == "ok"
    assert result.speech == "Voilà"
    assert result.ui == ui_payload


@pytest.mark.asyncio
async def test_say_with_null_ui_returns_none() -> None:
    """``say(speech, ui=null)`` returns ``ui=None`` on the dispatch result."""

    dispatcher, _store, _emitted = _make_say_dispatcher()

    result = await dispatcher.dispatch(
        ToolCall(
            id="call_null_ui",
            name="say",
            arguments={"speech": "Plain reply", "ui": None},
        )
    )

    assert result.outcome == "ok"
    assert result.speech == "Plain reply"
    assert result.ui is None


@pytest.mark.asyncio
async def test_say_strips_leading_and_trailing_whitespace() -> None:
    """Whitespace around ``speech`` is stripped before persistence."""

    dispatcher, jarvis_store, _emitted = _make_say_dispatcher()

    result = await dispatcher.dispatch(
        ToolCall(
            id="call_ws",
            name="say",
            arguments={"speech": "   Coucou   "},
        )
    )

    assert result.outcome == "ok"
    assert result.speech == "Coucou"
    assert jarvis_store.history()[-1]["content"] == "Coucou"


@pytest.mark.asyncio
async def test_say_rejects_whitespace_only_speech() -> None:
    """The Pydantic ``min_length=1`` constraint rejects whitespace-only ``speech``."""

    dispatcher, jarvis_store, emitted = _make_say_dispatcher()

    # Pydantic accepts the string (length>=1), the handler strips and
    # returns the structured invalid_args error.
    result = await dispatcher.dispatch(
        ToolCall(
            id="call_blank",
            name="say",
            arguments={"speech": "   "},
        )
    )

    assert result.outcome == "error"
    assert result.error_code == "invalid_args"
    assert result.speech is None
    assert result.ui is None
    # Nothing persisted, nothing emitted.
    assert jarvis_store.history() == []
    assert emitted == []


@pytest.mark.asyncio
async def test_say_rejects_missing_speech() -> None:
    """Pydantic rejects the call entirely when ``speech`` is missing."""

    dispatcher, jarvis_store, emitted = _make_say_dispatcher()

    result = await dispatcher.dispatch(
        ToolCall(
            id="call_no_speech",
            name="say",
            arguments={},
        )
    )

    assert result.outcome == "error"
    assert result.error_code == "invalid_args"
    assert result.error_message is not None
    assert "speech" in result.error_message
    assert jarvis_store.history() == []
    assert emitted == []


@pytest.mark.asyncio
async def test_say_rejects_empty_string_speech() -> None:
    """``speech=""`` fails the ``min_length=1`` Pydantic constraint."""

    dispatcher, _store, _emitted = _make_say_dispatcher()

    result = await dispatcher.dispatch(
        ToolCall(
            id="call_empty",
            name="say",
            arguments={"speech": ""},
        )
    )

    assert result.outcome == "error"
    assert result.error_code == "invalid_args"


@pytest.mark.asyncio
async def test_say_handler_tolerates_missing_jarvis_store() -> None:
    """The handler skips persistence cleanly when no store is wired.

    Narrow registry-only test harnesses build the dispatcher with
    ``jarvis_store=None``. The handler must still return the speech +
    ui payload so the dispatcher contract stays intact.
    """

    class _StubTaskStore:
        def create_task(
            self,
            *,
            title: str,
            goal: str,
            parent_task_id: str | None = None,
            lineage: Any = None,
        ) -> str:
            return "task-stub"

        def get_task(self, task_id: str) -> Any:
            raise NotImplementedError

        def list_tasks(self, *, state: Any = None, limit: Any = None) -> Any:
            return []

        def append_message(
            self,
            task_id: str,
            *,
            role: Any,
            content: str,
            action: Any = None,
        ) -> int:
            return 1

        def get_task_messages(self, task_id: str) -> Any:
            return []

        def update_state(self, task_id: str, new_state: Any) -> None:
            return None

        def set_result(self, task_id: str, result: str) -> None:
            return None

        def set_delivered_at_turn(self, task_id: str, turn_index: int) -> None:
            return None

        def mark_superseded(self, task_id: str) -> None:
            return None

        def find_by_query(
            self,
            query: str,
            *,
            prefer_state: Any = None,
            limit: int = 1,
        ) -> Any:
            return []

    async def _emit(event: dict[str, Any]) -> None:
        return None

    dispatcher = ToolDispatcher(
        registry=ToolRegistry([build_say_tool()]),
        context=ToolHandlerContext(
            task_store=_StubTaskStore(),
            task_scheduler=_StubScheduler(),
            ws_emit=_emit,
            # jarvis_store intentionally omitted (defaults to None).
        ),
    )

    result = await dispatcher.dispatch(
        ToolCall(
            id="call_no_store",
            name="say",
            arguments={"speech": "ping"},
        )
    )

    assert result.outcome == "ok"
    assert result.speech == "ping"
