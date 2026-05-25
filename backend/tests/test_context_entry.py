"""Tests for :mod:`bob.context.entry`."""

from __future__ import annotations

from bob.context.entry import (
    CONTEXT_ENTRY_SCHEMA_VERSION,
    ContextEntry,
)


def test_schema_version_constant_is_one() -> None:
    assert CONTEXT_ENTRY_SCHEMA_VERSION == 1


def test_context_entry_carries_full_field_set() -> None:
    entry = ContextEntry(
        id="hist:0",
        kind="user_turn",
        source="jarvis_store",
        token_estimate=12,
        pinned=False,
        created_at="2026-05-25T10:00:00+00:00",
        provider_id="legacy_full_history",
        payload={"role": "user", "content": "hi"},
    )

    assert entry.id == "hist:0"
    assert entry.kind == "user_turn"
    assert entry.source == "jarvis_store"
    assert entry.token_estimate == 12
    assert entry.pinned is False
    assert entry.created_at == "2026-05-25T10:00:00+00:00"
    assert entry.provider_id == "legacy_full_history"
    assert entry.payload == {"role": "user", "content": "hi"}
    assert entry.schema_version == CONTEXT_ENTRY_SCHEMA_VERSION


def test_context_entry_defaults_schema_version_to_constant() -> None:
    entry = ContextEntry(
        id="x",
        kind="assistant_turn",
        source="anywhere",
        token_estimate=0,
        pinned=True,
        created_at="",
        provider_id="x",
    )
    assert entry.schema_version == CONTEXT_ENTRY_SCHEMA_VERSION
    # Default payload is an empty dict (independent copies per instance).
    assert entry.payload == {}


def test_context_entry_is_frozen() -> None:
    """Immutability is part of the contract — providers emit, assemblers read."""

    entry = ContextEntry(
        id="x",
        kind="user_turn",
        source="s",
        token_estimate=0,
        pinned=False,
        created_at="",
        provider_id="p",
    )
    try:
        entry.id = "y"  # type: ignore[misc]
    except Exception:
        return
    raise AssertionError("ContextEntry must be frozen but allowed attribute mutation")
