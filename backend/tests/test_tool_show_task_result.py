"""Behavior tests for the ``show_task_result`` tool.

Pins the contract: the handler fuzzy-matches the query against the task
store, returns a Markdown component descriptor built from the stored
``task.result``, persists the spoken intro into :class:`JarvisStore`, and
surfaces clean error codes when no row matches or the matching row has no
result yet. Mirrors the structure of ``test_tool_say``.
"""

from __future__ import annotations

import sqlite3
from typing import Any

import pytest

from bob.db.migrations_runner import apply_migrations, default_migrations_dir
from bob.jarvis_store import JarvisStore
from bob.llm.types import ToolCall
from bob.task_store import TaskStore
from bob.tools.definitions.show_task_result import build_show_task_result_tool
from bob.tools.dispatcher import ToolDispatcher, ToolHandlerContext
from bob.tools.registry import ToolRegistry


class _StubScheduler:
    async def enqueue(self, task_id: str) -> None:
        raise AssertionError("enqueue not expected on show_task_result path")

    async def resume(self, task_id: str) -> None:
        raise AssertionError("resume not expected on show_task_result path")

    async def cancel(self, task_id: str, *, reason: str = "user_cancelled") -> None:
        raise AssertionError("cancel not expected on show_task_result path")


def _make_dispatcher() -> tuple[ToolDispatcher, TaskStore, JarvisStore, list[dict[str, Any]]]:
    """Build a dispatcher pre-wired with the tool + fresh stores."""

    conn = sqlite3.connect(":memory:")
    apply_migrations(conn, default_migrations_dir())
    jarvis_store = JarvisStore(conn)
    task_store = TaskStore(conn)
    emitted: list[dict[str, Any]] = []

    async def _emit(event: dict[str, Any]) -> None:
        emitted.append(event)

    dispatcher = ToolDispatcher(
        registry=ToolRegistry([build_show_task_result_tool()]),
        context=ToolHandlerContext(
            task_store=task_store,
            task_scheduler=_StubScheduler(),
            ws_emit=_emit,
            jarvis_store=jarvis_store,
        ),
    )
    return dispatcher, task_store, jarvis_store, emitted


def _seed_done_task(
    store: TaskStore,
    *,
    title: str,
    goal: str,
    result: str | None,
) -> str:
    """Insert a row + transition to ``done`` so it ranks first by prefer_state."""

    task_id = store.create_task(title=title, goal=goal)
    store.update_state(task_id, "running")
    store.update_state(task_id, "done")
    if result is not None:
        store.set_result(task_id, result)
    return task_id


@pytest.mark.asyncio
async def test_show_task_result_returns_stored_markdown_ui() -> None:
    """Happy path: query matches a done task, ui carries its stored result."""

    dispatcher, task_store, jarvis_store, emitted = _make_dispatcher()
    _seed_done_task(
        task_store,
        title="Approfondissement Bitcoin Pizza Day",
        goal="story of 10k BTC",
        result="# 🍕 Bitcoin Pizza Day\n\n10 000 BTC pour deux pizzas…",
    )

    result = await dispatcher.dispatch(
        ToolCall(
            id="call_show",
            name="show_task_result",
            arguments={
                "speech": "Tu m'avais demandé un focus sur le pizza day, voilà :",
                "query": "pizza day",
            },
        )
    )

    assert result.outcome == "ok"
    assert result.tool_name == "show_task_result"
    assert result.tool_version == "v1"
    assert result.speech == "Tu m'avais demandé un focus sur le pizza day, voilà :"
    assert result.ui == {
        "component": "Markdown",
        "props": {"content": "# 🍕 Bitcoin Pizza Day\n\n10 000 BTC pour deux pizzas…"},
    }
    # Intro phrase landed in JarvisStore so the next user turn sees it.
    assert jarvis_store.history()[-1] == {
        "role": "assistant",
        "content": "Tu m'avais demandé un focus sur le pizza day, voilà :",
    }
    # Handler must NOT emit ``assistant_msg`` itself — same contract as ``say``.
    assert emitted == []


@pytest.mark.asyncio
async def test_show_task_result_recalls_structured_mail_descriptor() -> None:
    """PRD 0008 / issue 0064 — when the matched task stored a structured Mail
    descriptor, the recall path re-emits it VERBATIM (original ``component``)
    instead of wrapping ``task.result`` as Markdown, so the Mail overlay
    rebuilds on recall the same way it did on live completion."""

    dispatcher, task_store, _jarvis_store, _emitted = _make_dispatcher()
    task_id = task_store.create_task(title="dernier mail d'Holyana", goal="…")
    task_store.update_state(task_id, "running")
    task_store.update_state(task_id, "done")
    mail_descriptor: dict[str, object] = {
        "component": "Mail",
        "props": {
            "messageId": "msg-12345",
            "subject": "Récap réunion produit",
            "bodyPreview": "Hello Tom…",
        },
    }
    # The spoken text stays in ``result``; the structured section list in
    # ``result_payload`` (exactly what the runner persists for a Mail done —
    # PRD 0010 / issue 0066, a single card is a list-of-one).
    task_store.set_result(
        task_id,
        "Mail de Holyana, sujet 'Récap réunion produit'",
        result_payload=[mail_descriptor],
    )

    result = await dispatcher.dispatch(
        ToolCall(
            id="call_mail_recall",
            name="show_task_result",
            arguments={"speech": "Tu m'avais demandé le mail d'Holyana :", "query": "Holyana"},
        )
    )

    assert result.outcome == "ok"
    # Re-emitted with the ORIGINAL Mail component + full props — NOT a Markdown
    # wrap of the spoken result text. The recall surface returns the full
    # section LIST (a single card is a list-of-one).
    assert result.ui == [mail_descriptor]
    assert result.ui[0] is not mail_descriptor  # defensive copy, not the stored ref


@pytest.mark.asyncio
async def test_show_task_result_reemits_all_mail_cards() -> None:
    """Regression — a multi-card deliverable (e.g. "20 derniers mails") must
    recall EVERY card, not collapse to the first. Before the fix the recall
    path returned only the first renderable section."""

    dispatcher, task_store, _jarvis_store, _emitted = _make_dispatcher()
    task_id = task_store.create_task(title="20 derniers mails reçus", goal="…")
    task_store.update_state(task_id, "running")
    task_store.update_state(task_id, "done")
    cards: list[dict[str, object]] = [
        {"component": "Mail", "props": {"messageId": f"msg-{i}", "subject": f"Sujet {i}"}}
        for i in range(20)
    ]
    task_store.set_result(task_id, "20 email(s) trouvé(s).", result_payload=cards)

    result = await dispatcher.dispatch(
        ToolCall(
            id="call_multi_mail",
            name="show_task_result",
            arguments={"speech": "Voilà tes 20 derniers mails :", "query": "20 derniers mails"},
        )
    )

    assert result.outcome == "ok"
    assert isinstance(result.ui, list)
    assert len(result.ui) == 20
    assert result.ui == cards


@pytest.mark.asyncio
async def test_show_task_result_returns_no_matching_task_error() -> None:
    """A query that hits zero rows returns ``no_matching_task`` cleanly."""

    dispatcher, task_store, _store, _emitted = _make_dispatcher()
    _seed_done_task(
        task_store,
        title="Révolution française",
        goal="dates clés",
        result="# Révolution",
    )

    result = await dispatcher.dispatch(
        ToolCall(
            id="call_miss",
            name="show_task_result",
            arguments={"speech": "Voilà :", "query": "pizza day"},
        )
    )

    assert result.outcome == "error"
    assert result.tool_name == "show_task_result"
    assert result.error_code == "no_matching_task"
    assert result.speech is None
    assert result.ui is None


@pytest.mark.asyncio
async def test_show_task_result_returns_no_persisted_result_error() -> None:
    """A match without a persisted ``result`` surfaces ``no_persisted_result``."""

    dispatcher, task_store, _store, _emitted = _make_dispatcher()
    _seed_done_task(
        task_store,
        title="Pizza Day",
        goal="…",
        result=None,
    )

    result = await dispatcher.dispatch(
        ToolCall(
            id="call_empty",
            name="show_task_result",
            arguments={"speech": "Voilà :", "query": "pizza day"},
        )
    )

    assert result.outcome == "error"
    assert result.error_code == "no_persisted_result"


@pytest.mark.asyncio
async def test_show_task_result_prefers_done_over_running() -> None:
    """When two rows match, the ``done`` one wins via ``prefer_state``."""

    dispatcher, task_store, _store, _emitted = _make_dispatcher()
    running_id = task_store.create_task(title="Pizza Day", goal="…")
    task_store.update_state(running_id, "running")
    task_store.set_result(running_id, "STALE RUNNING RESULT")

    done_id = _seed_done_task(
        task_store,
        title="Pizza Day",
        goal="…",
        result="DONE RESULT",
    )

    result = await dispatcher.dispatch(
        ToolCall(
            id="call_prefer",
            name="show_task_result",
            arguments={"speech": "Voilà :", "query": "pizza"},
        )
    )

    assert result.outcome == "ok"
    assert result.ui == {"component": "Markdown", "props": {"content": "DONE RESULT"}}
    # Sanity check — the running row exists but lost the ranking.
    assert running_id != done_id


@pytest.mark.asyncio
async def test_show_task_result_strips_whitespace_around_args() -> None:
    """Whitespace around ``speech`` / ``query`` is stripped before use."""

    dispatcher, task_store, jarvis_store, _emitted = _make_dispatcher()
    _seed_done_task(task_store, title="Pizza Day", goal="…", result="# OK")

    result = await dispatcher.dispatch(
        ToolCall(
            id="call_ws",
            name="show_task_result",
            arguments={
                "speech": "   Voilà   ",
                "query": "   pizza   ",
            },
        )
    )

    assert result.outcome == "ok"
    assert result.speech == "Voilà"
    assert jarvis_store.history()[-1]["content"] == "Voilà"
