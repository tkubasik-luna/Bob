"""Tests for :class:`bob.context.providers.legacy_full_history.LegacyFullHistoryProvider`.

The provider is the byte-for-byte reproduction of the pre-0043 prompt
assembly. The golden snapshot here is the single most important guard
during the v2 overhaul — every later refactor must keep this passing
until the legacy provider is removed entirely (planned in slice 0046+).
"""

from __future__ import annotations

import sqlite3
from typing import Any

from bob.context.assembler import ContextAssembler
from bob.context.entry import CONTEXT_ENTRY_SCHEMA_VERSION
from bob.context.policy import legacy_full_history_policy
from bob.context.provider import AssemblyContext
from bob.context.providers.legacy_full_history import (
    LEGACY_FULL_HISTORY_PROVIDER_ID,
    LegacyFullHistoryProvider,
)
from bob.db.migrations_runner import apply_migrations, default_migrations_dir
from bob.jarvis_store import JarvisStore

from ._harness.golden_prompt import (
    assert_matches_snapshot,
    load_transcript_fixture,
    seed_history,
)


def _make_store() -> tuple[JarvisStore, sqlite3.Connection]:
    conn = sqlite3.connect(":memory:")
    apply_migrations(conn, default_migrations_dir())
    return JarvisStore(conn), conn


def _legacy_messages_for(
    *, system_content: str, history: list[dict[str, str]]
) -> list[dict[str, Any]]:
    """Reproduce the pre-0043 inline message list construction in the orchestrator.

    This mirrors lines 339-342 of the pre-0043 orchestrator exactly:

        [{"role": "system", "content": complete_system},
         *({"role": m["role"], "content": m["content"]} for m in history)]
    """

    return [
        {"role": "system", "content": system_content},
        *({"role": m["role"], "content": m["content"]} for m in history),
    ]


def test_provider_id_is_stable() -> None:
    store, _conn = _make_store()
    provider = LegacyFullHistoryProvider(jarvis_store=store, system_content="sys")
    assert provider.provider_id == LEGACY_FULL_HISTORY_PROVIDER_ID


def test_provider_emits_system_then_history_entries() -> None:
    store, _conn = _make_store()
    store.append("user", "Salut")
    store.append("assistant", "Bonjour")

    provider = LegacyFullHistoryProvider(jarvis_store=store, system_content="SYS")
    entries = list(provider.entries(AssemblyContext(policy=legacy_full_history_policy())))

    assert [e.payload["role"] for e in entries] == ["system", "user", "assistant"]
    assert [e.payload["content"] for e in entries] == ["SYS", "Salut", "Bonjour"]
    assert [e.kind for e in entries] == ["system_note", "user_turn", "assistant_turn"]
    assert all(e.provider_id == LEGACY_FULL_HISTORY_PROVIDER_ID for e in entries)
    assert all(e.schema_version == CONTEXT_ENTRY_SCHEMA_VERSION for e in entries)


def test_provider_pins_system_entry_only() -> None:
    store, _conn = _make_store()
    store.append("user", "x")
    provider = LegacyFullHistoryProvider(jarvis_store=store, system_content="SYS")
    entries = list(provider.entries(AssemblyContext(policy=legacy_full_history_policy())))
    pinned = [e.pinned for e in entries]
    assert pinned == [True, False]


def test_assembled_prompt_matches_pre_0043_inline_construction() -> None:
    """Byte-for-byte equivalence with the pre-0043 orchestrator code path.

    This is the acceptance-criterion guard: instrument both the old and new
    paths against the same transcript and assert the chat-message lists are
    identical.
    """

    transcript = load_transcript_fixture("simple_two_turns")
    system_content = transcript["system_content"]

    store, _conn = _make_store()
    seed_history(store, transcript)

    # New path: assembler-driven.
    provider = LegacyFullHistoryProvider(jarvis_store=store, system_content=system_content)
    assembler = ContextAssembler(
        providers=[provider],
        policy=legacy_full_history_policy(),
    )
    new_path = assembler.assemble(user_message=transcript["pending_user_message"])

    # Old path: reproduce inline construction in tests directly.
    old_history = [
        *transcript["history"],
        {"role": "user", "content": transcript["pending_user_message"]},
    ]
    legacy_path = _legacy_messages_for(system_content=system_content, history=old_history)

    assert new_path == legacy_path


def test_golden_snapshot_simple_two_turns() -> None:
    """Snapshot test pinning the exact assembled prompt for ``simple_two_turns``."""

    transcript = load_transcript_fixture("simple_two_turns")
    store, _conn = _make_store()
    seed_history(store, transcript)

    provider = LegacyFullHistoryProvider(
        jarvis_store=store, system_content=transcript["system_content"]
    )
    assembler = ContextAssembler(
        providers=[provider],
        policy=legacy_full_history_policy(),
    )
    messages = assembler.assemble(user_message=transcript["pending_user_message"])

    assert_matches_snapshot(messages, "simple_two_turns")


def test_empty_history_still_emits_system_entry() -> None:
    """First-ever turn: only the system entry is emitted."""

    store, _conn = _make_store()
    provider = LegacyFullHistoryProvider(jarvis_store=store, system_content="bootstrap")
    entries = list(provider.entries(AssemblyContext(policy=legacy_full_history_policy())))
    assert len(entries) == 1
    assert entries[0].kind == "system_note"
    assert entries[0].payload == {"role": "system", "content": "bootstrap"}


def test_token_estimate_is_populated_per_entry() -> None:
    store, _conn = _make_store()
    long_message = "x" * 400
    store.append("user", long_message)
    provider = LegacyFullHistoryProvider(jarvis_store=store, system_content="sys")
    entries = list(provider.entries(AssemblyContext(policy=legacy_full_history_policy())))

    # ``len(content) // 4`` is the agreed-upon rough heuristic shared with
    # ``bob.llm_client._estimate_tokens``. Assert the field is populated;
    # later slices will swap in a real tokenizer.
    assert entries[1].token_estimate == len(long_message) // 4
    assert entries[0].token_estimate == len("sys") // 4
