"""End-to-end sub-agent runner test for the ``gmail_search`` tool (issue 0055).

Stubs the LLM with a deterministic action script:

1. ``progress("recherche Gmail")`` — pre-tool-call reflection.
2. ``tool_call(gmail_search, …)`` — exercises the connector boundary
   through the dispatcher.
3. ``progress("lecture du mail")`` — post-tool-call reflection.
4. ``done(ui_payload={component:"Mail", props:…}, …)`` — terminal action
   carrying the Mail overlay payload.

Asserts:

- Both reflection events land in the debug ring buffer with
  ``kind=thought`` (HudTasks subscribes to these).
- The terminal task row carries the Mail ``ui_payload`` so the overlay
  can render it.
- The Mail props embedded in ``ui_payload`` are precisely what
  :func:`to_mail_props` produces from the canonical fixture.

The Gmail HTTP layer is monkey-patched the same way as in
``test_gmail_search_tool.py`` — no OAuth, no googleapiclient, no network.
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
from datetime import UTC, datetime
from typing import Any

import pytest

from bob import ws_events
from bob.connectors.gmail.models import Attachment, EmailMessage, to_mail_props
from bob.db.migrations_runner import apply_migrations, default_migrations_dir
from bob.debug_log import snapshot_for_task
from bob.event_bus import EventBus
from bob.llm.types import LLMResponse, ToolDefinition
from bob.llm_client import LLMClient
from bob.sub_agent import (
    SubAgentPolicy,
    SubAgentRunner,
    build_default_subagent_registry,
)
from bob.task_store import TaskStore

_CANONICAL_MESSAGE = EmailMessage(
    id="msg-12345",
    thread_id="thread-99",
    from_name="Holyana Callejon",
    from_email="holyana@example.com",
    received_at=datetime(2026, 5, 27, 14, 22, 0, tzinfo=UTC),
    subject="Récap réunion produit",
    snippet="Hello Tom, je récapitule les points de la réunion …",
    labels=["IMPORTANT", "INBOX"],
    attachments=[
        Attachment(
            filename="notes.pdf",
            size_bytes=12_345,
            mime_type="application/pdf",
            attachment_id="ATT-1",
        ),
    ],
)


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


def _make_running_task(store: TaskStore) -> str:
    task_id = store.create_task(
        title="trouver le dernier mail d'Holyana",
        goal="Trouve-moi le dernier mail d'Holyana Callejon",
    )
    store.update_state(task_id, "running")
    return task_id


@pytest.mark.asyncio
async def test_gmail_search_runner_e2e(monkeypatch: pytest.MonkeyPatch) -> None:
    """Full sub-agent loop: progress → gmail_search → progress → done with Mail UI."""

    # --- Stub the Gmail connector boundary --------------------------------

    def _fake_get_credentials() -> object:
        return object()

    class _FakeClient:
        def __init__(self, _credentials: Any) -> None:
            pass

        def search_messages(self, query: str, max_results: int = 1) -> list[EmailMessage]:
            return [_CANONICAL_MESSAGE]

    monkeypatch.setattr("bob.connectors.gmail.auth.get_credentials", _fake_get_credentials)
    monkeypatch.setattr("bob.connectors.gmail.GmailClient", _FakeClient)

    # --- Build the expected Mail props the sub-agent should surface ------

    expected_props = to_mail_props(_CANONICAL_MESSAGE)

    # --- Scripted LLM responses -------------------------------------------
    #
    # The sub-agent prompt instructs the model to emit two progress
    # reflections around the gmail_search call. We bake exactly that script
    # so the test verifies the runtime wiring (dispatcher + emit_debug +
    # task row finalisation) rather than re-testing the LLM's adherence
    # to the prompt.

    script = [
        json.dumps({"action": "progress", "thought": "recherche Gmail"}),
        json.dumps(
            {
                "action": "tool_call",
                "name": "gmail_search",
                "args": {"from_name": "Holyana Callejon", "max_results": 1},
            }
        ),
        json.dumps({"action": "progress", "thought": "lecture du mail"}),
        json.dumps(
            {
                "action": "done",
                "result_summary": (
                    "Mail de Holyana Callejon, sujet 'Récap réunion produit', "
                    "reçu il y a un instant"
                ),
                "ui_payload": {"component": "Mail", "props": expected_props},
                "status": "complete",
                "reason_code": "ok",
                "cost": {},
            }
        ),
    ]
    client = _ScriptedClient(chat_values=script)

    # --- Set up the runner with the default registry (gmail_search wired) --

    store = _make_store()
    task_id = _make_running_task(store)

    # This test exercises the full MODEL-DRIVEN flow (progress → tool_call →
    # progress → done) through the real connector boundary, so convergence is
    # disabled here; the deterministic-convergence happy path is covered by
    # ``test_gmail_search_runner_e2e_converges`` below (PRD 0009 P5).
    runner = SubAgentRunner(
        subagent_client=client,
        task_store=store,
        event_bus=EventBus(),
        policy=SubAgentPolicy(
            max_iterations=10,
            wall_clock_seconds=999.0,
            token_cap=999_999,
            converge_on_terminal_result=False,
        ),
        tool_registry=build_default_subagent_registry(),
    )

    # Capture the chat WS frames (issue 0064): the runner forwards the REAL
    # Mail props on the ``task_result`` event to the chat socket while only a
    # redacted copy lands in the debug ring buffer. We assert both posture
    # ends here, so we tap the WS emitter in addition to the debug snapshot.
    ws_frames: list[dict[str, Any]] = []

    async def _capture_ws(event: dict[str, Any]) -> None:
        ws_frames.append(event)

    ws_events.set_emitter(_capture_ws)
    try:
        await runner.run(task_id)
        # Yield once so any queued emits flush through the ring buffer before
        # we snapshot it.
        for _ in range(5):
            await asyncio.sleep(0)
    finally:
        ws_events.set_emitter(None)

    # Collect every debug event the runner emitted for this task.
    captured_events = [event.to_dict() for event in snapshot_for_task(task_id)]

    # --- Assertions -------------------------------------------------------

    # 1. The runner consumed all 4 scripted messages.
    assert len(client.calls) == 4

    # 2. Terminal task state is ``done`` with the Mail ui_payload in the row.
    task = store.get_task(task_id)
    assert task.state == "done"

    # The runner persists ``task.result`` as the markdown deliverable text;
    # for non-markdown payloads (like our Mail dict) ``_deliverable_text``
    # returns None and the row falls back to ``result_summary``. So we
    # cross-check both: the result_summary phrasing (recorded as the
    # ``done`` assistant message) and the ui_payload routed through the
    # debug event (which is where the overlay actually subscribes).
    assert task.result is not None
    assert "Holyana" in task.result
    assert "Récap" in task.result

    # 3. Reflection events for "recherche Gmail" + "lecture du mail" landed.
    thought_summaries = [
        ev.get("payload", {}).get("thought")
        for ev in captured_events
        if ev.get("payload", {}).get("kind") == "thought"
    ]
    assert "recherche Gmail" in thought_summaries
    assert "lecture du mail" in thought_summaries

    # 4. The final ``status_change`` reflection carries the Mail ui_payload
    #    so the overlay can pick it up — but the subject / bodyPreview are
    #    REDACTED in the debug envelope per the issue 0056 privacy posture.
    #    Metadata (sender email, thread id, message id, labels) is allowed
    #    to flow through the overlay subscriber unchanged. The unredacted
    #    Mail props still reach the LLM and the Mail overlay via the
    #    ``task_result`` WS event and the streaming ``ui_payload`` frame —
    #    just not through this debug payload.
    status_change_events = [
        ev
        for ev in captured_events
        if ev.get("payload", {}).get("kind") == "status_change"
        and ev.get("payload", {}).get("new_state") == "done"
    ]
    assert status_change_events
    final_payload = status_change_events[-1]["payload"]
    # PRD 0010 / issue 0066 — the deliverable is a LIST of section descriptors.
    assert isinstance(final_payload.get("ui_payload"), list)
    assert final_payload["ui_payload"][0]["component"] == "Mail"
    mail_props = final_payload["ui_payload"][0]["props"]
    # Metadata stays in clear — needed to route the event downstream.
    assert mail_props["from"]["name"] == "Holyana Callejon"
    assert mail_props["from"]["email"] == "holyana@example.com"
    assert mail_props["threadId"] == "thread-99"
    assert mail_props["messageId"] == "msg-12345"
    # Body fields are scrubbed.
    assert mail_props["subject"] == "<redacted-for-privacy>"
    assert mail_props["bodyPreview"] == "<redacted-for-privacy>"

    # 5. PRD 0008 / issue 0064 — the structured Mail descriptor SURVIVES
    #    persistence: ``task.result_payload`` holds the full ``{component,
    #    props}`` shape with the REAL (unredacted) subject so the recall path
    #    (``show_task_result``) and a reconnect replay can rebuild the overlay.
    assert task.result_payload is not None
    assert task.result_payload[0]["component"] == "Mail"
    persisted_props = task.result_payload[0]["props"]
    assert isinstance(persisted_props, dict)
    assert persisted_props["messageId"] == "msg-12345"
    assert persisted_props["subject"] == "Récap réunion produit"
    assert persisted_props == expected_props

    # 6. The chat WS ``task_result`` frame carries the REAL props (the overlay
    #    needs the subject / body to render) — this is the frame the frontend
    #    dispatches on ``component`` to open MailOverlay.
    ws_task_results = [f for f in ws_frames if f.get("type") == "task_result"]
    assert ws_task_results
    ws_result = ws_task_results[-1]
    assert ws_result["task_id"] == task_id
    assert "result_payload" in ws_result
    assert ws_result["result_payload"][0]["component"] == "Mail"
    ws_props = ws_result["result_payload"][0]["props"]
    assert ws_props["subject"] == "Récap réunion produit"
    assert ws_props["bodyPreview"] == expected_props["bodyPreview"]

    # 7. Privacy: the SAME ``task_result`` event, as captured in the debug ring
    #    buffer, must carry the REDACTED descriptor — the subject / bodyPreview
    #    never reach the debug feed / JSONL sink. The chat WS (asserted above)
    #    still got the real content; only the debug copy is scrubbed.
    debug_task_results = [
        ev
        for ev in captured_events
        if isinstance(ev.get("payload", {}).get("ws_event"), dict)
        and ev["payload"]["ws_event"].get("type") == "task_result"
    ]
    assert debug_task_results
    debug_ws_event = debug_task_results[-1]["payload"]["ws_event"]
    assert debug_ws_event["result_payload"][0]["component"] == "Mail"
    debug_props = debug_ws_event["result_payload"][0]["props"]
    assert debug_props["subject"] == "<redacted-for-privacy>"
    assert debug_props["bodyPreview"] == "<redacted-for-privacy>"
    # Metadata still flows (needed to route / identify the mail downstream).
    assert debug_props["messageId"] == "msg-12345"
    # For a Mail payload the redacted ``result`` text is elided entirely
    # (it typically embeds the subject) — matching the status_change posture.
    assert debug_ws_event["result"] is None


@pytest.mark.asyncio
async def test_gmail_search_runner_e2e_converges(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """PRD 0009 P5 — the DEFAULT happy path through the real connector: a single
    ``gmail_search`` tool call CONVERGES deterministically to ``done(complete)``
    with the Mail card built from the stored result. No progress/done
    round-trips, and the model never hand-builds the descriptor — yet the
    overlay still renders and the 0056 redaction posture holds."""

    def _fake_get_credentials() -> object:
        return object()

    class _FakeClient:
        def __init__(self, _credentials: Any) -> None:
            pass

        def search_messages(self, query: str, max_results: int = 1) -> list[EmailMessage]:
            return [_CANONICAL_MESSAGE]

    monkeypatch.setattr("bob.connectors.gmail.auth.get_credentials", _fake_get_credentials)
    monkeypatch.setattr("bob.connectors.gmail.GmailClient", _FakeClient)

    expected_props = to_mail_props(_CANONICAL_MESSAGE)

    # ONLY the tool call is scripted — convergence ends the run before the model
    # would emit progress/done. A second scripted reply would be unused.
    script = [
        json.dumps(
            {
                "action": "tool_call",
                "name": "gmail_search",
                "args": {"from_name": "Holyana Callejon", "max_results": 1},
            }
        ),
    ]
    client = _ScriptedClient(chat_values=script)
    store = _make_store()
    task_id = _make_running_task(store)

    runner = SubAgentRunner(
        subagent_client=client,
        task_store=store,
        event_bus=EventBus(),
        # Default policy: converge_on_terminal_result is True.
        policy=SubAgentPolicy(max_iterations=10, wall_clock_seconds=999.0, token_cap=999_999),
        tool_registry=build_default_subagent_registry(),
    )

    ws_frames: list[dict[str, Any]] = []

    async def _capture_ws(event: dict[str, Any]) -> None:
        ws_frames.append(event)

    ws_events.set_emitter(_capture_ws)
    try:
        await runner.run(task_id)
        for _ in range(5):
            await asyncio.sleep(0)
    finally:
        ws_events.set_emitter(None)

    # Converged in ONE LLM call (the tool_call); no second turn was needed.
    assert len(client.calls) == 1

    task = store.get_task(task_id)
    assert task.state == "done"
    # The Mail card was built deterministically from the stored result.
    assert task.result_payload is not None
    assert task.result_payload[0]["component"] == "Mail"
    assert task.result_payload[0]["props"] == expected_props
    # Spoken summary is the deterministic projection.
    assert task.result is not None
    assert "Holyana Callejon" in task.result

    # The chat WS frame carries the REAL props for the overlay …
    ws_task_results = [f for f in ws_frames if f.get("type") == "task_result"]
    assert ws_task_results
    assert ws_task_results[-1]["result_payload"][0]["props"]["subject"] == "Récap réunion produit"

    # … while the debug ring buffer copy redacts subject / bodyPreview (0056).
    captured_events = [event.to_dict() for event in snapshot_for_task(task_id)]
    status_change_events = [
        ev
        for ev in captured_events
        if ev.get("payload", {}).get("kind") == "status_change"
        and ev.get("payload", {}).get("new_state") == "done"
    ]
    assert status_change_events
    redacted = status_change_events[-1]["payload"]["ui_payload"][0]["props"]
    assert redacted["subject"] == "<redacted-for-privacy>"
    assert redacted["bodyPreview"] == "<redacted-for-privacy>"
    # Metadata still flows for routing.
    assert redacted["messageId"] == "msg-12345"


@pytest.mark.asyncio
async def test_gmail_search_runner_handles_search_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A failing ``gmail_search`` round-trips as a tool error → sub-agent ``done(failed)``.

    The LLM script reacts to the tool failure by emitting a final ``done``
    with ``status=failed``; in production the prompt nudges this path. We
    pin the contract end-to-end: the dispatcher folds the exception into
    an ``error`` outcome and the runner keeps looping rather than crashing.
    """

    def _fake_get_credentials() -> object:
        return object()

    class _FakeClient:
        def __init__(self, _credentials: Any) -> None:
            pass

        def search_messages(self, query: str, max_results: int = 1) -> list[EmailMessage]:
            raise RuntimeError("Gmail down")

    monkeypatch.setattr("bob.connectors.gmail.auth.get_credentials", _fake_get_credentials)
    monkeypatch.setattr("bob.connectors.gmail.GmailClient", _FakeClient)

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
                "result_summary": "Impossible d'accéder à Gmail pour le moment.",
                "ui_payload": None,
                "status": "failed",
                "reason_code": "gmail_search_failed",
                "cost": {},
            }
        ),
    ]
    client = _ScriptedClient(chat_values=script)
    store = _make_store()
    task_id = _make_running_task(store)

    runner = SubAgentRunner(
        subagent_client=client,
        task_store=store,
        event_bus=EventBus(),
        policy=SubAgentPolicy(max_iterations=5, wall_clock_seconds=999.0, token_cap=999_999),
        tool_registry=build_default_subagent_registry(),
    )

    await runner.run(task_id)

    task = store.get_task(task_id)
    assert task.state == "failed"

    # The tool's structured error round-trips to the LLM as a ``tool`` message.
    tool_msgs = [m for m in store.get_task_messages(task_id) if m.role == "tool"]
    assert tool_msgs
    body = json.loads(tool_msgs[-1].content)
    assert body["tool"] == "gmail_search"
    assert body["status"] == "error"
    assert body["error_code"] == "gmail_search_failed"
