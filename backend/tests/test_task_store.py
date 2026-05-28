"""Tests for :mod:`bob.task_store`."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from bob.db.migrations_runner import apply_migrations, default_migrations_dir
from bob.task_store import (
    TaskStore,
    TaskStoreError,
    _decode_result_payload,
)


def _make_store_in_memory() -> tuple[TaskStore, sqlite3.Connection]:
    conn = sqlite3.connect(":memory:")
    apply_migrations(conn, default_migrations_dir())
    return TaskStore(conn), conn


@pytest.fixture()
def fresh_task_store() -> TaskStore:
    """In-memory TaskStore with migrations applied — fast happy path."""

    store, _conn = _make_store_in_memory()
    return store


def test_create_task_inserts_pending_state(fresh_task_store: TaskStore) -> None:
    task_id = fresh_task_store.create_task(title="Research X", goal="find Y")

    task = fresh_task_store.get_task(task_id)
    assert task.id == task_id
    assert task.title == "Research X"
    assert task.goal == "find Y"
    assert task.state == "pending"
    assert task.needs_attention is False
    assert task.result is None
    assert task.parent_task_id is None
    assert task.created_at  # populated by SQL DEFAULT
    assert task.updated_at


def test_create_task_returns_distinct_ids(fresh_task_store: TaskStore) -> None:
    a = fresh_task_store.create_task(title="A", goal="ga")
    b = fresh_task_store.create_task(title="B", goal="gb")
    assert a != b


def test_create_task_with_parent_records_parent_id(fresh_task_store: TaskStore) -> None:
    parent_id = fresh_task_store.create_task(title="Parent", goal="g")
    child_id = fresh_task_store.create_task(
        title="Child",
        goal="c",
        parent_task_id=parent_id,
    )

    assert fresh_task_store.get_task(child_id).parent_task_id == parent_id


def test_get_task_unknown_raises(fresh_task_store: TaskStore) -> None:
    with pytest.raises(TaskStoreError):
        fresh_task_store.get_task("missing-id")


def test_list_tasks_returns_in_creation_order(fresh_task_store: TaskStore) -> None:
    a = fresh_task_store.create_task(title="A", goal="ga")
    b = fresh_task_store.create_task(title="B", goal="gb")
    c = fresh_task_store.create_task(title="C", goal="gc")

    ids = [task.id for task in fresh_task_store.list_tasks()]
    assert ids == [a, b, c]


def test_list_tasks_filters_by_state(fresh_task_store: TaskStore) -> None:
    a = fresh_task_store.create_task(title="A", goal="ga")
    fresh_task_store.create_task(title="B", goal="gb")

    fresh_task_store.update_state(a, "running")

    pending_ids = [task.id for task in fresh_task_store.list_tasks(state="pending")]
    running_ids = [task.id for task in fresh_task_store.list_tasks(state="running")]
    done_ids = [task.id for task in fresh_task_store.list_tasks(state="done")]

    assert a in running_ids
    assert a not in pending_ids
    assert len(pending_ids) == 1
    assert done_ids == []


def test_list_tasks_respects_limit(fresh_task_store: TaskStore) -> None:
    for i in range(5):
        fresh_task_store.create_task(title=f"T{i}", goal=f"g{i}")

    assert len(fresh_task_store.list_tasks(limit=2)) == 2
    assert len(fresh_task_store.list_tasks(limit=10)) == 5


def test_find_by_query_single_token_matches_title(
    fresh_task_store: TaskStore,
) -> None:
    pizza_id = fresh_task_store.create_task(
        title="Approfondissement Bitcoin Pizza Day", goal="story of 10k BTC"
    )
    fresh_task_store.create_task(title="Révolution française en 5 points", goal="dates clés")

    matches = fresh_task_store.find_by_query("pizza")
    assert [t.id for t in matches] == [pizza_id]


def test_find_by_query_multi_token_match_is_case_insensitive(
    fresh_task_store: TaskStore,
) -> None:
    target = fresh_task_store.create_task(
        title="Exposé Révolution Française", goal="dates clés et acteurs"
    )
    fresh_task_store.create_task(title="Bitcoin", goal="generic crypto")

    # Tokens spread across title (Révolution) + goal (acteurs) — every
    # token must hit somewhere in ``title || ' ' || goal``.
    matches = fresh_task_store.find_by_query("RÉVOLUTION acteurs")
    assert [t.id for t in matches] == [target]


def test_find_by_query_no_match_returns_empty(
    fresh_task_store: TaskStore,
) -> None:
    fresh_task_store.create_task(title="Pizza", goal="day")

    assert fresh_task_store.find_by_query("révolution française") == []


def test_find_by_query_blank_query_returns_empty(
    fresh_task_store: TaskStore,
) -> None:
    fresh_task_store.create_task(title="Pizza Day", goal="…")

    assert fresh_task_store.find_by_query("") == []
    assert fresh_task_store.find_by_query("   ") == []


def test_find_by_query_prefer_state_ranks_done_first(
    fresh_task_store: TaskStore,
) -> None:
    running_id = fresh_task_store.create_task(title="Pizza Day", goal="…")
    fresh_task_store.update_state(running_id, "running")

    done_id = fresh_task_store.create_task(title="Pizza Day", goal="…")
    fresh_task_store.update_state(done_id, "running")
    fresh_task_store.update_state(done_id, "done")

    matches = fresh_task_store.find_by_query("pizza", prefer_state="done", limit=2)
    # The done row wins the rank even though the running row was inserted
    # first (created_at-ASC would otherwise put it on top).
    assert matches[0].id == done_id
    assert matches[1].id == running_id


def test_find_by_query_delivered_at_turn_breaks_ties(
    fresh_task_store: TaskStore,
) -> None:
    old = fresh_task_store.create_task(title="Pizza Day", goal="…")
    fresh_task_store.update_state(old, "running")
    fresh_task_store.update_state(old, "done")
    fresh_task_store.set_delivered_at_turn(old, 12)

    new = fresh_task_store.create_task(title="Pizza Day", goal="…")
    fresh_task_store.update_state(new, "running")
    fresh_task_store.update_state(new, "done")
    fresh_task_store.set_delivered_at_turn(new, 47)

    matches = fresh_task_store.find_by_query("pizza", limit=2)
    assert matches[0].id == new
    assert matches[1].id == old


def test_find_by_query_excludes_dismissed(fresh_task_store: TaskStore) -> None:
    visible = fresh_task_store.create_task(title="Pizza Day", goal="…")
    dismissed = fresh_task_store.create_task(title="Pizza Day", goal="…")
    fresh_task_store.dismiss_task(dismissed)

    matches = fresh_task_store.find_by_query("pizza", limit=5)
    assert [t.id for t in matches] == [visible]


def test_find_by_query_respects_limit(fresh_task_store: TaskStore) -> None:
    for _ in range(4):
        fresh_task_store.create_task(title="Pizza Day", goal="…")

    assert len(fresh_task_store.find_by_query("pizza", limit=2)) == 2


def test_find_by_query_escapes_like_wildcards(
    fresh_task_store: TaskStore,
) -> None:
    # A literal ``%`` in the stored title is matched only by a literal ``%``
    # in the query — the wildcard must NOT be interpreted as a glob.
    plain = fresh_task_store.create_task(title="Pizza Day", goal="…")
    fresh_task_store.create_task(title="100% real", goal="…")

    # ``%`` here is escaped → searches for the literal char. Only the
    # "100% real" row contains it.
    literal_pct = fresh_task_store.find_by_query("%")
    assert all(t.id != plain for t in literal_pct)


def test_valid_transition_chain(fresh_task_store: TaskStore) -> None:
    """pending → running → waiting_input → running → done."""

    task_id = fresh_task_store.create_task(title="T", goal="g")

    fresh_task_store.update_state(task_id, "running")
    assert fresh_task_store.get_task(task_id).state == "running"

    fresh_task_store.update_state(task_id, "waiting_input")
    assert fresh_task_store.get_task(task_id).state == "waiting_input"

    fresh_task_store.update_state(task_id, "running")
    assert fresh_task_store.get_task(task_id).state == "running"

    fresh_task_store.update_state(task_id, "done")
    assert fresh_task_store.get_task(task_id).state == "done"


def test_invalid_transition_pending_to_done_raises(fresh_task_store: TaskStore) -> None:
    task_id = fresh_task_store.create_task(title="T", goal="g")
    with pytest.raises(TaskStoreError):
        fresh_task_store.update_state(task_id, "done")


def test_invalid_transition_done_to_running_raises(fresh_task_store: TaskStore) -> None:
    """Terminal states have no outgoing transitions."""

    task_id = fresh_task_store.create_task(title="T", goal="g")
    fresh_task_store.update_state(task_id, "running")
    fresh_task_store.update_state(task_id, "done")

    with pytest.raises(TaskStoreError):
        fresh_task_store.update_state(task_id, "running")


def test_invalid_transition_failed_terminal(fresh_task_store: TaskStore) -> None:
    task_id = fresh_task_store.create_task(title="T", goal="g")
    fresh_task_store.update_state(task_id, "failed")

    with pytest.raises(TaskStoreError):
        fresh_task_store.update_state(task_id, "running")


def test_update_state_unknown_task_raises(fresh_task_store: TaskStore) -> None:
    with pytest.raises(TaskStoreError):
        fresh_task_store.update_state("missing-id", "running")


def test_update_state_bumps_updated_at(fresh_task_store: TaskStore) -> None:
    """updated_at must advance (or stay equal) — never go backwards."""

    task_id = fresh_task_store.create_task(title="T", goal="g")
    before = fresh_task_store.get_task(task_id).updated_at

    fresh_task_store.update_state(task_id, "running")
    after = fresh_task_store.get_task(task_id).updated_at

    # ``datetime('now')`` is second-precision so they may be equal in a tight
    # loop — what matters is "not before".
    assert after >= before


def test_set_needs_attention_round_trip(fresh_task_store: TaskStore) -> None:
    task_id = fresh_task_store.create_task(title="T", goal="g")
    assert fresh_task_store.get_task(task_id).needs_attention is False

    fresh_task_store.set_needs_attention(task_id, True)
    assert fresh_task_store.get_task(task_id).needs_attention is True

    fresh_task_store.set_needs_attention(task_id, False)
    assert fresh_task_store.get_task(task_id).needs_attention is False


def test_set_needs_attention_unknown_task_raises(fresh_task_store: TaskStore) -> None:
    with pytest.raises(TaskStoreError):
        fresh_task_store.set_needs_attention("missing-id", True)


def test_set_result_persists_value(fresh_task_store: TaskStore) -> None:
    task_id = fresh_task_store.create_task(title="T", goal="g")

    fresh_task_store.set_result(task_id, "X")
    assert fresh_task_store.get_task(task_id).result == "X"


def test_set_result_does_not_change_state(fresh_task_store: TaskStore) -> None:
    task_id = fresh_task_store.create_task(title="T", goal="g")
    fresh_task_store.update_state(task_id, "running")

    fresh_task_store.set_result(task_id, "partial")
    # State still ``running`` — transition is the orchestrator's responsibility.
    assert fresh_task_store.get_task(task_id).state == "running"


def test_set_result_unknown_task_raises(fresh_task_store: TaskStore) -> None:
    with pytest.raises(TaskStoreError):
        fresh_task_store.set_result("missing-id", "X")


def test_set_result_defaults_result_payload_to_none(
    fresh_task_store: TaskStore,
) -> None:
    """PRD 0008 / issue 0064 — no descriptor → ``result_payload`` stays ``None``."""

    task_id = fresh_task_store.create_task(title="T", goal="g")
    fresh_task_store.set_result(task_id, "plain markdown deliverable")
    task = fresh_task_store.get_task(task_id)
    assert task.result == "plain markdown deliverable"
    assert task.result_payload is None


def test_set_result_round_trips_structured_payload(
    fresh_task_store: TaskStore,
) -> None:
    """PRD 0008 / issue 0064 — a ``{component, props}`` descriptor survives a
    set/get round-trip as a Python dict (JSON-text column under the hood)."""

    task_id = fresh_task_store.create_task(title="T", goal="g")
    descriptor: dict[str, object] = {
        "component": "Mail",
        "props": {"messageId": "msg-1", "subject": "Récap réunion produit"},
    }
    fresh_task_store.set_result(task_id, "spoken summary", result_payload=descriptor)
    task = fresh_task_store.get_task(task_id)
    assert task.result == "spoken summary"
    assert task.result_payload == descriptor
    # list_tasks goes through the same SELECT / decode path.
    listed = {t.id: t for t in fresh_task_store.list_tasks()}
    assert listed[task_id].result_payload == descriptor


def test_set_result_clears_stale_payload(fresh_task_store: TaskStore) -> None:
    """Passing ``result_payload=None`` overwrites a previously stored descriptor
    so a stale Mail card never lingers under a later summary-only result."""

    task_id = fresh_task_store.create_task(title="T", goal="g")
    fresh_task_store.set_result(
        task_id,
        "first",
        result_payload={"component": "Mail", "props": {"messageId": "m"}},
    )
    assert fresh_task_store.get_task(task_id).result_payload is not None
    fresh_task_store.set_result(task_id, "second")
    assert fresh_task_store.get_task(task_id).result_payload is None


def test_decode_result_payload_robustness() -> None:
    """The decoder collapses NULL / corrupt / non-object JSON to ``None`` — the
    descriptor is a rendering hint, never load-bearing for task execution."""

    assert _decode_result_payload(None) is None
    assert _decode_result_payload("not json {{{") is None
    assert _decode_result_payload("[1, 2, 3]") is None  # JSON array, not object
    assert _decode_result_payload('"a string"') is None
    assert _decode_result_payload('{"component": "Mail"}') == {"component": "Mail"}


def test_append_message_returns_increasing_ids(fresh_task_store: TaskStore) -> None:
    task_id = fresh_task_store.create_task(title="T", goal="g")

    id_a = fresh_task_store.append_message(task_id, role="user", content="a")
    id_b = fresh_task_store.append_message(task_id, role="assistant", content="b")
    id_c = fresh_task_store.append_message(
        task_id,
        role="assistant",
        content="c",
        action="done",
    )

    assert id_a < id_b < id_c


def test_get_task_messages_preserves_chronological_order(fresh_task_store: TaskStore) -> None:
    task_id = fresh_task_store.create_task(title="T", goal="g")

    fresh_task_store.append_message(task_id, role="user", content="a")
    fresh_task_store.append_message(task_id, role="assistant", content="b", action="progress")
    fresh_task_store.append_message(task_id, role="assistant", content="c", action="done")

    messages = fresh_task_store.get_task_messages(task_id)
    assert [m.content for m in messages] == ["a", "b", "c"]
    assert [m.role for m in messages] == ["user", "assistant", "assistant"]
    assert [m.action for m in messages] == [None, "progress", "done"]
    for msg in messages:
        assert msg.task_id == task_id
        assert msg.created_at


def test_get_task_messages_isolates_by_task_id(fresh_task_store: TaskStore) -> None:
    task_a = fresh_task_store.create_task(title="A", goal="ga")
    task_b = fresh_task_store.create_task(title="B", goal="gb")

    fresh_task_store.append_message(task_a, role="user", content="for-a")
    fresh_task_store.append_message(task_b, role="user", content="for-b")

    assert [m.content for m in fresh_task_store.get_task_messages(task_a)] == ["for-a"]
    assert [m.content for m in fresh_task_store.get_task_messages(task_b)] == ["for-b"]


def test_state_survives_reopen(tmp_path: Path) -> None:
    """A fresh TaskStore on the same DB file sees the prior task + messages."""

    db_path = tmp_path / "bob.db"

    first_conn = sqlite3.connect(db_path)
    apply_migrations(first_conn, default_migrations_dir())
    first = TaskStore(first_conn)
    task_id = first.create_task(title="Persisted", goal="g")
    first.update_state(task_id, "running")
    first.set_needs_attention(task_id, True)
    first.set_result(task_id, "answer-42")
    first.append_message(task_id, role="user", content="hello")
    first.append_message(task_id, role="assistant", content="hi", action="progress")
    first_conn.close()

    second_conn = sqlite3.connect(db_path)
    apply_migrations(second_conn, default_migrations_dir())
    second = TaskStore(second_conn)

    reloaded = second.get_task(task_id)
    assert reloaded.title == "Persisted"
    assert reloaded.goal == "g"
    assert reloaded.state == "running"
    assert reloaded.needs_attention is True
    assert reloaded.result == "answer-42"

    messages = second.get_task_messages(task_id)
    assert [(m.role, m.content, m.action) for m in messages] == [
        ("user", "hello", None),
        ("assistant", "hi", "progress"),
    ]


def test_state_check_constraint_rejects_unknown_state() -> None:
    """SQL CHECK on tasks.state guards against unknown literals at the DB level."""

    _store, conn = _make_store_in_memory()
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO tasks(id, title, goal, state) VALUES (?, ?, ?, ?)",
            ("x", "t", "g", "garbage"),
        )


def test_role_check_constraint_rejects_unknown_role() -> None:
    """SQL CHECK on task_messages.role mirrors the JarvisStore contract."""

    store, conn = _make_store_in_memory()
    task_id = store.create_task(title="T", goal="g")
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO task_messages(task_id, role, content) VALUES (?, ?, ?)",
            (task_id, "garbage", "x"),
        )


def test_dismiss_task_sets_flag_and_filters_from_list(fresh_task_store: TaskStore) -> None:
    """Dismissed tasks drop from ``list_tasks()`` but ``get_task`` keeps them."""

    a = fresh_task_store.create_task(title="A", goal="ga")
    b = fresh_task_store.create_task(title="B", goal="gb")
    fresh_task_store.update_state(a, "running")
    fresh_task_store.update_state(a, "done")

    fresh_task_store.dismiss_task(a)

    # Default list excludes dismissed.
    listed = [task.id for task in fresh_task_store.list_tasks()]
    assert listed == [b]

    # Explicit include surfaces every row.
    full = [task.id for task in fresh_task_store.list_tasks(include_dismissed=True)]
    assert full == [a, b]

    # get_task always works; the flag is exposed for the drawer.
    dismissed_task = fresh_task_store.get_task(a)
    assert dismissed_task.dismissed is True

    not_dismissed = fresh_task_store.get_task(b)
    assert not_dismissed.dismissed is False


def test_dismiss_task_unknown_raises(fresh_task_store: TaskStore) -> None:
    with pytest.raises(TaskStoreError):
        fresh_task_store.dismiss_task("missing-id")


def test_dismiss_task_is_orthogonal_to_state(fresh_task_store: TaskStore) -> None:
    """The data layer accepts dismissal in any state — UI restricts to done/failed."""

    task_id = fresh_task_store.create_task(title="T", goal="g")
    # Still ``pending`` — should not raise.
    fresh_task_store.dismiss_task(task_id)
    assert fresh_task_store.get_task(task_id).dismissed is True


def test_dismissed_flag_survives_reopen(tmp_path: Path) -> None:
    """A fresh TaskStore on the same DB file still sees the dismissed flag."""

    db_path = tmp_path / "bob.db"

    first_conn = sqlite3.connect(db_path)
    apply_migrations(first_conn, default_migrations_dir())
    first = TaskStore(first_conn)
    task_id = first.create_task(title="T", goal="g")
    first.update_state(task_id, "running")
    first.update_state(task_id, "done")
    first.dismiss_task(task_id)
    first_conn.close()

    second_conn = sqlite3.connect(db_path)
    apply_migrations(second_conn, default_migrations_dir())
    second = TaskStore(second_conn)
    assert second.get_task(task_id).dismissed is True
    assert second.list_tasks() == []


def test_get_default_store_raises_before_priming() -> None:
    """Accessing the singleton before boot raises a clear error."""

    from bob import task_store as task_store_module

    previous: TaskStore | None
    try:
        previous = task_store_module.get_default_store()
    except RuntimeError:
        previous = None

    task_store_module.set_default_store(None)
    try:
        with pytest.raises(RuntimeError):
            task_store_module.get_default_store()
    finally:
        task_store_module.set_default_store(previous)
