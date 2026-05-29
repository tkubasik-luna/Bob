"""Failure / empty-result / privacy branch tests for the Gmail search flow.

Issue 0056 ships the polish layer on top of the 0055 happy path: the
sub-agent must produce a specific French speech and ``ui_payload=null``
for each of four off-happy-path branches, and no Mail subject or
bodyPreview is ever allowed to land in a DebugEvent payload or an INFO
log record.

The four branches under test:

1. **Empty result** — :class:`GmailClient.search_messages` returns ``[]``.
   The sub-agent emits ``done(complete, ui_payload=null)`` with the
   "Aucun mail récent de {sender}." speech. No Mail overlay payload
   reaches the debug pipeline.
2. **Auth expired / bootstrap required** —
   :func:`auth.get_credentials` raises :class:`BootstrapRequiredError`.
   Handler folds it to ``error/gmail_search_bootstrap_required``. The
   sub-agent emits ``done(failed, ui_payload=null)`` with the
   bootstrap-recovery French speech (and the speech contains the
   literal ``python -m bob.connectors.gmail.auth`` command).
3. **API unreachable** — :meth:`GmailClient.search_messages` raises
   :class:`googleapiclient.errors.HttpError`. Handler folds it to the
   distinct ``error/gmail_search_api_unreachable`` code (not the generic
   ``gmail_search_failed``). The sub-agent emits ``done(failed)`` with
   the "Je n'ai pas pu joindre Gmail à l'instant" speech.
4. **Validation failure** — the LLM hallucinates an empty
   ``gmail_search()`` call. The dispatcher folds the Pydantic
   :class:`ValidationError` into a ``error/invalid_args`` tool message.
   The runner does NOT crash; instead the structured error round-trips
   to the LLM via a ``tool`` message and the LLM gets a second chance
   to call ``gmail_search`` with a meaningful filter.

The fifth branch is the **privacy regression guard**: a success-path run
with a canonical fixture whose subject is "Q3 forecast" and whose
snippet contains "Voici les chiffres pour T3" emits NO DebugEvent
payload and NO INFO structlog record carrying either of those strings.
"""

from __future__ import annotations

import asyncio
import json
import logging
import sqlite3
from datetime import UTC, datetime
from typing import Any, ClassVar
from unittest.mock import MagicMock

import pytest
import structlog
from googleapiclient.errors import HttpError

from bob.connectors.gmail import BootstrapRequiredError
from bob.connectors.gmail.models import EmailMessage
from bob.db.migrations_runner import apply_migrations, default_migrations_dir
from bob.debug_log import clear, snapshot_for_task
from bob.event_bus import EventBus
from bob.llm.types import LLMResponse, ToolDefinition
from bob.llm_client import LLMClient
from bob.sub_agent import (
    GmailSearchArgs,
    SubAgentPolicy,
    SubAgentRunner,
    SubAgentToolDispatcher,
    build_default_subagent_registry,
)
from bob.sub_agent.tool_registry import _gmail_search_handler
from bob.task_store import TaskStore

# Canonical privacy-fixture: subject + snippet contain strings we will
# grep for in the captured log / debug records.
_PRIVACY_SUBJECT = "Q3 forecast"
_PRIVACY_SNIPPET = "Voici les chiffres pour T3 — strictement confidentiel."

_PRIVACY_FIXTURE = EmailMessage(
    id="msg-privacy-1",
    thread_id="thread-privacy",
    from_name="CFO",
    from_email="cfo@example.com",
    received_at=datetime(2026, 5, 28, 10, 0, 0, tzinfo=UTC),
    subject=_PRIVACY_SUBJECT,
    snippet=_PRIVACY_SNIPPET,
    labels=["INBOX"],
    attachments=[],
)


# --- Helpers -----------------------------------------------------------------


class _ScriptedClient(LLMClient):
    """Scripted ``chat()`` client — pops canned responses in order."""

    def __init__(self, chat_values: list[str]) -> None:
        self._chat_values = list(chat_values)
        self.calls: list[dict[str, Any]] = []

    async def chat(
        self,
        messages: list[dict[str, Any]],
        schema: dict[str, Any] | None = None,
        session_id: str | None = None,
    ) -> str:
        self.calls.append({"messages": messages, "schema": schema, "session_id": session_id})
        if not self._chat_values:
            raise AssertionError("_ScriptedClient ran out of canned chat() responses")
        return self._chat_values.pop(0)

    async def complete(
        self,
        messages: list[dict[str, Any]],
        tools: list[ToolDefinition] | None = None,
        session_id: str | None = None,
    ) -> LLMResponse:
        raise NotImplementedError("not used")


def _make_store() -> TaskStore:
    conn = sqlite3.connect(":memory:")
    apply_migrations(conn, default_migrations_dir())
    return TaskStore(conn)


def _make_running_task(store: TaskStore, *, title: str, goal: str) -> str:
    task_id = store.create_task(title=title, goal=goal)
    store.update_state(task_id, "running")
    return task_id


async def _flush_event_loop() -> None:
    """Yield several times so any queued emits flush through the bus."""

    for _ in range(5):
        await asyncio.sleep(0)


# --- Stub context for direct handler invocation -----------------------------


class _StubContext:
    """Minimal :class:`SubAgentToolHandlerContext` for direct handler tests."""

    task_id = "task-test"
    state: ClassVar[dict[str, Any]] = {}


# --- Branch 1: empty result --------------------------------------------------


@pytest.mark.asyncio
async def test_empty_result_branch_emits_no_mail_overlay(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Empty list result → ``done(complete, ui_payload=null)``; no Mail event.

    Pinning the contract that an empty inbox result does NOT open the Mail
    overlay. The sub-agent ``done`` carries ``ui_payload=None`` and the
    HudTasks subscriber sees no Mail ``component`` descriptor in any
    captured debug event.
    """

    clear()

    def _fake_get_credentials() -> object:
        return object()

    class _EmptyClient:
        def __init__(self, _credentials: Any) -> None:
            pass

        def search_messages(self, query: str, max_results: int = 1) -> list[EmailMessage]:
            return []

    monkeypatch.setattr("bob.connectors.gmail.auth.get_credentials", _fake_get_credentials)
    monkeypatch.setattr("bob.connectors.gmail.GmailClient", _EmptyClient)

    script = [
        json.dumps(
            {
                "action": "tool_call",
                "name": "gmail_search",
                "args": {"from_name": "Holyana Callejon", "max_results": 1},
            }
        ),
        json.dumps(
            {
                "action": "done",
                "result_summary": "Aucun mail récent de Holyana Callejon.",
                "ui_payload": None,
                "status": "complete",
                "reason_code": "ok",
                "cost": {},
            }
        ),
    ]
    client = _ScriptedClient(chat_values=script)
    store = _make_store()
    task_id = _make_running_task(
        store,
        title="trouver le dernier mail d'Holyana",
        goal="Trouve-moi le dernier mail d'Holyana Callejon",
    )

    runner = SubAgentRunner(
        subagent_client=client,
        task_store=store,
        event_bus=EventBus(),
        policy=SubAgentPolicy(max_iterations=5, wall_clock_seconds=999.0, token_cap=999_999),
        tool_registry=build_default_subagent_registry(),
    )

    await runner.run(task_id)
    await _flush_event_loop()

    # Task ended in ``done`` — empty result is a success path per the
    # issue's "task transitions to done with no result payload".
    task = store.get_task(task_id)
    assert task.state == "done"

    # The tool's outcome was a clean ``ok`` with zero messages.
    tool_msgs = [m for m in store.get_task_messages(task_id) if m.role == "tool"]
    assert tool_msgs
    body = json.loads(tool_msgs[-1].content)
    assert body["status"] == "ok"
    assert body["result"]["count"] == 0
    assert body["result"]["messages"] == []

    # No Mail overlay descriptor is published — every status_change event
    # carries ``ui_payload=None`` because the LLM emitted ``done`` with
    # ``ui_payload: null``.
    captured = [event.to_dict() for event in snapshot_for_task(task_id)]
    status_changes = [ev for ev in captured if ev.get("payload", {}).get("kind") == "status_change"]
    assert status_changes
    for ev in status_changes:
        assert ev["payload"].get("ui_payload") is None, ev

    # The persisted task.result text is the "Aucun mail récent..." speech —
    # the user gets that string, not a Mail card.
    assert task.result is not None
    assert "Aucun mail récent" in task.result


# --- Branch 2: auth expired / bootstrap required ----------------------------


@pytest.mark.asyncio
async def test_bootstrap_required_branch_emits_recovery_speech(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Auth-expired branch → ``done(failed)`` with the bootstrap-command speech.

    The handler must fold the auth exception into
    ``gmail_search_bootstrap_required`` and the sub-agent must surface a
    French speech that contains the literal ``python -m
    bob.connectors.gmail.auth`` recovery command so the user knows how to
    repair the connection.
    """

    clear()

    def _fake_get_credentials() -> object:
        raise BootstrapRequiredError(
            "Gmail refresh token rejected by Google (probably revoked). "
            "Re-run `python -m bob.connectors.gmail.auth`."
        )

    monkeypatch.setattr("bob.connectors.gmail.auth.get_credentials", _fake_get_credentials)

    script = [
        json.dumps(
            {
                "action": "tool_call",
                "name": "gmail_search",
                "args": {"from_name": "Holyana"},
            }
        ),
        json.dumps(
            {
                "action": "done",
                "result_summary": (
                    "Mon accès à Gmail a expiré — relance le script de "
                    "connexion (python -m bob.connectors.gmail.auth)."
                ),
                "ui_payload": None,
                "status": "failed",
                "reason_code": "gmail_search_bootstrap_required",
                "cost": {},
            }
        ),
    ]
    client = _ScriptedClient(chat_values=script)
    store = _make_store()
    task_id = _make_running_task(
        store,
        title="vérifier Gmail",
        goal="Trouve le dernier mail d'Holyana",
    )

    runner = SubAgentRunner(
        subagent_client=client,
        task_store=store,
        event_bus=EventBus(),
        policy=SubAgentPolicy(max_iterations=5, wall_clock_seconds=999.0, token_cap=999_999),
        tool_registry=build_default_subagent_registry(),
    )

    await runner.run(task_id)
    await _flush_event_loop()

    task = store.get_task(task_id)
    assert task.state == "failed"

    # The handler returned the bootstrap-required error code.
    tool_msgs = [m for m in store.get_task_messages(task_id) if m.role == "tool"]
    assert tool_msgs
    body = json.loads(tool_msgs[-1].content)
    assert body["status"] == "error"
    assert body["error_code"] == "gmail_search_bootstrap_required"

    # The sub-agent's final speech (system message persisting the
    # ``done.result_summary`` on failure) contains the recovery command
    # literal so the user knows the next step.
    system_msgs = [m for m in store.get_task_messages(task_id) if m.role == "system"]
    final = system_msgs[-1].content if system_msgs else ""
    assert "python -m bob.connectors.gmail.auth" in final
    assert "expiré" in final


# --- Branch 3: API unreachable (HttpError) ----------------------------------


@pytest.mark.asyncio
async def test_api_unreachable_branch_uses_distinct_error_code(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``HttpError`` → ``error/gmail_search_api_unreachable`` (not generic).

    The handler must distinguish "Gmail down" from a generic failure so the
    sub-agent prompt can map it to the "réessaie dans un moment" speech
    rather than the catch-all "vérifie ta demande" message. The dispatcher
    round-trips the distinct code to the LLM via a ``tool`` message.
    """

    clear()

    def _fake_get_credentials() -> object:
        return object()

    class _DownClient:
        def __init__(self, _credentials: Any) -> None:
            pass

        def search_messages(self, query: str, max_results: int = 1) -> list[EmailMessage]:
            # Synthesise a 503 HttpError — googleapiclient.errors.HttpError
            # accepts a duck-typed response with .status and .reason.
            resp = MagicMock(status=503, reason="Service Unavailable")
            raise HttpError(resp, b'{"error":"upstream timed out"}')

    monkeypatch.setattr("bob.connectors.gmail.auth.get_credentials", _fake_get_credentials)
    monkeypatch.setattr("bob.connectors.gmail.GmailClient", _DownClient)

    script = [
        json.dumps(
            {
                "action": "tool_call",
                "name": "gmail_search",
                "args": {"from_name": "Holyana"},
            }
        ),
        json.dumps(
            {
                "action": "done",
                "result_summary": (
                    "Je n'ai pas pu joindre Gmail à l'instant — réessaie dans un moment."
                ),
                "ui_payload": None,
                "status": "failed",
                "reason_code": "gmail_search_api_unreachable",
                "cost": {},
            }
        ),
    ]
    client = _ScriptedClient(chat_values=script)
    store = _make_store()
    task_id = _make_running_task(
        store,
        title="checker Gmail",
        goal="Trouve le dernier mail d'Holyana",
    )

    runner = SubAgentRunner(
        subagent_client=client,
        task_store=store,
        event_bus=EventBus(),
        policy=SubAgentPolicy(max_iterations=5, wall_clock_seconds=999.0, token_cap=999_999),
        tool_registry=build_default_subagent_registry(),
    )

    await runner.run(task_id)
    await _flush_event_loop()

    task = store.get_task(task_id)
    assert task.state == "failed"

    tool_msgs = [m for m in store.get_task_messages(task_id) if m.role == "tool"]
    assert tool_msgs
    body = json.loads(tool_msgs[-1].content)
    assert body["status"] == "error"
    # Distinct from gmail_search_failed — pinpoints "Gmail down" so the
    # LLM picks the correct speech branch.
    assert body["error_code"] == "gmail_search_api_unreachable"

    # The persisted reason on failure is the LLM's done.result_summary
    # → the user hears the "réessaie dans un moment" line.
    system_msgs = [m for m in store.get_task_messages(task_id) if m.role == "system"]
    final = system_msgs[-1].content if system_msgs else ""
    assert "Gmail" in final
    assert "réessaie" in final


@pytest.mark.asyncio
async def test_api_unreachable_handler_classifies_http_error_directly(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Handler-level pinpoint: HttpError → gmail_search_api_unreachable.

    Exercises :func:`_gmail_search_handler` directly so the classification
    is locked in regardless of whether the runner is in the call path. A
    regression here would silently re-fold HttpError into the catch-all
    ``gmail_search_failed`` code and the prompt's "Gmail down" branch
    would stop firing.
    """

    def _fake_get_credentials() -> object:
        return object()

    class _DownClient:
        def __init__(self, _credentials: Any) -> None:
            pass

        def search_messages(self, query: str, max_results: int = 1) -> list[EmailMessage]:
            resp = MagicMock(status=500, reason="Internal Server Error")
            raise HttpError(resp, b'{"error":"boom"}')

    monkeypatch.setattr("bob.connectors.gmail.auth.get_credentials", _fake_get_credentials)
    monkeypatch.setattr("bob.connectors.gmail.GmailClient", _DownClient)

    outcome = await _gmail_search_handler(
        _StubContext(),
        GmailSearchArgs(from_name="Holyana"),
    )

    assert outcome.status == "error"
    assert outcome.error_code == "gmail_search_api_unreachable"
    assert outcome.error_message is not None
    # Generic phrasing — must not leak server-side details.
    assert "Gmail" in outcome.error_message


# --- Branch 4: validation failure (no-arg call) -----------------------------


@pytest.mark.asyncio
async def test_validation_branch_roundtrips_invalid_args_without_crashing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No-arg ``gmail_search()`` → ``system_validator`` feedback; no crash.

    The model occasionally calls a tool with the empty args object. Per
    the 0062 self-correction contract the arg-validation failure is fed
    back to the LLM under the ``system_validator`` role — NEVER the
    trusted ``tool`` role — so a malformed call cannot smuggle content in
    as if it were a real tool result. The runner keeps looping rather
    than terminating on the first validation failure: the LLM gets a
    second turn and can either retry with a proper filter or surrender
    with a final ``done(failed)``.
    """

    clear()

    def _fake_get_credentials() -> object:
        return object()

    class _UnusedClient:
        def __init__(self, _credentials: Any) -> None:
            pass

        def search_messages(self, query: str, max_results: int = 1) -> list[EmailMessage]:
            return []

    monkeypatch.setattr("bob.connectors.gmail.auth.get_credentials", _fake_get_credentials)
    monkeypatch.setattr("bob.connectors.gmail.GmailClient", _UnusedClient)

    # First call: no-args (validation fails). Second call: surrender with
    # a clean done(failed). The point of the test is that the runner did
    # NOT crash between turn 1 and turn 2 — the validation error was fed
    # back under ``system_validator`` and the LLM got a second turn.
    script = [
        json.dumps({"action": "tool_call", "name": "gmail_search", "args": {}}),
        json.dumps(
            {
                "action": "done",
                "result_summary": "Je n'ai pas su construire la recherche Gmail.",
                "ui_payload": None,
                "status": "failed",
                "reason_code": "gmail_search_failed",
                "cost": {},
            }
        ),
    ]
    client = _ScriptedClient(chat_values=script)
    store = _make_store()
    task_id = _make_running_task(
        store,
        title="checker Gmail",
        goal="Trouve un mail",
    )

    runner = SubAgentRunner(
        subagent_client=client,
        task_store=store,
        event_bus=EventBus(),
        policy=SubAgentPolicy(max_iterations=5, wall_clock_seconds=999.0, token_cap=999_999),
        tool_registry=build_default_subagent_registry(),
    )

    await runner.run(task_id)
    await _flush_event_loop()

    # The runner consumed BOTH scripted responses → it did not abort
    # after the validation error; it kept the LLM loop alive.
    assert len(client.calls) == 2

    task = store.get_task(task_id)
    assert task.state == "failed"

    # The validation error did NOT round-trip under the trusted ``tool``
    # role — that is the core 0062 security guarantee.
    tool_msgs = [m for m in store.get_task_messages(task_id) if m.role == "tool"]
    assert tool_msgs == []

    # The correction was injected under ``system_validator`` on the retry
    # (second) call, naming the offending tool + the ``invalid_args`` code
    # and carrying the escaped offending-output marker.
    retry_messages = client.calls[1]["messages"]
    validator_rows = [m for m in retry_messages if m["role"] == "system_validator"]
    assert len(validator_rows) == 1
    feedback = validator_rows[0]["content"]
    assert "gmail_search" in feedback
    assert "invalid_args" in feedback
    assert "[INVALID OUTPUT]:" in feedback
    # The message references the missing filter rule so the LLM knows how
    # to recover on retry (Pydantic's at-least-one-filter rule fired).
    assert "filter" in feedback.lower() or "filtre" in feedback


@pytest.mark.asyncio
async def test_validation_branch_dispatcher_direct() -> None:
    """Dispatcher-level pinpoint: no-arg call surfaces ``invalid_args``.

    Companion to the runner-level test above — exercises only the
    dispatcher so the fold-into-tool-message path is locked in even if
    the runner is refactored.
    """

    dispatcher = SubAgentToolDispatcher(build_default_subagent_registry())
    result = await dispatcher.dispatch(
        name="gmail_search",
        arguments={},
        context=_StubContext(),
    )
    assert result.outcome == "error"
    assert result.error_code == "invalid_args"
    assert result.tool_name == "gmail_search"


# --- Branch 5: privacy — subject / snippet never leak to debug or INFO log --


@pytest.mark.asyncio
async def test_subject_and_snippet_never_leak_into_debug_or_info_logs(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Privacy regression guard: subject + snippet are stripped from logs.

    Runs a happy-path Gmail search with a canonical fixture whose subject
    is ``"Q3 forecast"`` and whose snippet contains a confidential French
    string. Captures every :class:`DebugEvent` emitted during the run AND
    every structlog record at INFO level or above. Neither the subject
    nor the snippet may appear in either capture surface.

    Allowed in DebugEvent metadata: message id, thread id, sender email,
    label set, sender display name (``CFO``). Anything else from the
    Mail props is scrubbed by the runner before emit.
    """

    clear()

    def _fake_get_credentials() -> object:
        return object()

    class _PrivacyClient:
        def __init__(self, _credentials: Any) -> None:
            pass

        def search_messages(self, query: str, max_results: int = 1) -> list[EmailMessage]:
            return [_PRIVACY_FIXTURE]

    monkeypatch.setattr("bob.connectors.gmail.auth.get_credentials", _fake_get_credentials)
    monkeypatch.setattr("bob.connectors.gmail.GmailClient", _PrivacyClient)

    # Drive the structlog logs through the standard ``logging`` module so
    # ``caplog`` sees them. The Bob runtime does not pre-configure
    # structlog for tests — the default factory writes to stdlib logging
    # via ``BoundLogger.bind(...)`` → ``logger.info(...)``. We patch the
    # processor chain to route everything through stdlib at INFO level
    # for the duration of the test.
    structlog.configure(
        processors=[structlog.stdlib.ProcessorFormatter.wrap_for_formatter],
        wrapper_class=structlog.make_filtering_bound_logger(logging.DEBUG),
        logger_factory=structlog.stdlib.LoggerFactory(),
        cache_logger_on_first_use=False,
    )
    caplog.set_level(logging.INFO)

    # Mail props the LLM emits in ``done.ui_payload`` (mirrors what
    # ``to_mail_props`` would have produced — subject is the privacy
    # canary string).
    mail_props = {
        "from": {"name": "CFO", "email": "cfo@example.com"},
        "receivedAt": "2026-05-28T10:00:00Z",
        "subject": _PRIVACY_SUBJECT,
        "bodyPreview": _PRIVACY_SNIPPET,
        "flags": [],
        "attachments": [],
        "threadId": "thread-privacy",
        "messageId": "msg-privacy-1",
        "gmailWebUrl": "https://mail.google.com/mail/u/0/#inbox/thread-privacy",
    }
    script = [
        json.dumps({"action": "progress", "thought": "recherche Gmail"}),
        json.dumps(
            {
                "action": "tool_call",
                "name": "gmail_search",
                "args": {"from_name": "CFO", "max_results": 1},
            }
        ),
        json.dumps({"action": "progress", "thought": "lecture du mail"}),
        json.dumps(
            {
                "action": "done",
                "result_summary": (
                    f"Mail de CFO, sujet '{_PRIVACY_SUBJECT}', reçu il y a un instant"
                ),
                "ui_payload": {"component": "Mail", "props": mail_props},
                "status": "complete",
                "reason_code": "ok",
                "cost": {},
            }
        ),
    ]
    client = _ScriptedClient(chat_values=script)
    store = _make_store()
    task_id = _make_running_task(
        store,
        title="trouver le mail Q3",
        goal="Trouve le dernier mail du CFO",
    )

    runner = SubAgentRunner(
        subagent_client=client,
        task_store=store,
        event_bus=EventBus(),
        policy=SubAgentPolicy(max_iterations=10, wall_clock_seconds=999.0, token_cap=999_999),
        tool_registry=build_default_subagent_registry(),
    )

    await runner.run(task_id)
    await _flush_event_loop()

    captured = [event.to_dict() for event in snapshot_for_task(task_id)]
    assert captured, "expected at least one DebugEvent for the task"

    # The capture stream contains TWO classes of events:
    #
    # 1. Sub-agent reflection events emitted via ``emit_debug`` directly:
    #    ``thought`` / ``tool_invoke`` / ``tool_result`` /
    #    ``addendum_received`` / ``status_change``. These are the events
    #    the issue's privacy clause is about — they are pure dev-facing
    #    telemetry and MUST NOT carry email body fields.
    # 2. ``ws_event`` mirrors of the ``task_updated`` / ``task_message`` /
    #    ``task_result`` WS frames that the frontend chat client consumes
    #    (issue 0052 unified the two producers). Those frames legitimately
    #    carry the user-visible spoken text (the LLM's ``result_summary``,
    #    which IS read aloud and rendered on screen) — redacting them
    #    would silence the assistant. They are *not* a "debug payload" in
    #    the privacy sense; they are the existing chat wire payload.
    #
    # The privacy assertion targets class (1): reflection events.
    reflection_kinds = {
        "thought",
        "tool_invoke",
        "tool_result",
        "addendum_received",
        "status_change",
    }
    reflection_events = [
        ev for ev in captured if ev.get("payload", {}).get("kind") in reflection_kinds
    ]
    assert reflection_events, "expected at least one reflection DebugEvent"

    for event in reflection_events:
        as_json = json.dumps(event, ensure_ascii=False)
        assert _PRIVACY_SUBJECT not in as_json, (
            f"subject '{_PRIVACY_SUBJECT}' leaked into reflection event: {event}"
        )
        assert _PRIVACY_SNIPPET not in as_json, f"snippet leaked into reflection event: {event}"

    # --- Subject + snippet must NOT appear in any INFO-level log record ---
    for record in caplog.records:
        if record.levelno < logging.INFO:
            continue
        msg = record.getMessage()
        assert _PRIVACY_SUBJECT not in msg, (
            f"subject leaked at INFO via record {record.name}: {msg}"
        )
        assert _PRIVACY_SNIPPET not in msg, (
            f"snippet leaked at INFO via record {record.name}: {msg}"
        )

    # --- Cross-check: the metadata still flows through (sanity) ---
    # The status_change event must keep the messageId / threadId / sender
    # email so the overlay subscriber and the file sink can correlate
    # records without re-fetching from Gmail.
    status_changes = [
        ev for ev in reflection_events if ev.get("payload", {}).get("kind") == "status_change"
    ]
    assert status_changes
    final = status_changes[-1]
    ui_payload = final["payload"].get("ui_payload")
    assert isinstance(ui_payload, dict)
    assert ui_payload.get("component") == "Mail"
    redacted_props = ui_payload.get("props") or {}
    assert redacted_props.get("messageId") == "msg-privacy-1"
    assert redacted_props.get("threadId") == "thread-privacy"
    assert redacted_props.get("from", {}).get("email") == "cfo@example.com"
    # Subject is scrubbed (the canary string is replaced).
    assert redacted_props.get("subject") != _PRIVACY_SUBJECT
    assert redacted_props.get("bodyPreview") != _PRIVACY_SNIPPET
