"""Tests for :mod:`bob.ui_registry`."""

from __future__ import annotations

import pytest
from jsonschema import Draft202012Validator

from bob import ui_registry
from bob.ui_registry import (
    ResponseSchemaError,
    UIComponent,
    build_registry,
    coerce_component_descriptor,
)


def test_response_schema_is_valid_json_schema() -> None:
    wrapped = ui_registry.get_response_schema()
    assert wrapped["name"] == "BobResponse"
    assert wrapped["strict"] is True
    Draft202012Validator.check_schema(wrapped["schema"])


def test_response_schema_contains_v0_components_in_oneof() -> None:
    inner = ui_registry.get_response_schema()["schema"]
    one_of = inner["properties"]["ui"]["items"]["oneOf"]
    names = {variant["properties"]["component"]["const"] for variant in one_of}
    assert names == {"ChatMessage", "Markdown", "Mail"}


def test_components_description_lists_v0_components() -> None:
    text = ui_registry.get_components_description_for_prompt()
    assert "ChatMessage" in text
    assert "Markdown" in text
    assert "Mail" in text
    assert "role" in text
    assert "content" in text


def test_validate_response_accepts_valid_payload() -> None:
    payload = {
        "speech": "Bonjour",
        "ui": [
            {
                "component": "ChatMessage",
                "props": {"role": "assistant", "content": "Bonjour"},
            },
            {"component": "Markdown", "props": {"content": "**hello**"}},
        ],
    }
    parsed = ui_registry.validate_response(payload)
    assert parsed.speech == "Bonjour"
    assert len(parsed.ui) == 2
    assert parsed.ui[0].component == "ChatMessage"
    assert parsed.ui[0].props == {"role": "assistant", "content": "Bonjour"}


def test_validate_response_rejects_unknown_component() -> None:
    payload = {
        "speech": "hi",
        "ui": [{"component": "NotAComponent", "props": {}}],
    }
    with pytest.raises(ResponseSchemaError) as excinfo:
        ui_registry.validate_response(payload)
    assert excinfo.value.errors


def test_validate_response_rejects_missing_props() -> None:
    payload = {
        "speech": "hi",
        "ui": [{"component": "ChatMessage", "props": {"role": "assistant"}}],
    }
    with pytest.raises(ResponseSchemaError):
        ui_registry.validate_response(payload)


def test_validate_response_rejects_missing_speech() -> None:
    payload: dict[str, object] = {"ui": []}
    with pytest.raises(ResponseSchemaError):
        ui_registry.validate_response(payload)


def test_build_registry_supports_extra_components() -> None:
    extra = UIComponent(
        name="Banner",
        description="A banner.",
        props_schema={
            "type": "object",
            "additionalProperties": False,
            "required": ["text"],
            "properties": {"text": {"type": "string"}},
        },
    )
    registry = build_registry(extra_components={extra.name: extra})
    inner = registry.response_schema()["schema"]
    names = {
        variant["properties"]["component"]["const"]
        for variant in inner["properties"]["ui"]["items"]["oneOf"]
    }
    assert names == {"ChatMessage", "Markdown", "Mail", "Banner"}

    payload: dict[str, object] = {
        "speech": "hi",
        "ui": [{"component": "Banner", "props": {"text": "hello"}}],
    }
    parsed = registry.validate_response(payload)
    assert parsed.ui[0].component == "Banner"


def test_coerce_component_descriptor_canonical_shape() -> None:
    cd = coerce_component_descriptor({"component": "Markdown", "props": {"content": "# Hi"}})
    assert cd is not None
    assert cd.component == "Markdown"
    assert cd.props == {"content": "# Hi"}


def test_coerce_component_descriptor_lifts_flat_shape_into_props() -> None:
    # The LLM sometimes emits ``content`` as a sibling of ``component``
    # instead of nesting it under ``props`` — the flat shape that silently
    # dropped the Markdown payload before this fix.
    cd = coerce_component_descriptor({"component": "Markdown", "content": "# Hi"})
    assert cd is not None
    assert cd.component == "Markdown"
    assert cd.props == {"content": "# Hi"}


def test_coerce_component_descriptor_explicit_props_win_over_siblings() -> None:
    cd = coerce_component_descriptor(
        {"component": "Markdown", "content": "stray", "props": {"content": "real"}}
    )
    assert cd is not None
    assert cd.props == {"content": "real"}


@pytest.mark.parametrize("ui", [None, "oops", 42, [], {"props": {"content": "x"}}])
def test_coerce_component_descriptor_returns_none_for_bad_shapes(ui: object) -> None:
    assert coerce_component_descriptor(ui) is None


def test_say_ui_schema_is_valid_and_allows_null_or_component() -> None:
    schema = ui_registry.get_say_ui_schema()
    Draft202012Validator.check_schema(schema)
    validator = Draft202012Validator(schema)
    assert validator.is_valid(None)
    assert validator.is_valid({"component": "Markdown", "props": {"content": "x"}})
    # Flat shape is rejected by the schema (guidance) even though the
    # runtime coerces it — the schema steers the LLM toward ``props``.
    assert not validator.is_valid({"component": "Markdown", "content": "x"})


# ── Mail component ──────────────────────────────────────────────────────────


def _mail_fixture() -> dict[str, object]:
    """Canonical Mail props fixture matching the issue's design mockup
    (Marie Lefèvre, Q3 forecast, 2 attachments, PRIORITY flag). Shared by
    the happy-path test and the negative tests below — each negative case
    mutates one field of the same baseline so a regression is easy to read."""

    return {
        "from": {
            "name": "Marie Lefèvre",
            "email": "marie.lefevre@lunabee.com",
            "role": "CFO · Lunabee",
        },
        "receivedAt": "2026-05-28T14:22:00Z",
        "subject": "Q3 forecast — final review before Thursday",
        "bodyPreview": (
            "Bob, can you have the deck ready by Thursday afternoon? "
            "I want to walk through it with Antoine before the board call."
        ),
        "flags": ["priority"],
        "attachments": [
            {"name": "Q3-forecast-v4.pdf", "sizeBytes": 2_400_000, "mime": "application/pdf"},
            {"name": "Asia-deck-notes.md", "sizeBytes": 18_432, "mime": "text/markdown"},
        ],
        "threadId": "thread-xyz-001",
        "messageId": "msg-xyz-001",
        "gmailWebUrl": ("https://mail.google.com/mail/u/0/#inbox/thread-xyz-001"),
    }


def test_validate_response_accepts_canonical_mail_payload() -> None:
    payload = {
        "speech": "Email from Marie about the Q3 forecast.",
        "ui": [{"component": "Mail", "props": _mail_fixture()}],
    }
    parsed = ui_registry.validate_response(payload)
    assert len(parsed.ui) == 1
    assert parsed.ui[0].component == "Mail"
    assert parsed.ui[0].props["from"]["name"] == "Marie Lefèvre"
    assert parsed.ui[0].props["flags"] == ["priority"]
    assert len(parsed.ui[0].props["attachments"]) == 2


def test_validate_response_accepts_mail_without_optional_fields() -> None:
    """`role` (in `from`), `flags` and `attachments` are all optional."""

    props = _mail_fixture()
    del props["flags"]
    del props["attachments"]
    # `from` is a dict literal in the fixture; cast for mypy then mutate.
    assert isinstance(props["from"], dict)
    props["from"].pop("role", None)

    payload = {
        "speech": "Mail",
        "ui": [{"component": "Mail", "props": props}],
    }
    parsed = ui_registry.validate_response(payload)
    assert parsed.ui[0].component == "Mail"


@pytest.mark.parametrize(
    "missing_field",
    [
        "from",
        "receivedAt",
        "subject",
        "bodyPreview",
        "threadId",
        "messageId",
        "gmailWebUrl",
    ],
)
def test_validate_response_rejects_mail_missing_required_field(missing_field: str) -> None:
    props = _mail_fixture()
    del props[missing_field]
    payload = {
        "speech": "x",
        "ui": [{"component": "Mail", "props": props}],
    }
    with pytest.raises(ResponseSchemaError):
        ui_registry.validate_response(payload)


def test_validate_response_rejects_mail_bad_flags_enum() -> None:
    props = _mail_fixture()
    props["flags"] = ["urgent"]  # not in {priority, unread, starred}
    payload = {
        "speech": "x",
        "ui": [{"component": "Mail", "props": props}],
    }
    with pytest.raises(ResponseSchemaError):
        ui_registry.validate_response(payload)


def test_validate_response_rejects_mail_bad_received_at_format() -> None:
    props = _mail_fixture()
    props["receivedAt"] = "yesterday afternoon"
    payload = {
        "speech": "x",
        "ui": [{"component": "Mail", "props": props}],
    }
    with pytest.raises(ResponseSchemaError):
        ui_registry.validate_response(payload)


def test_validate_response_rejects_mail_from_missing_email() -> None:
    """`from.email` is required even though `role` is not."""

    props = _mail_fixture()
    assert isinstance(props["from"], dict)
    del props["from"]["email"]
    payload = {
        "speech": "x",
        "ui": [{"component": "Mail", "props": props}],
    }
    with pytest.raises(ResponseSchemaError):
        ui_registry.validate_response(payload)


def test_validate_response_rejects_mail_attachment_missing_size() -> None:
    props = _mail_fixture()
    assert isinstance(props["attachments"], list)
    # Drop a required key from the first attachment.
    props["attachments"][0].pop("sizeBytes")
    payload = {
        "speech": "x",
        "ui": [{"component": "Mail", "props": props}],
    }
    with pytest.raises(ResponseSchemaError):
        ui_registry.validate_response(payload)
