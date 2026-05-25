"""Tests for :mod:`bob.debug_log`.

Covers the deep / pure layer: envelope shape, ring buffer, snapshot,
subscribe iterator (snapshot + live), non-blocking overflow strategy.
The WS surface is tested separately in ``test_ws_debug.py``.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Iterator

import pytest
import structlog

from bob import debug_log
from bob.debug_log import (
    DebugEvent,
    clear,
    current_task_id,
    current_turn_id,
    emit_debug,
    install_structlog_bridge,
    snapshot,
    start_task,
    start_turn,
    subscribe,
    subscriber_count,
    uninstall_structlog_bridge,
)


@pytest.fixture(autouse=True)
def _clean_state() -> Iterator[None]:
    """Reset module-level state before and after each test."""

    clear()
    debug_log._subscribers.clear()
    # Slice 0039: also reset the ``current_turn_id`` ContextVar so a leftover
    # turn from a previous test does not leak into the next one.
    current_turn_id.set(None)
    current_task_id.set(None)
    yield
    clear()
    debug_log._subscribers.clear()
    current_turn_id.set(None)
    current_task_id.set(None)


def test_emit_debug_appends_to_ring_buffer() -> None:
    emit_debug(
        category="input",
        severity="info",
        source="test.emit",
        summary='User envoie: "hello"',
        payload={"content": "hello"},
    )

    events = snapshot()
    assert len(events) == 1
    event = events[0]
    assert isinstance(event, DebugEvent)
    assert event.category == "input"
    assert event.severity == "info"
    assert event.source == "test.emit"
    assert event.summary == 'User envoie: "hello"'
    assert event.payload == {"content": "hello"}
    assert event.turn_id is None
    assert event.correlation_id is None
    assert event.parent_task_id is None
    assert event.replayed is False


def test_emit_debug_default_payload_is_empty_dict() -> None:
    emit_debug(
        category="system",
        severity="info",
        source="test.emit",
        summary="boot",
    )

    [event] = snapshot()
    assert event.payload == {}


def test_emit_debug_optional_ids_are_propagated() -> None:
    emit_debug(
        category="llm",
        severity="debug",
        source="test.emit",
        summary="LLM call",
        turn_id="turn-123",
        correlation_id="corr-456",
    )

    [event] = snapshot()
    assert event.turn_id == "turn-123"
    assert event.correlation_id == "corr-456"


def test_event_to_dict_matches_wire_envelope() -> None:
    emit_debug(
        category="llm",
        severity="info",
        source="bob.llm_client.complete",
        summary="LLM call démarré",
        payload={"messages": [{"role": "user", "content": "hi"}]},
        turn_id="t1",
        correlation_id="c1",
    )

    [event] = snapshot()
    wire = event.to_dict()
    # PRD `Schema sur le fil` field set.
    assert set(wire.keys()) == {
        "ts",
        "category",
        "severity",
        "source",
        "summary",
        "payload",
        "turn_id",
        "correlation_id",
        "parent_task_id",
        "replayed",
    }
    assert wire["turn_id"] == "t1"
    assert wire["correlation_id"] == "c1"
    assert wire["replayed"] is False


def test_timestamp_is_iso8601_with_z_suffix() -> None:
    emit_debug(
        category="input",
        severity="info",
        source="test.ts",
        summary="x",
    )

    [event] = snapshot()
    # Format: YYYY-MM-DDTHH:MM:SS.sssZ
    assert event.ts.endswith("Z")
    assert "T" in event.ts
    # Length sanity: 4+1+2+1+2+1+2+1+2+1+2+1+3+1 = 24
    assert len(event.ts) == 24


def test_ring_buffer_caps_at_maxlen() -> None:
    # The buffer caps at 2000; emit a few past the cap.
    for i in range(2005):
        emit_debug(
            category="input",
            severity="trace",
            source="test.cap",
            summary=f"event-{i}",
        )

    events = snapshot()
    assert len(events) == 2000
    # Oldest dropped — first remaining is event 5, last is 2004.
    assert events[0].summary == "event-5"
    assert events[-1].summary == "event-2004"


def test_no_subscribers_does_not_crash() -> None:
    """`emit_debug` is a pure side-effect when nobody listens."""

    assert subscriber_count() == 0
    emit_debug(
        category="system",
        severity="info",
        source="test.no_sub",
        summary="lonely",
    )
    # Buffer still grew.
    assert len(snapshot()) == 1


@pytest.mark.asyncio
async def test_subscribe_replays_snapshot_then_streams_live() -> None:
    # Seed two events before any subscriber connects.
    emit_debug(category="input", severity="info", source="t", summary="seed-1")
    emit_debug(category="input", severity="info", source="t", summary="seed-2")

    received: list[DebugEvent] = []

    async def consumer() -> None:
        async for event in subscribe():
            received.append(event)
            if len(received) >= 4:
                break

    task = asyncio.create_task(consumer())
    # Let the consumer drain the snapshot.
    await asyncio.sleep(0)
    await asyncio.sleep(0)

    # Now emit live; should land on the consumer's queue.
    emit_debug(category="input", severity="info", source="t", summary="live-1")
    emit_debug(category="input", severity="info", source="t", summary="live-2")

    await asyncio.wait_for(task, timeout=1.0)

    assert [e.summary for e in received] == ["seed-1", "seed-2", "live-1", "live-2"]
    # Snapshot events carry replayed=True; live events keep replayed=False.
    assert received[0].replayed is True
    assert received[1].replayed is True
    assert received[2].replayed is False
    assert received[3].replayed is False


@pytest.mark.asyncio
async def test_subscriber_unregisters_when_generator_closes() -> None:
    """Closing the subscribe generator releases the subscriber slot."""

    emit_debug(category="input", severity="info", source="t", summary="seed")

    gen = subscribe()
    first = await gen.__anext__()
    assert first.summary == "seed"
    assert subscriber_count() == 1

    # Explicit cleanup mimics what FastAPI's WS handler does on disconnect
    # (the consumer breaks out and the generator's finally runs).
    await gen.aclose()
    assert subscriber_count() == 0


@pytest.mark.asyncio
async def test_emit_is_non_blocking_when_subscriber_is_full() -> None:
    """A slow subscriber must not block the producer — overflow drops oldest."""

    queue: asyncio.Queue[DebugEvent] = asyncio.Queue(maxsize=3)
    debug_log._subscribers.append(queue)
    try:
        # Emit far more than the queue can hold. None of these should raise
        # or block — non-blocking contract.
        for i in range(50):
            emit_debug(
                category="input",
                severity="trace",
                source="t",
                summary=f"event-{i}",
            )

        # Queue must be at its cap, never above.
        assert queue.qsize() == 3
        # Drain and verify the LAST 3 events are what survived (drop_oldest).
        drained = [queue.get_nowait().summary for _ in range(3)]
        assert drained == ["event-47", "event-48", "event-49"]
    finally:
        debug_log._subscribers.remove(queue)


@pytest.mark.asyncio
async def test_two_concurrent_subscribers_get_independent_streams() -> None:
    received_a: list[str] = []
    received_b: list[str] = []

    async def consumer(target: list[str], n: int) -> None:
        async for event in subscribe():
            target.append(event.summary)
            if len(target) >= n:
                break

    task_a = asyncio.create_task(consumer(received_a, 2))
    task_b = asyncio.create_task(consumer(received_b, 2))
    await asyncio.sleep(0)
    await asyncio.sleep(0)

    emit_debug(category="input", severity="info", source="t", summary="a")
    emit_debug(category="input", severity="info", source="t", summary="b")

    await asyncio.wait_for(task_a, timeout=1.0)
    await asyncio.wait_for(task_b, timeout=1.0)

    assert received_a == ["a", "b"]
    assert received_b == ["a", "b"]


# ---------------------------------------------------------------------------
# Slice 0039 — ContextVar propagation
# ---------------------------------------------------------------------------


def test_start_turn_sets_context_var_and_returns_id() -> None:
    assert current_turn_id.get() is None
    turn_id = start_turn()
    assert isinstance(turn_id, str)
    assert len(turn_id) == 32  # uuid4().hex
    assert current_turn_id.get() == turn_id


def test_emit_debug_reads_context_var_when_turn_id_omitted() -> None:
    turn_id = start_turn()

    emit_debug(
        category="input",
        severity="info",
        source="t.contextvar",
        summary="auto",
    )

    [event] = snapshot()
    assert event.turn_id == turn_id


def test_emit_debug_explicit_turn_id_overrides_context_var() -> None:
    start_turn()  # installs a different id

    emit_debug(
        category="input",
        severity="info",
        source="t.override",
        summary="explicit",
        turn_id="explicit-turn",
    )

    [event] = snapshot()
    assert event.turn_id == "explicit-turn"


def test_two_start_turn_calls_yield_distinct_ids() -> None:
    first = start_turn()
    second = start_turn()
    assert first != second
    assert current_turn_id.get() == second


@pytest.mark.asyncio
async def test_context_var_inherited_by_spawned_coroutine() -> None:
    """``asyncio.create_task`` snapshots the calling context — sub-tasks inherit ``turn_id``."""

    turn_id = start_turn()

    async def _emit_from_subtask() -> None:
        # Same context as the parent: no explicit turn_id, ContextVar wins.
        emit_debug(
            category="task",
            severity="info",
            source="t.subtask",
            summary="from sub-task",
        )

    task = asyncio.create_task(_emit_from_subtask())
    await task

    [event] = snapshot()
    assert event.turn_id == turn_id


@pytest.mark.asyncio
async def test_context_var_not_leaked_across_independent_tasks() -> None:
    """A task that does NOT inherit a set turn_id still emits with turn_id=None."""

    # Run the emitter in a fresh context so the autouse cleanup is honoured
    # even though we did NOT call ``start_turn()`` first.
    import contextvars

    captured: list[str | None] = []

    async def _emit() -> None:
        emit_debug(
            category="input",
            severity="info",
            source="t.fresh",
            summary="fresh",
        )
        events = snapshot()
        captured.append(events[-1].turn_id)

    ctx = contextvars.copy_context()
    ctx.run(asyncio.get_event_loop().create_task, _emit())
    # Drain.
    for _ in range(5):
        await asyncio.sleep(0)

    # The event emitted inside the fresh context has turn_id=None because no
    # ``start_turn()`` was called there.
    assert captured == [None]


# ---------------------------------------------------------------------------
# Slice 0039 — structlog bridge (WARN/ERROR auto-forward)
# ---------------------------------------------------------------------------


@pytest.fixture()
def _bridge_installed() -> Iterator[None]:
    """Install + tear down the bridge for the duration of one test."""

    install_structlog_bridge()
    try:
        yield
    finally:
        uninstall_structlog_bridge()


def test_bridge_install_is_idempotent() -> None:
    install_structlog_bridge()
    install_structlog_bridge()
    install_structlog_bridge()

    # Count handlers of our type on the bob root logger — exactly one.
    bob_logger = logging.getLogger("bob")
    matches = [h for h in bob_logger.handlers if isinstance(h, debug_log._DebugBridgeHandler)]
    assert len(matches) == 1

    uninstall_structlog_bridge()


def test_bridge_forwards_error_log_to_system_error_event(_bridge_installed: None) -> None:
    """A ``logger.error`` in a ``bob.*`` module produces a ``system/error`` event."""

    _logger = logging.getLogger("bob.test_module")
    _logger.error("ouch %s", "boom", extra={"task_id": "abc"})

    events = snapshot()
    matching = [e for e in events if e.summary.startswith("ouch")]
    assert len(matching) == 1
    event = matching[0]
    assert event.category == "system"
    assert event.severity == "error"
    assert event.source == "bob.test_module"
    assert event.summary == "ouch boom"
    # User-supplied structured fields ride along in the payload.
    assert event.payload.get("task_id") == "abc"


def test_bridge_forwards_warning_log_to_system_warn_event(_bridge_installed: None) -> None:
    _logger = logging.getLogger("bob.somewhere")
    _logger.warning("uh oh")

    events = snapshot()
    matching = [e for e in events if e.summary == "uh oh"]
    assert len(matching) == 1
    assert matching[0].category == "system"
    assert matching[0].severity == "warn"
    assert matching[0].source == "bob.somewhere"


def test_bridge_ignores_info_records(_bridge_installed: None) -> None:
    _logger = logging.getLogger("bob.info_only")
    _logger.info("just info")

    events = snapshot()
    assert all(e.summary != "just info" for e in events)


def test_bridge_skips_records_flagged_as_already_emitted(_bridge_installed: None) -> None:
    """The ``_debug_emitted`` extra flag opts out of the bridge."""

    _logger = logging.getLogger("bob.no_dup")
    _logger.error("explicit", extra={"_debug_emitted": True})

    events = snapshot()
    assert all(e.summary != "explicit" for e in events)


def test_bridge_skips_records_from_debug_log_itself(_bridge_installed: None) -> None:
    """The bridge MUST NOT loop back when ``debug_log`` itself logs."""

    _logger = logging.getLogger("bob.debug_log")
    _logger.error("loopy")

    events = snapshot()
    assert all(e.summary != "loopy" for e in events)


def test_bridge_captures_structlog_warn_calls(_bridge_installed: None) -> None:
    """A structlog ``warning`` event surfaces in the debug feed.

    Bob configures structlog with ``PrintLoggerFactory`` which writes JSON to
    stdout, so to confirm the bridge sees the record we route a real
    ``logging`` call through the same ``bob.*`` namespace — structlog's
    JSONRenderer-then-PrintLoggerFactory pipeline does not pass through
    ``logging``, but in production the safety net catches any code path that
    DOES log via the stdlib logger (sub-libraries, traceback handlers,
    ``_logger.exception`` paths).
    """

    _logger = logging.getLogger("bob.structlog_like")
    try:
        raise ValueError("boom")
    except ValueError:
        _logger.exception("during processing")

    events = snapshot()
    matching = [e for e in events if e.summary == "during processing"]
    assert len(matching) == 1
    event = matching[0]
    assert event.category == "system"
    assert event.severity == "error"
    # The traceback rode along in the payload.
    assert "ValueError: boom" in event.payload.get("exc_info", "")


def test_bridge_handles_record_format_errors_gracefully(_bridge_installed: None) -> None:
    """A record whose ``getMessage`` raises must not crash the producer."""

    _logger = logging.getLogger("bob.bad_format")
    # ``%s`` with no arg raises in ``record.getMessage`` — the bridge must
    # swallow this and either drop the record or fall back to ``record.msg``.
    _logger.warning("hello %s")

    # No assertion on the produced event itself — only that we don't blow up.
    # The bridge's contract is "safety net", so a defensive drop is fine.
    assert isinstance(snapshot(), list)


def test_bridge_can_be_installed_then_uninstalled(_bridge_installed: None) -> None:
    """After uninstall the bridge no longer forwards events."""

    uninstall_structlog_bridge()
    _logger = logging.getLogger("bob.no_longer_bridged")
    _logger.error("after uninstall")

    events = snapshot()
    assert all(e.summary != "after uninstall" for e in events)

    # Re-install so the fixture teardown finds the handler.
    install_structlog_bridge()


def test_bridge_includes_logger_name_in_source(_bridge_installed: None) -> None:
    for name in ("bob.orchestrator", "bob.llm_client", "bob.deeply.nested.thing"):
        logging.getLogger(name).error(f"err-{name}")
    events = snapshot()
    sources = {e.source for e in events if e.summary.startswith("err-bob.")}
    assert sources == {
        "bob.orchestrator",
        "bob.llm_client",
        "bob.deeply.nested.thing",
    }


def test_bridge_does_not_forward_non_bob_loggers(_bridge_installed: None) -> None:
    """Loggers outside the ``bob`` namespace are not bridged."""

    logging.getLogger("other.module").error("noisy 3rd party")
    structlog.get_logger("requests").warning("requests log")

    events = snapshot()
    assert all("noisy 3rd party" not in e.summary for e in events)


def test_emit_debug_with_current_turn_set_propagates_through_bridge(
    _bridge_installed: None,
) -> None:
    """Records forwarded by the bridge inherit ``current_turn_id`` automatically."""

    turn_id = start_turn()
    logging.getLogger("bob.during_turn").error("turn-scoped error")

    matching = [e for e in snapshot() if e.summary == "turn-scoped error"]
    assert len(matching) == 1
    assert matching[0].turn_id == turn_id


# ---------------------------------------------------------------------------
# Slice 0043 — current_task_id ContextVar + parent_task_id propagation
# ---------------------------------------------------------------------------


def test_start_task_sets_context_var_and_returns_reset_token() -> None:
    assert current_task_id.get() is None
    token = start_task("task-A")
    assert current_task_id.get() == "task-A"
    # The returned token restores the previous value (``None``) when reset.
    current_task_id.reset(token)
    assert current_task_id.get() is None


def test_emit_debug_captures_parent_task_id_from_context_var() -> None:
    token = start_task("task-A")
    try:
        emit_debug(
            category="task",
            severity="info",
            source="t.parent",
            summary="inside sub-task",
        )
    finally:
        current_task_id.reset(token)

    [event] = snapshot()
    assert event.parent_task_id == "task-A"


def test_emit_debug_orphan_has_no_turn_no_parent_task() -> None:
    """Outside any turn / task scope the event carries both ids as ``None``."""

    emit_debug(
        category="system",
        severity="info",
        source="t.orphan",
        summary="lonely",
    )

    [event] = snapshot()
    assert event.turn_id is None
    assert event.parent_task_id is None


def test_start_task_reset_token_restores_previous_task_for_nesting() -> None:
    """Nested start_task / reset round-trips correctly (sub-task spawns sub-task)."""

    outer = start_task("task-A")
    assert current_task_id.get() == "task-A"
    inner = start_task("task-B")
    assert current_task_id.get() == "task-B"

    # Emit while task-B is active: parent_task_id == B.
    emit_debug(category="task", severity="info", source="t.nest", summary="inner")
    current_task_id.reset(inner)
    # After reset we're back to task-A.
    assert current_task_id.get() == "task-A"
    emit_debug(category="task", severity="info", source="t.nest", summary="outer")
    current_task_id.reset(outer)
    assert current_task_id.get() is None

    events = snapshot()
    by_summary = {e.summary: e.parent_task_id for e in events}
    assert by_summary["inner"] == "task-B"
    assert by_summary["outer"] == "task-A"


@pytest.mark.asyncio
async def test_parent_task_id_inherited_by_spawned_coroutine() -> None:
    """``asyncio.create_task`` snapshots context — child coroutines inherit task id."""

    token = start_task("task-A")
    try:

        async def _emit_from_child() -> None:
            emit_debug(
                category="task",
                severity="info",
                source="t.spawn",
                summary="from child",
            )

        child = asyncio.create_task(_emit_from_child())
        await child
    finally:
        current_task_id.reset(token)

    [event] = snapshot()
    assert event.parent_task_id == "task-A"


@pytest.mark.asyncio
async def test_two_level_nesting_via_spawned_subtasks() -> None:
    """Sub-task A spawns sub-task B → B's events carry parent_task_id == B.

    The model is that ``start_task`` is called at the entry of each
    sub-task's runner. The *immediate* enclosing sub-task is what
    ``parent_task_id`` records — exactly like ``current_turn_id`` records
    the immediate enclosing turn.
    """

    async def _sub_b() -> None:
        token = start_task("task-B")
        try:
            emit_debug(category="task", severity="info", source="t.B", summary="B")
        finally:
            current_task_id.reset(token)

    async def _sub_a() -> None:
        token = start_task("task-A")
        try:
            emit_debug(category="task", severity="info", source="t.A", summary="A")
            await asyncio.create_task(_sub_b())
            # After B finished its scope, we're still in A.
            emit_debug(category="task", severity="info", source="t.A2", summary="A-after-B")
        finally:
            current_task_id.reset(token)

    await _sub_a()

    by_summary = {e.summary: e.parent_task_id for e in snapshot()}
    assert by_summary["A"] == "task-A"
    assert by_summary["B"] == "task-B"
    assert by_summary["A-after-B"] == "task-A"
