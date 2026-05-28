"""Tests for :mod:`bob.connectors.gmail.models`.

Exercises the pure-function payload factory + UI props adapter against
canned Gmail JSON shapes. No googleapiclient imports — these tests run
without any network or third-party I/O.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest

from bob.connectors.gmail.models import (
    Attachment,
    EmailMessage,
    from_gmail_payload,
    to_mail_props,
)


def _basic_payload(**overrides: Any) -> dict[str, Any]:
    """Helper: build a minimal-but-realistic Gmail messages.get payload.

    Override individual fields via kwargs — keeps each test focused on the
    field it asserts about.
    """

    payload: dict[str, Any] = {
        "id": "msg-id-1",
        "threadId": "thread-id-1",
        "snippet": "Voici un aperçu",
        "labelIds": ["INBOX", "IMPORTANT"],
        "internalDate": "1717084920000",  # 2024-05-30T16:02:00Z
        "payload": {
            "mimeType": "multipart/mixed",
            "headers": [
                {"name": "From", "value": "Marie Lefèvre <marie@lunabee.com>"},
                {"name": "Subject", "value": "Q3 forecast"},
                {"name": "Date", "value": "Thu, 30 May 2024 18:02:00 +0200"},
            ],
            "parts": [
                {
                    "mimeType": "text/plain",
                    "filename": "",
                    "body": {"size": 256},
                }
            ],
        },
    }
    payload.update(overrides)
    return payload


def test_from_gmail_payload_basic_fields() -> None:
    msg = from_gmail_payload(_basic_payload())

    assert msg.id == "msg-id-1"
    assert msg.thread_id == "thread-id-1"
    assert msg.from_name == "Marie Lefèvre"
    assert msg.from_email == "marie@lunabee.com"
    assert msg.subject == "Q3 forecast"
    assert msg.snippet == "Voici un aperçu"
    assert msg.labels == ["INBOX", "IMPORTANT"]
    assert msg.attachments == []


def test_from_gmail_payload_prefers_date_header_over_internal_date() -> None:
    msg = from_gmail_payload(_basic_payload())
    # 2024-05-30T18:02:00 +02:00 == 2024-05-30T16:02:00 UTC
    assert msg.received_at == datetime(2024, 5, 30, 16, 2, 0, tzinfo=UTC)


def test_from_gmail_payload_falls_back_to_internal_date() -> None:
    payload = _basic_payload()
    payload["payload"]["headers"] = [
        {"name": "From", "value": "x@y.com"},
        {"name": "Subject", "value": "no-date"},
    ]
    msg = from_gmail_payload(payload)
    assert msg.received_at == datetime.fromtimestamp(1717084920, tz=UTC)


def test_from_gmail_payload_no_date_no_internal_date_uses_epoch() -> None:
    payload = _basic_payload()
    payload["payload"]["headers"] = []
    del payload["internalDate"]
    msg = from_gmail_payload(payload)
    assert msg.received_at == datetime.fromtimestamp(0, tz=UTC)


def test_from_gmail_payload_with_attachments() -> None:
    payload = _basic_payload()
    payload["payload"]["parts"] = [
        {"mimeType": "text/plain", "filename": "", "body": {"size": 100}},
        {
            "mimeType": "application/pdf",
            "filename": "forecast-q3.pdf",
            "body": {"size": 102400, "attachmentId": "att-1"},
        },
        {
            "mimeType": "image/png",
            "filename": "chart.png",
            "body": {"size": 5120, "attachmentId": "att-2"},
        },
    ]
    msg = from_gmail_payload(payload)
    assert msg.attachments == [
        Attachment(
            filename="forecast-q3.pdf",
            size_bytes=102400,
            mime_type="application/pdf",
            attachment_id="att-1",
        ),
        Attachment(
            filename="chart.png",
            size_bytes=5120,
            mime_type="image/png",
            attachment_id="att-2",
        ),
    ]


def test_from_gmail_payload_walks_nested_multipart() -> None:
    """Attachments can live inside multipart/* sub-parts; the walker recurses."""

    payload = _basic_payload()
    payload["payload"]["parts"] = [
        {
            "mimeType": "multipart/alternative",
            "parts": [
                {"mimeType": "text/plain", "filename": "", "body": {"size": 50}},
                {"mimeType": "text/html", "filename": "", "body": {"size": 100}},
            ],
        },
        {
            "mimeType": "multipart/mixed",
            "parts": [
                {
                    "mimeType": "application/zip",
                    "filename": "deep.zip",
                    "body": {"size": 9000, "attachmentId": "att-deep"},
                },
            ],
        },
    ]
    msg = from_gmail_payload(payload)
    assert len(msg.attachments) == 1
    assert msg.attachments[0].filename == "deep.zip"
    assert msg.attachments[0].attachment_id == "att-deep"


def test_from_gmail_payload_decodes_non_ascii_subject() -> None:
    payload = _basic_payload()
    payload["payload"]["headers"] = [
        {"name": "From", "value": "x@y.com"},
        {"name": "Subject", "value": "Réunion à 14h — café ☕"},
    ]
    msg = from_gmail_payload(payload)
    assert msg.subject == "Réunion à 14h — café ☕"


def test_from_gmail_payload_decodes_rfc2047_subject() -> None:
    payload = _basic_payload()
    payload["payload"]["headers"] = [
        {"name": "From", "value": "x@y.com"},
        # base64 of "Réunion" in UTF-8 = UsOpdW5pb24=
        {"name": "Subject", "value": "=?utf-8?B?UsOpdW5pb24=?="},
    ]
    msg = from_gmail_payload(payload)
    assert msg.subject == "Réunion"


def test_from_gmail_payload_handles_missing_payload_object() -> None:
    msg = from_gmail_payload({"id": "x", "threadId": "y", "snippet": ""})
    assert msg.id == "x"
    assert msg.thread_id == "y"
    assert msg.from_name == ""
    assert msg.from_email == ""
    assert msg.subject == ""
    assert msg.labels == []
    assert msg.attachments == []


def test_from_gmail_payload_handles_no_display_name_in_from() -> None:
    payload = _basic_payload()
    payload["payload"]["headers"] = [
        {"name": "From", "value": "marie@lunabee.com"},
        {"name": "Subject", "value": "x"},
    ]
    msg = from_gmail_payload(payload)
    assert msg.from_name == ""
    assert msg.from_email == "marie@lunabee.com"


def test_from_gmail_payload_tolerates_invalid_internal_date() -> None:
    payload = _basic_payload()
    payload["payload"]["headers"] = []
    payload["internalDate"] = "not-a-number"
    msg = from_gmail_payload(payload)
    assert msg.received_at == datetime.fromtimestamp(0, tz=UTC)


# --- to_mail_props ------------------------------------------------------------


def _make_msg(**overrides: Any) -> EmailMessage:
    defaults: dict[str, Any] = {
        "id": "msg-1",
        "thread_id": "thread-1",
        "from_name": "Holyana Callejon",
        "from_email": "holy@example.com",
        "received_at": datetime(2025, 5, 28, 14, 22, 0, tzinfo=UTC),
        "subject": "Q3 forecast",
        "snippet": "Voici un aperçu...",
        "labels": [],
        "attachments": [],
    }
    defaults.update(overrides)
    return EmailMessage(**defaults)


def test_to_mail_props_basic_shape() -> None:
    msg = _make_msg()
    props = to_mail_props(msg)
    assert props == {
        "from": {"name": "Holyana Callejon", "email": "holy@example.com"},
        "receivedAt": "2025-05-28T14:22:00Z",
        "subject": "Q3 forecast",
        "bodyPreview": "Voici un aperçu...",
        "flags": [],
        "attachments": [],
        "threadId": "thread-1",
        "messageId": "msg-1",
        "gmailWebUrl": "https://mail.google.com/mail/u/0/#inbox/thread-1",
    }


def test_to_mail_props_derives_flags_in_canonical_order() -> None:
    msg = _make_msg(labels=["UNREAD", "STARRED", "IMPORTANT", "INBOX"])
    props = to_mail_props(msg)
    # Order is enum order (priority → unread → starred), not Gmail's label
    # list order — deterministic chip rendering.
    assert props["flags"] == ["priority", "unread", "starred"]


def test_to_mail_props_skips_unknown_labels() -> None:
    msg = _make_msg(labels=["INBOX", "CATEGORY_PROMOTIONS"])
    props = to_mail_props(msg)
    assert props["flags"] == []


def test_to_mail_props_attachments_shape() -> None:
    msg = _make_msg(
        attachments=[
            Attachment(
                filename="forecast-q3.pdf",
                size_bytes=102400,
                mime_type="application/pdf",
                attachment_id="att-1",
            ),
        ]
    )
    props = to_mail_props(msg)
    assert props["attachments"] == [
        {"name": "forecast-q3.pdf", "sizeBytes": 102400, "mime": "application/pdf"},
    ]


def test_to_mail_props_account_index_changes_url() -> None:
    msg = _make_msg(thread_id="abc")
    props = to_mail_props(msg, account_index=3)
    assert props["gmailWebUrl"] == "https://mail.google.com/mail/u/3/#inbox/abc"


def test_to_mail_props_normalises_non_utc_received_at() -> None:
    from datetime import timedelta, timezone

    tz_plus2 = timezone(timedelta(hours=2))
    msg = _make_msg(received_at=datetime(2025, 5, 28, 16, 22, 0, tzinfo=tz_plus2))
    props = to_mail_props(msg)
    # 16:22 +02:00 → 14:22 UTC, rendered with trailing Z.
    assert props["receivedAt"] == "2025-05-28T14:22:00Z"


def test_to_mail_props_matches_mail_schema_if_registered() -> None:
    """When issue 0053 lands, ``Mail`` becomes part of the default registry.

    This test validates the adapter output against the registered schema
    when present, and is xfailed otherwise (with a pointer to 0053). The
    adapter's output shape is the contract regardless — see the test
    above asserting the literal dict.
    """

    from bob import ui_registry

    registry = ui_registry.build_registry()
    mail = registry.components.get("Mail")
    if mail is None:
        pytest.xfail(
            "Mail component not yet registered in build_registry(); "
            "lands with issue 0053. The literal-shape test above covers "
            "the contract independently."
        )

    msg = _make_msg(
        labels=["IMPORTANT", "UNREAD"],
        attachments=[
            Attachment(
                filename="forecast.pdf",
                size_bytes=512,
                mime_type="application/pdf",
                attachment_id="att-1",
            )
        ],
    )
    props = to_mail_props(msg)

    from jsonschema import Draft202012Validator

    validator = Draft202012Validator(mail.props_schema)
    errors = list(validator.iter_errors(props))
    assert errors == [], f"Mail schema validation errors: {errors}"
