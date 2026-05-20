"""Tests for :mod:`bob.conversation`."""

from __future__ import annotations

from bob.conversation import ConversationStore


def test_get_history_unknown_session_returns_empty() -> None:
    store = ConversationStore()
    assert store.get_history("nope") == []


def test_clear_unknown_session_is_no_op() -> None:
    store = ConversationStore()
    store.clear("does-not-exist")  # must not raise
    assert store.get_history("does-not-exist") == []


def test_append_and_get_history_returns_ordered() -> None:
    store = ConversationStore()
    store.append("s1", "user", "hi")
    store.append("s1", "assistant", "hello")
    store.append("s1", "user", "how are you")

    history = store.get_history("s1")
    assert history == [
        {"role": "user", "content": "hi"},
        {"role": "assistant", "content": "hello"},
        {"role": "user", "content": "how are you"},
    ]


def test_sessions_are_isolated() -> None:
    store = ConversationStore()
    store.append("a", "user", "in-a")
    store.append("b", "user", "in-b")

    assert store.get_history("a") == [{"role": "user", "content": "in-a"}]
    assert store.get_history("b") == [{"role": "user", "content": "in-b"}]


def test_get_history_returns_copy() -> None:
    store = ConversationStore()
    store.append("s", "user", "hi")
    snapshot = store.get_history("s")
    snapshot.append({"role": "assistant", "content": "tampered"})
    assert store.get_history("s") == [{"role": "user", "content": "hi"}]


def test_clear_drops_session() -> None:
    store = ConversationStore()
    store.append("s", "user", "hi")
    store.clear("s")
    assert store.get_history("s") == []
