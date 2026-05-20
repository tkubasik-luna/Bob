"""Tests for :mod:`bob.ui_registry`."""

from __future__ import annotations

import pytest
from jsonschema import Draft202012Validator

from bob import ui_registry
from bob.ui_registry import (
    ResponseSchemaError,
    UIComponent,
    build_registry,
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
    assert names == {"ChatMessage", "Markdown"}


def test_components_description_lists_v0_components() -> None:
    text = ui_registry.get_components_description_for_prompt()
    assert "ChatMessage" in text
    assert "Markdown" in text
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
    assert names == {"ChatMessage", "Markdown", "Banner"}

    payload: dict[str, object] = {
        "speech": "hi",
        "ui": [{"component": "Banner", "props": {"text": "hello"}}],
    }
    parsed = registry.validate_response(payload)
    assert parsed.ui[0].component == "Banner"
