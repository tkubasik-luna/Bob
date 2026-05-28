"""Tests for :mod:`bob.connectors.gmail.client`.

We mock the Gmail HTTP boundary by injecting a fake ``service`` via the
``service_factory`` seam — no patching of ``googleapiclient`` internals,
so tests survive library upgrades (PRD 0007 user story #40).
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from bob.connectors.gmail.client import GmailClient


class _FakeRequest:
    """Mimics the ``execute()`` step of googleapiclient's chained-method API."""

    def __init__(self, response: Any) -> None:
        self._response = response

    def execute(self) -> Any:
        return self._response


class _FakeMessages:
    """Records calls to ``list`` / ``get`` and replays canned responses."""

    def __init__(
        self,
        *,
        list_responses: list[dict[str, Any]] | None = None,
        get_responses: dict[str, dict[str, Any]] | None = None,
    ) -> None:
        self.list_calls: list[dict[str, Any]] = []
        self.get_calls: list[dict[str, Any]] = []
        self._list_responses = list(list_responses or [])
        self._get_responses = dict(get_responses or {})

    def list(self, **kwargs: Any) -> _FakeRequest:
        self.list_calls.append(kwargs)
        if not self._list_responses:
            return _FakeRequest({"messages": []})
        return _FakeRequest(self._list_responses.pop(0))

    def get(self, **kwargs: Any) -> _FakeRequest:
        self.get_calls.append(kwargs)
        msg_id = kwargs.get("id")
        response = self._get_responses.get(str(msg_id), {})
        return _FakeRequest(response)


class _FakeUsers:
    def __init__(self, messages: _FakeMessages) -> None:
        self._messages = messages

    def messages(self) -> _FakeMessages:
        return self._messages


class _FakeService:
    def __init__(self, messages: _FakeMessages) -> None:
        self._users = _FakeUsers(messages)

    def users(self) -> _FakeUsers:
        return self._users


def _payload(
    msg_id: str,
    *,
    thread_id: str = "thread-1",
    from_value: str = "Holyana Callejon <holy@example.com>",
    subject: str = "Status",
    snippet: str = "snippet",
    labels: list[str] | None = None,
    internal_date: str = "1717084920000",
) -> dict[str, Any]:
    return {
        "id": msg_id,
        "threadId": thread_id,
        "snippet": snippet,
        "labelIds": labels or ["INBOX"],
        "internalDate": internal_date,
        "payload": {
            "mimeType": "text/plain",
            "headers": [
                {"name": "From", "value": from_value},
                {"name": "Subject", "value": subject},
            ],
            "body": {"size": 0},
        },
    }


def _make_client(messages: _FakeMessages) -> GmailClient:
    service = _FakeService(messages)
    return GmailClient(
        credentials="ignored",
        service_factory=lambda creds: service,
    )


# --- search_messages ----------------------------------------------------------


def test_search_messages_returns_translated_emails() -> None:
    fake = _FakeMessages(
        list_responses=[
            {"messages": [{"id": "m1"}, {"id": "m2"}]},
        ],
        get_responses={
            "m1": _payload("m1", subject="First", from_value="A <a@a.com>"),
            "m2": _payload("m2", subject="Second", from_value="B <b@b.com>"),
        },
    )
    client = _make_client(fake)

    results = client.search_messages('from:"A"', max_results=2)

    assert [m.id for m in results] == ["m1", "m2"]
    assert [m.subject for m in results] == ["First", "Second"]
    assert [m.from_email for m in results] == ["a@a.com", "b@b.com"]


def test_search_messages_passes_query_and_max_results_to_api() -> None:
    fake = _FakeMessages(list_responses=[{"messages": []}])
    client = _make_client(fake)

    client.search_messages('from:"X"', max_results=5)

    assert fake.list_calls == [
        {"userId": "me", "q": 'from:"X"', "maxResults": 5},
    ]


def test_search_messages_returns_empty_list_when_no_matches() -> None:
    fake = _FakeMessages(list_responses=[{"messages": []}])
    client = _make_client(fake)
    assert client.search_messages("from:nobody") == []


def test_search_messages_handles_missing_messages_key() -> None:
    # Gmail returns `{"resultSizeEstimate": 0}` with no `messages` key when
    # there are no hits. The client must not KeyError on that.
    fake = _FakeMessages(list_responses=[{"resultSizeEstimate": 0}])
    client = _make_client(fake)
    assert client.search_messages("from:nobody") == []


def test_search_messages_caps_at_max_results() -> None:
    fake = _FakeMessages(
        list_responses=[
            {"messages": [{"id": "m1"}, {"id": "m2"}, {"id": "m3"}]},
        ],
        get_responses={
            "m1": _payload("m1"),
            "m2": _payload("m2"),
            "m3": _payload("m3"),
        },
    )
    client = _make_client(fake)

    results = client.search_messages("q", max_results=2)
    assert [m.id for m in results] == ["m1", "m2"]


def test_search_messages_zero_max_returns_empty_without_hitting_api() -> None:
    fake = _FakeMessages()
    client = _make_client(fake)

    assert client.search_messages("q", max_results=0) == []
    assert fake.list_calls == []
    assert fake.get_calls == []


def test_search_messages_skips_refs_with_no_id() -> None:
    fake = _FakeMessages(
        list_responses=[
            {"messages": [{"id": "m1"}, {"no_id": True}, {"id": "m3"}]},
        ],
        get_responses={
            "m1": _payload("m1"),
            "m3": _payload("m3"),
        },
    )
    client = _make_client(fake)

    results = client.search_messages("q", max_results=3)
    assert [m.id for m in results] == ["m1", "m3"]


# --- get_message --------------------------------------------------------------


def test_get_message_returns_email_message() -> None:
    fake = _FakeMessages(
        get_responses={
            "m1": _payload(
                "m1",
                thread_id="thread-xyz",
                subject="Hello",
                labels=["INBOX", "IMPORTANT"],
            )
        }
    )
    client = _make_client(fake)

    msg = client.get_message("m1")

    assert msg.id == "m1"
    assert msg.thread_id == "thread-xyz"
    assert msg.subject == "Hello"
    assert "IMPORTANT" in msg.labels
    assert msg.received_at == datetime.fromtimestamp(1717084920, tz=UTC)


def test_get_message_uses_full_format() -> None:
    fake = _FakeMessages(get_responses={"m1": _payload("m1")})
    client = _make_client(fake)
    client.get_message("m1")

    assert fake.get_calls == [{"userId": "me", "id": "m1", "format": "full"}]


def test_search_messages_internal_json_never_leaks() -> None:
    """Public surface returns ``EmailMessage`` only — no raw dicts.

    Regression guard: even if a future refactor accidentally returns the
    raw Gmail payload, this assertion fails.
    """

    fake = _FakeMessages(
        list_responses=[{"messages": [{"id": "m1"}]}],
        get_responses={"m1": _payload("m1")},
    )
    client = _make_client(fake)
    [msg] = client.search_messages("q")
    # EmailMessage is a frozen dataclass — never a dict.
    assert not isinstance(msg, dict)
    from bob.connectors.gmail.models import EmailMessage

    assert isinstance(msg, EmailMessage)


def test_custom_user_id_is_passed_through() -> None:
    fake = _FakeMessages(list_responses=[{"messages": []}])
    service = _FakeService(fake)
    client = GmailClient(
        credentials="ignored",
        service_factory=lambda creds: service,
        user_id="alice@example.com",
    )

    client.search_messages("q")

    assert fake.list_calls[0]["userId"] == "alice@example.com"
