"""Tests for :class:`bob.context.providers.recent_turns.RecentTurnsProvider`."""

from __future__ import annotations

import sqlite3

from bob.context.policy import bounded_v1_policy, parse_policy_overrides
from bob.context.provider import AssemblyContext
from bob.context.providers.recent_turns import (
    DEFAULT_RECENT_TURNS_WINDOW,
    RECENT_TURNS_PROVIDER_ID,
    RecentTurnsProvider,
)
from bob.db.migrations_runner import apply_migrations, default_migrations_dir
from bob.jarvis_store import JarvisStore


def _make_store() -> JarvisStore:
    conn = sqlite3.connect(":memory:")
    apply_migrations(conn, default_migrations_dir())
    return JarvisStore(conn)


def _seed_pairs(store: JarvisStore, count: int) -> None:
    """Append ``count`` user↔assistant pairs to ``store``."""

    for idx in range(count):
        store.append("user", f"u{idx}")
        store.append("assistant", f"a{idx}")


def test_provider_id_is_stable() -> None:
    provider = RecentTurnsProvider(jarvis_store=_make_store())
    assert provider.provider_id == RECENT_TURNS_PROVIDER_ID


def test_emits_empty_when_history_empty() -> None:
    provider = RecentTurnsProvider(jarvis_store=_make_store())
    ctx = AssemblyContext(policy=bounded_v1_policy())
    assert list(provider.entries(ctx)) == []


def test_respects_recent_turns_window_from_policy() -> None:
    store = _make_store()
    _seed_pairs(store, 10)
    # Live user turn appended by the orchestrator before assembly.
    store.append("user", "live")

    policy = parse_policy_overrides(
        base=bounded_v1_policy(),
        recent_turns_window=2,
    )
    provider = RecentTurnsProvider(jarvis_store=store)
    ctx = AssemblyContext(policy=policy, user_message="live")
    entries = list(provider.entries(ctx))

    # K=2 → 2 user/assistant pairs = 4 rows; trailing user is dropped.
    assert [e.payload["role"] for e in entries] == [
        "user",
        "assistant",
        "user",
        "assistant",
    ]
    # Chronological order — oldest first within the window.
    assert [e.payload["content"] for e in entries] == ["u8", "a8", "u9", "a9"]


def test_default_window_when_policy_field_missing() -> None:
    store = _make_store()
    _seed_pairs(store, 5)
    store.append("user", "live")

    policy = parse_policy_overrides(
        base=bounded_v1_policy(),
        recent_turns_window=None,
    )
    # Force the field to ``None`` explicitly so the default branch fires.
    object.__setattr__(policy, "recent_turns_window", None)

    provider = RecentTurnsProvider(jarvis_store=store)
    entries = list(provider.entries(AssemblyContext(policy=policy)))

    # Default window = 3 → 6 rows.
    assert len(entries) == 2 * DEFAULT_RECENT_TURNS_WINDOW


def test_includes_live_user_when_flag_set() -> None:
    store = _make_store()
    _seed_pairs(store, 1)
    store.append("user", "live")

    provider = RecentTurnsProvider(jarvis_store=store, include_live_user_message=True)
    policy = parse_policy_overrides(
        base=bounded_v1_policy(),
        recent_turns_window=5,
    )
    entries = list(provider.entries(AssemblyContext(policy=policy)))

    contents = [e.payload["content"] for e in entries]
    assert "live" in contents


def test_older_history_helper_returns_non_recent_rows() -> None:
    store = _make_store()
    _seed_pairs(store, 5)  # 10 rows
    store.append("user", "live")  # +1 trailing user

    provider = RecentTurnsProvider(jarvis_store=store)
    older = provider.older_history(window=2)  # 4 recent rows kept

    # Trailing user dropped + 4 recent rows kept → 10 - 4 = 6 older rows.
    assert len(older) == 6
    assert [msg["content"] for msg in older] == [
        "u0",
        "a0",
        "u1",
        "a1",
        "u2",
        "a2",
    ]


def test_older_history_empty_when_history_shorter_than_window() -> None:
    store = _make_store()
    _seed_pairs(store, 2)
    store.append("user", "live")

    provider = RecentTurnsProvider(jarvis_store=store)
    older = provider.older_history(window=3)
    assert older == []


def test_token_estimate_populated() -> None:
    store = _make_store()
    store.append("user", "x" * 80)
    store.append("assistant", "y" * 40)
    store.append("user", "live")

    provider = RecentTurnsProvider(jarvis_store=store)
    entries = list(provider.entries(AssemblyContext(policy=bounded_v1_policy())))
    assert entries[0].token_estimate == 80 // 4
    assert entries[1].token_estimate == 40 // 4
