"""Unit tests for the ``gmail_search`` sub-agent tool (issue 0055).

Two layers are exercised:

- :class:`GmailSearchArgs` Pydantic validation — at-least-one-filter rule,
  ``max_results`` cap, all-None rejection.
- :func:`_gmail_search_handler` end-to-end — happy path returns
  ``to_mail_props``-shaped dicts whose first entry validates against the
  ``Mail`` JSON schema, error path folds a :class:`GmailClient` exception
  into a structured ``error`` outcome (handler never raises through the
  dispatcher).

The Gmail HTTP layer is stubbed at the connector boundary:
``bob.connectors.gmail.auth.get_credentials`` returns a sentinel, and
``GmailClient.search_messages`` is monkey-patched to return a canonical
:class:`EmailMessage` fixture. We never touch the real OAuth flow or
``googleapiclient`` discovery during these tests.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, ClassVar

import pytest
from jsonschema import Draft202012Validator, FormatChecker
from pydantic import ValidationError

from bob.connectors.gmail.models import Attachment, EmailMessage
from bob.sub_agent.tool_registry import (
    GmailSearchArgs,
    SubAgentToolDispatcher,
    SubAgentToolHandlerOutcome,
    _gmail_search_handler,
    build_default_subagent_registry,
    build_gmail_search_tool,
)
from bob.ui_registry import MAIL

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


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


class _StubContext:
    """Minimal :class:`SubAgentToolHandlerContext` implementation for tests."""

    task_id = "task-test"
    state: ClassVar[dict[str, Any]] = {}


# ---------------------------------------------------------------------------
# GmailSearchArgs validation
# ---------------------------------------------------------------------------


def test_args_accepts_single_filter() -> None:
    """Any single non-None filter satisfies the at-least-one rule."""

    args = GmailSearchArgs(from_name="Holyana")
    assert args.from_name == "Holyana"
    assert args.max_results == 1


def test_args_all_none_rejected() -> None:
    """All-None payload trips the ``model_validator``."""

    with pytest.raises(ValidationError):
        GmailSearchArgs()


def test_args_all_blank_strings_rejected() -> None:
    """Empty / whitespace-only string filters are treated as absent.

    The validator's intent is "at least one meaningful filter". A payload
    full of empty strings would emit an empty query and Gmail would return
    the entire inbox — the same failure mode all-None protects against.
    """

    with pytest.raises(ValidationError):
        GmailSearchArgs(from_name="  ", subject_contains="")


def test_args_max_results_capped_at_five() -> None:
    """``max_results`` rejects values above the hard cap of 5."""

    with pytest.raises(ValidationError):
        GmailSearchArgs(from_name="Holyana", max_results=10)


def test_args_max_results_zero_rejected() -> None:
    """``max_results`` must be ≥ 1; zero is meaningless."""

    with pytest.raises(ValidationError):
        GmailSearchArgs(from_name="Holyana", max_results=0)


def test_args_has_attachment_only_filter_accepted() -> None:
    """``has_attachment=True`` alone counts as a filter."""

    args = GmailSearchArgs(has_attachment=True)
    assert args.has_attachment is True


# ---------------------------------------------------------------------------
# Handler happy path
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_handler_returns_mail_props_shape(monkeypatch: pytest.MonkeyPatch) -> None:
    """Mocked client returns the canonical fixture → handler emits Mail props.

    Cross-checks the first dict against the ``Mail`` component's JSON schema
    so any drift in :func:`to_mail_props` surfaces here.
    """

    captured: dict[str, Any] = {}

    def _fake_get_credentials() -> object:
        return object()

    class _FakeClient:
        def __init__(self, _credentials: Any) -> None:
            captured["client_built"] = True

        def search_messages(self, query: str, max_results: int = 1) -> list[EmailMessage]:
            captured["query"] = query
            captured["max_results"] = max_results
            return [_CANONICAL_MESSAGE]

    monkeypatch.setattr("bob.connectors.gmail.auth.get_credentials", _fake_get_credentials)
    monkeypatch.setattr("bob.connectors.gmail.GmailClient", _FakeClient)

    args = GmailSearchArgs(from_name="Holyana Callejon", max_results=1)
    outcome = await _gmail_search_handler(_StubContext(), args)

    assert isinstance(outcome, SubAgentToolHandlerOutcome)
    assert outcome.status == "ok"
    assert outcome.error_code is None
    assert outcome.result["count"] == 1
    assert isinstance(outcome.result["messages"], list)
    assert len(outcome.result["messages"]) == 1

    props = outcome.result["messages"][0]
    # Spot-check key fields — the full mapping lives in to_mail_props tests.
    assert props["from"]["name"] == "Holyana Callejon"
    assert props["from"]["email"] == "holyana@example.com"
    assert props["subject"] == "Récap réunion produit"
    assert props["receivedAt"].endswith("Z")
    assert props["threadId"] == "thread-99"
    assert props["messageId"] == "msg-12345"
    assert "priority" in props["flags"]  # IMPORTANT label → priority

    # The query was built from the structured args.
    assert "from:" in captured["query"]
    assert "Holyana" in captured["query"]

    # Cross-check against the Mail JSON schema — exactly the contract the
    # UI registry advertises (PRD 0007 / issue 0053).
    validator = Draft202012Validator(MAIL.props_schema, format_checker=FormatChecker())
    errors = sorted(validator.iter_errors(props), key=lambda e: list(e.path))
    assert not errors, [f"{'/'.join(str(p) for p in err.path)}: {err.message}" for err in errors]


@pytest.mark.asyncio
async def test_handler_no_results(monkeypatch: pytest.MonkeyPatch) -> None:
    """Zero-result search is a success path with an empty ``messages`` list."""

    def _fake_get_credentials() -> object:
        return object()

    class _FakeClient:
        def __init__(self, _credentials: Any) -> None:
            pass

        def search_messages(self, query: str, max_results: int = 1) -> list[EmailMessage]:
            return []

    monkeypatch.setattr("bob.connectors.gmail.auth.get_credentials", _fake_get_credentials)
    monkeypatch.setattr("bob.connectors.gmail.GmailClient", _FakeClient)

    outcome = await _gmail_search_handler(
        _StubContext(),
        GmailSearchArgs(from_name="Nobody"),
    )

    assert outcome.status == "ok"
    assert outcome.result["count"] == 0
    assert outcome.result["messages"] == []


# ---------------------------------------------------------------------------
# Handler error paths
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_handler_folds_search_exception_into_error_outcome(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A raising :class:`GmailClient` produces ``error/gmail_search_failed``."""

    def _fake_get_credentials() -> object:
        return object()

    class _FakeClient:
        def __init__(self, _credentials: Any) -> None:
            pass

        def search_messages(self, query: str, max_results: int = 1) -> list[EmailMessage]:
            raise RuntimeError("Gmail API exploded")

    monkeypatch.setattr("bob.connectors.gmail.auth.get_credentials", _fake_get_credentials)
    monkeypatch.setattr("bob.connectors.gmail.GmailClient", _FakeClient)

    outcome = await _gmail_search_handler(
        _StubContext(),
        GmailSearchArgs(from_name="Holyana"),
    )

    assert outcome.status == "error"
    assert outcome.error_code == "gmail_search_failed"
    assert outcome.error_message is not None
    assert "Gmail" in outcome.error_message


@pytest.mark.asyncio
async def test_handler_surfaces_bootstrap_required(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A missing token surfaces as ``error/gmail_search_bootstrap_required``."""

    from bob.connectors.gmail import BootstrapRequiredError

    def _fake_get_credentials() -> object:
        raise BootstrapRequiredError("No token at /tmp/token.json")

    monkeypatch.setattr("bob.connectors.gmail.auth.get_credentials", _fake_get_credentials)

    outcome = await _gmail_search_handler(
        _StubContext(),
        GmailSearchArgs(from_name="Holyana"),
    )

    assert outcome.status == "error"
    assert outcome.error_code == "gmail_search_bootstrap_required"


@pytest.mark.asyncio
async def test_handler_surfaces_refresh_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A refresh failure surfaces as ``error/gmail_search_refresh_failed``."""

    from bob.connectors.gmail import RefreshFailedError

    def _fake_get_credentials() -> object:
        raise RefreshFailedError("DNS resolution failed")

    monkeypatch.setattr("bob.connectors.gmail.auth.get_credentials", _fake_get_credentials)

    outcome = await _gmail_search_handler(
        _StubContext(),
        GmailSearchArgs(from_name="Holyana"),
    )

    assert outcome.status == "error"
    assert outcome.error_code == "gmail_search_refresh_failed"


# ---------------------------------------------------------------------------
# Dispatcher integration
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_dispatcher_routes_through_handler(monkeypatch: pytest.MonkeyPatch) -> None:
    """End-to-end through :class:`SubAgentToolDispatcher` — folds raises into outcomes.

    Even if the handler raised through the dispatcher, the dispatcher would
    fold it to ``error/handler_failed`` — but the handler should intercept
    first and return a domain-specific ``gmail_search_failed`` code. This
    test pins that contract.
    """

    def _fake_get_credentials() -> object:
        return object()

    class _FakeClient:
        def __init__(self, _credentials: Any) -> None:
            pass

        def search_messages(self, query: str, max_results: int = 1) -> list[EmailMessage]:
            raise RuntimeError("boom")

    monkeypatch.setattr("bob.connectors.gmail.auth.get_credentials", _fake_get_credentials)
    monkeypatch.setattr("bob.connectors.gmail.GmailClient", _FakeClient)

    registry = build_default_subagent_registry()
    dispatcher = SubAgentToolDispatcher(registry)

    result = await dispatcher.dispatch(
        name="gmail_search",
        arguments={"from_name": "Holyana"},
        context=_StubContext(),
    )

    # Handler-classified error rather than dispatcher's generic handler_failed.
    assert result.outcome == "error"
    assert result.error_code == "gmail_search_failed"
    assert result.tool_name == "gmail_search"
    assert result.tool_version == "v1"


# ---------------------------------------------------------------------------
# Builder shape
# ---------------------------------------------------------------------------


def test_build_gmail_search_tool_shape() -> None:
    """The builder returns a v1 ``gmail_search`` :class:`SubAgentToolDefinition`."""

    tool = build_gmail_search_tool()
    assert tool.name == "gmail_search"
    assert tool.version == "v1"
    assert tool.qualified_name == "v1.gmail_search"
    assert tool.args_model is GmailSearchArgs
    assert tool.handler is _gmail_search_handler
    assert tool.description  # non-empty French string


def test_default_registry_includes_gmail_search() -> None:
    """``build_default_subagent_registry`` exposes the gmail_search tool."""

    registry = build_default_subagent_registry()
    assert "gmail_search" in registry.names()
    tool = registry.get("gmail_search")
    assert tool is not None
    assert tool.args_model is GmailSearchArgs
