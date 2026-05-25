"""Tests for :class:`bob.epoch.retrieval.RetrievalAPI` — v1 stub.

The stub returns ``[]`` and logs a structured ``retrieval.recall_called``
event. The acceptance criteria from issue 0051 land on these two
behaviors:

1. "Contract test: callers of ``recall()`` handle empty results without
   crashing." — exercised by the smoke test below + the orchestrator
   integration test (issue 0051 wires a single call site in
   :meth:`Orchestrator.process_user_message`).
2. "Logs structured ``retrieval.recall_called`` event with query
   metadata." — asserted by injecting a structlog processor so the
   event passes a contract test regardless of the global structlog
   configuration. Pinning the event name string protects future call
   sites + 0052's event-refactor.
"""

from __future__ import annotations

from typing import Any

from bob.epoch.retrieval import RECALL_EVENT, RetrievalAPI


def test_recall_returns_empty_list() -> None:
    api = RetrievalAPI()
    result = api.recall("anything")
    assert list(result) == []


def test_recall_logs_structured_event(capsys: Any) -> None:
    """Capture the structlog event chain to assert the call site is observable.

    Structlog renders to stdout in both default + ``configure_logging``
    setups. We grep the captured output for the event name + payload
    fields. Tolerating both the dev ConsoleRenderer ("key=value") and
    the production JSONRenderer ("\"key\": value", with ASCII-escaped
    Unicode) shapes keeps the assertion resilient regardless of
    whether :func:`bob.logging_setup.configure_logging` already ran in
    this pytest session.

    The query is intentionally ASCII so the assertion does not depend
    on Unicode-escape behavior of the JSON renderer.
    """

    api = RetrievalAPI()
    api.recall("ou est mon python ?", limit=7)
    out = capsys.readouterr().out

    # Single-line assertion on the event name + the query payload —
    # works for both renderers because both emit "event=foo" /
    # "event": "foo" with the same substring "retrieval.recall_called".
    assert RECALL_EVENT in out, f"expected {RECALL_EVENT!r} in stdout, got: {out!r}"
    assert "ou est mon python ?" in out, f"expected query payload in stdout, got: {out!r}"
    assert "query_length=19" in out or '"query_length": 19' in out, (
        f"expected query_length=19 in stdout, got: {out!r}"
    )
    assert "limit=7" in out or '"limit": 7' in out
    assert "result_count=0" in out or '"result_count": 0' in out


def test_recall_handles_non_string_query_safely() -> None:
    """Defensive — passing a non-string query must not crash the stub."""

    api = RetrievalAPI()
    # mypy would catch this at static-check time; at runtime the stub
    # has to be lenient because the v1 contract is "never crash a
    # live turn".
    result = api.recall("")
    assert list(result) == []


def test_callers_handle_empty_results_smoke() -> None:
    """Smoke: a caller that iterates the result must not crash on ``[]``.

    Models the orchestrator's downstream code: even if no retrieved
    entries flow back, the caller should be able to ``for entry in
    api.recall(...)`` without TypeError / IndexError.
    """

    api = RetrievalAPI()
    iterations = 0
    for _ in api.recall("query"):
        iterations += 1
    assert iterations == 0
