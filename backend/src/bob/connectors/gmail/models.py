"""Domain model for Gmail messages + adapter to the ``Mail`` UI component props.

The :class:`EmailMessage` dataclass is the connector's stable contract:
callers (tool handlers, tests, future synthesisers) work with it, never with
Gmail's wire-format JSON. The :func:`from_gmail_payload` factory translates a
Gmail ``users.messages.get`` (``format=full``) response into the dataclass;
the :func:`to_mail_props` adapter turns the dataclass into the ``Mail`` UI
component's props dict (see PRD 0007 + issue 0053 for the schema).

Keeping the factory and the adapter as **pure functions** is a deliberate
testability choice: both can be exercised against canned fixtures with no
mocking, and the rest of the connector can be swapped freely without
touching the data shape.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from email.utils import parseaddr, parsedate_to_datetime
from typing import Any


@dataclass(frozen=True)
class Attachment:
    """A single attachment on a Gmail message.

    ``attachment_id`` is Gmail's opaque handle; callers can later fetch the
    bytes via ``users.messages.attachments.get`` if needed. ``size_bytes``
    is Gmail's reported size (may be 0 when Gmail does not report it).
    """

    filename: str
    size_bytes: int
    mime_type: str
    attachment_id: str


@dataclass(frozen=True)
class EmailMessage:
    """A Gmail message in Bob's domain shape — independent of the API JSON.

    Times are timezone-aware UTC :class:`datetime` instances. ``labels`` is
    the raw Gmail label IDs list (e.g. ``["IMPORTANT", "UNREAD", "INBOX"]``)
    so the adapter can derive UI flags without re-parsing.
    """

    id: str
    thread_id: str
    from_name: str
    from_email: str
    received_at: datetime
    subject: str
    snippet: str
    labels: list[str] = field(default_factory=list)
    attachments: list[Attachment] = field(default_factory=list)


# --- Gmail payload -> EmailMessage --------------------------------------------


def _header(headers: list[dict[str, Any]], name: str) -> str:
    """Return the first header value matching ``name`` (case-insensitive).

    Gmail's ``payload.headers`` is a list of ``{"name": ..., "value": ...}``
    dicts; the same header can appear multiple times (esp. ``Received``).
    We return the first match; absent header returns ``""``.
    """

    lowered = name.lower()
    for header in headers:
        if str(header.get("name", "")).lower() == lowered:
            value = header.get("value")
            if isinstance(value, str):
                return value
    return ""


def _parse_from(raw_from: str) -> tuple[str, str]:
    """Split an RFC 5322 ``From:`` value into ``(display_name, email)``.

    Falls back to (empty name, raw value) when parsing yields no address —
    keeps the connector resilient to oddly-formatted senders.
    """

    name, email = parseaddr(raw_from)
    if not email and not name:
        return ("", raw_from)
    return (name, email)


def _parse_received_at(headers: list[dict[str, Any]], fallback_ms: int | None) -> datetime:
    """Resolve the message timestamp.

    Prefers the ``Date`` header (RFC 5322). If that's missing or unparseable,
    falls back to Gmail's ``internalDate`` (epoch ms). If both are missing
    returns ``datetime.fromtimestamp(0, tz=UTC)`` — the dataclass field
    stays well-typed even on degenerate payloads.
    """

    raw = _header(headers, "Date")
    if raw:
        try:
            dt = parsedate_to_datetime(raw)
        except (TypeError, ValueError):
            dt = None
        if dt is not None:
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=UTC)
            return dt.astimezone(UTC)
    if fallback_ms is not None:
        return datetime.fromtimestamp(fallback_ms / 1000, tz=UTC)
    return datetime.fromtimestamp(0, tz=UTC)


def _collect_attachments(payload: dict[str, Any]) -> list[Attachment]:
    """Walk Gmail's MIME tree and collect attachment parts.

    A part is considered an attachment when it has both a non-empty filename
    and an ``attachmentId``. Inline parts without an attachmentId (e.g. the
    text/plain or text/html body) are skipped.
    """

    out: list[Attachment] = []

    def walk(part: dict[str, Any]) -> None:
        filename = part.get("filename") or ""
        body = part.get("body") or {}
        attachment_id = body.get("attachmentId")
        if filename and attachment_id:
            out.append(
                Attachment(
                    filename=str(filename),
                    size_bytes=int(body.get("size") or 0),
                    mime_type=str(part.get("mimeType") or "application/octet-stream"),
                    attachment_id=str(attachment_id),
                )
            )
        for child in part.get("parts") or []:
            if isinstance(child, dict):
                walk(child)

    walk(payload)
    return out


def _decode_subject(raw: str) -> str:
    """Decode an RFC 2047 encoded-word ``Subject:`` header.

    Gmail typically delivers already-decoded UTF-8 in ``Subject``, but if the
    value still carries ``=?utf-8?B?...?=`` segments we decode them so the
    UI never has to deal with mojibake. Best-effort — on failure we return
    the raw input.
    """

    if not raw or "=?" not in raw:
        return raw
    try:
        from email.header import decode_header, make_header

        return str(make_header(decode_header(raw)))
    except Exception:
        return raw


def from_gmail_payload(payload: dict[str, Any]) -> EmailMessage:
    """Translate a Gmail ``messages.get`` (full format) JSON dict.

    Pure factory — no I/O, no globals. The only required keys are ``id``
    and ``threadId``; everything else degrades gracefully so callers do not
    need to special-case messages with missing headers or no body parts.
    """

    msg_id = str(payload.get("id") or "")
    thread_id = str(payload.get("threadId") or "")
    snippet = str(payload.get("snippet") or "")

    labels_raw = payload.get("labelIds") or []
    labels: list[str] = [str(lbl) for lbl in labels_raw if isinstance(lbl, str)]

    msg_payload = payload.get("payload") or {}
    headers_raw = msg_payload.get("headers") or []
    headers: list[dict[str, Any]] = [h for h in headers_raw if isinstance(h, dict)]

    from_name, from_email = _parse_from(_header(headers, "From"))
    subject = _decode_subject(_header(headers, "Subject"))

    internal_date = payload.get("internalDate")
    fallback_ms: int | None
    if internal_date is None:
        fallback_ms = None
    else:
        try:
            fallback_ms = int(internal_date)
        except (TypeError, ValueError):
            fallback_ms = None
    received_at = _parse_received_at(headers, fallback_ms)

    attachments = _collect_attachments(msg_payload)

    return EmailMessage(
        id=msg_id,
        thread_id=thread_id,
        from_name=from_name,
        from_email=from_email,
        received_at=received_at,
        subject=subject,
        snippet=snippet,
        labels=labels,
        attachments=attachments,
    )


# --- EmailMessage -> Mail UI props --------------------------------------------


_FLAG_BY_LABEL = {
    "IMPORTANT": "priority",
    "UNREAD": "unread",
    "STARRED": "starred",
}


def _derive_flags(labels: list[str]) -> list[str]:
    """Map Gmail labels to the ``Mail`` component's ``flags`` enum.

    Preserves the enum order (``priority`` -> ``unread`` -> ``starred``) so
    the UI gets a deterministic chip order regardless of how labels come
    back from Gmail.
    """

    label_set = set(labels)
    out: list[str] = []
    for label, flag in _FLAG_BY_LABEL.items():
        if label in label_set:
            out.append(flag)
    return out


def to_mail_props(msg: EmailMessage, account_index: int = 0) -> dict[str, Any]:
    """Build the props dict for the ``Mail`` UI component.

    The shape mirrors the JSON schema in :func:`bob.ui_registry.build_registry`
    (issue 0053): ``from`` carries ``name`` + ``email`` (no ``role`` in MVP),
    ``receivedAt`` is ISO 8601 UTC, ``flags`` is derived from ``labels``, and
    ``gmailWebUrl`` is pre-built so the frontend stays ignorant of Gmail URL
    patterns.
    """

    attachments_props = [
        {
            "name": att.filename,
            "sizeBytes": att.size_bytes,
            "mime": att.mime_type,
        }
        for att in msg.attachments
    ]

    return {
        "from": {"name": msg.from_name, "email": msg.from_email},
        "receivedAt": msg.received_at.astimezone(UTC).isoformat().replace("+00:00", "Z"),
        "subject": msg.subject,
        "bodyPreview": msg.snippet,
        "flags": _derive_flags(msg.labels),
        "attachments": attachments_props,
        "threadId": msg.thread_id,
        "messageId": msg.id,
        "gmailWebUrl": (f"https://mail.google.com/mail/u/{account_index}/#inbox/{msg.thread_id}"),
    }


__all__ = [
    "Attachment",
    "EmailMessage",
    "from_gmail_payload",
    "to_mail_props",
]
