"""Unit tests for ``project_gmail_search`` (PRD 0009 / P2).

The projector is the deterministic replacement for the prose recipe that asked
a weak model to hand-build the Mail card. These tests lock:

- the structured deliverable is built from ``messages[0]`` and VALIDATES against
  the single ``ui_registry`` Mail schema (no second Mail schema drift);
- the transcript digest is compact and body-free (0056 privacy + context saving);
- a mail lookup is always ``terminal`` (single-shot → runner may converge);
- empty / malformed results degrade safely.
"""

from __future__ import annotations

from typing import Any

from bob.sub_agent.tool_registry import build_gmail_search_tool, project_gmail_search
from bob.ui_registry import validate_component_descriptor


def _mail_props(subject: str = "KiLi DEV 2.9.0", sender: str = "TestFlight") -> dict[str, Any]:
    """A valid ``Mail`` props dict, shaped exactly like ``to_mail_props`` output."""

    return {
        "from": {"name": sender, "email": "noreply@example.com"},
        "receivedAt": "2026-05-29T15:04:47Z",
        "subject": subject,
        "bodyPreview": "KiLi DEV 2.9.0 (205) is ready to test on iOS. …",
        "flags": [],
        "attachments": [],
        "threadId": "19e744436f754b1f",
        "messageId": "19e744436f754b1f",
        "gmailWebUrl": "https://mail.google.com/mail/u/0/#inbox/19e744436f754b1f",
    }


def test_non_empty_result_builds_valid_mail_deliverable() -> None:
    result = {"query": "label:INBOX", "count": 1, "messages": [_mail_props()]}
    proj = project_gmail_search(result)

    # PRD 0010 / issue 0066 — the deliverable is a LIST of section descriptors
    # (a single Mail card is a list-of-one).
    assert proj.deliverable == [{"component": "Mail", "props": _mail_props()}]
    assert proj.terminal is True
    # The decisive cross-check: the card the runner will ship validates against
    # the SAME ui_registry schema the `say` tool uses — no drift, no second
    # hand-written Mail schema.
    assert proj.deliverable is not None
    assert validate_component_descriptor(proj.deliverable[0]) == []
    # Summary is deterministic and mentions the count + subject.
    assert "1 email" in proj.summary
    assert "KiLi DEV 2.9.0" in proj.summary


def test_digest_is_compact_and_body_free() -> None:
    result = {"query": "label:INBOX", "count": 1, "messages": [_mail_props()]}
    proj = project_gmail_search(result)

    # 0056 + PRD 0009 — the body must never enter the transcript digest.
    assert "bodyPreview" not in proj.digest["messages"][0]
    assert "attachments" not in proj.digest["messages"][0]
    assert "messageId" not in proj.digest["messages"][0]
    # Only the light fields survive.
    assert proj.digest["messages"][0] == {
        "subject": "KiLi DEV 2.9.0",
        "receivedAt": "2026-05-29T15:04:47Z",
        "from": "TestFlight",
    }
    assert proj.digest["count"] == 1
    assert proj.digest["query"] == "label:INBOX"
    # The body is NOT lost — it is retained in the deliverable (server-side).
    assert proj.deliverable is not None
    assert proj.deliverable[0]["props"]["bodyPreview"].startswith("KiLi DEV 2.9.0")


def test_digest_caps_message_count() -> None:
    msgs = [_mail_props(subject=f"mail {i}") for i in range(12)]
    proj = project_gmail_search({"query": "x", "count": 12, "messages": msgs})
    assert len(proj.digest["messages"]) == 5
    # But the count is reported faithfully.
    assert proj.digest["count"] == 12


def test_empty_result_has_no_deliverable_but_is_terminal() -> None:
    proj = project_gmail_search({"query": "from:nobody", "count": 0, "messages": []})
    assert proj.deliverable is None
    assert proj.terminal is True
    assert "Aucun email" in proj.summary
    assert proj.digest["count"] == 0


def test_malformed_result_does_not_crash() -> None:
    # messages is not a list / count missing / message not a dict — the
    # projector must degrade, never raise (the store also guards, but the
    # projector should be total on its own).
    proj = project_gmail_search({"messages": "oops"})
    assert proj.deliverable is None
    assert proj.terminal is True
    proj2 = project_gmail_search({"count": 1, "messages": ["not-a-dict"]})
    # count claims 1 but the message is unusable → no deliverable.
    assert proj2.deliverable is None


def test_missing_subject_falls_back_gracefully() -> None:
    msg = _mail_props()
    del msg["subject"]
    proj = project_gmail_search({"count": 1, "messages": [msg]})
    assert proj.deliverable is not None
    assert "(sans objet)" in proj.summary


def test_builder_wires_the_projector() -> None:
    tool = build_gmail_search_tool()
    assert tool.result_projector is project_gmail_search
