"""Contract tests for :class:`bob.streaming.StreamEmitter` (issue 0049).

The emitter is exercised through its public ``feed`` / ``finalize``
surface. We swap the default WS emit (``bob.event_bus_v2.emit_event``)
for a recording double so the assertions stay on the observable
contract — frame sequence + payload — instead of internal calls into
the unified event bus.
"""

from __future__ import annotations

from typing import Any

import pytest

from bob.streaming.stream_emitter import StreamEmitter


class _Recorder:
    """Async callable that records every emit payload."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []
        self.raise_on_call: Exception | None = None

    async def __call__(self, payload: dict[str, Any]) -> None:
        if self.raise_on_call is not None:
            raise self.raise_on_call
        self.calls.append(payload)


def _build_emitter(msg_id: str = "msg-1") -> tuple[StreamEmitter, _Recorder]:
    rec = _Recorder()
    emitter = StreamEmitter(msg_id=msg_id, emit=rec)
    return emitter, rec


@pytest.mark.asyncio
async def test_single_chunk_speech_emits_one_delta() -> None:
    """A single full-buffer feed surfaces one ``speech_delta``."""

    emitter, rec = _build_emitter()
    await emitter.feed('{"speech":"Hello world"}')
    assert len(rec.calls) == 1
    assert rec.calls[0] == {
        "type": "speech_delta",
        "msg_id": "msg-1",
        "delta": "Hello world",
    }


@pytest.mark.asyncio
async def test_byte_by_byte_yields_incremental_deltas() -> None:
    """Each incremental delta produces ONE ``speech_delta`` per growth."""

    emitter, rec = _build_emitter()
    chunks = [
        '{"',
        "speech",
        '":"',
        "Hel",
        "lo ",
        "world",
        '"}',
    ]
    for chunk in chunks:
        await emitter.feed(chunk)
    deltas = [c["delta"] for c in rec.calls if c["type"] == "speech_delta"]
    # Concatenation matches the full speech.
    assert "".join(deltas) == "Hello world"
    # We DID emit multiple deltas (otherwise streaming is broken).
    assert len(deltas) >= 3


@pytest.mark.asyncio
async def test_finalize_emits_ui_payload_for_non_null_ui() -> None:
    """A ``ui`` dict on close fires exactly one ``ui_payload`` frame."""

    emitter, rec = _build_emitter()
    await emitter.feed('{"speech":"hi","ui":{"component":"Markdown","props":{"content":"x"}}}')
    await emitter.finalize()

    speech_deltas = [c for c in rec.calls if c["type"] == "speech_delta"]
    ui_payloads = [c for c in rec.calls if c["type"] == "ui_payload"]
    assert len(speech_deltas) == 1
    assert speech_deltas[0]["delta"] == "hi"
    assert len(ui_payloads) == 1
    assert ui_payloads[0]["msg_id"] == "msg-1"
    assert ui_payloads[0]["ui"] == {
        "component": "Markdown",
        "props": {"content": "x"},
    }


@pytest.mark.asyncio
async def test_finalize_with_null_ui_does_not_emit() -> None:
    """``ui: null`` is the empty-overlay case — no frame."""

    emitter, rec = _build_emitter()
    await emitter.feed('{"speech":"hi","ui":null}')
    await emitter.finalize()
    assert not any(c["type"] == "ui_payload" for c in rec.calls)


@pytest.mark.asyncio
async def test_finalize_with_missing_ui_does_not_emit() -> None:
    """Absent ``ui`` field is treated identically to ``ui: null``."""

    emitter, rec = _build_emitter()
    await emitter.feed('{"speech":"hi"}')
    await emitter.finalize()
    assert not any(c["type"] == "ui_payload" for c in rec.calls)


@pytest.mark.asyncio
async def test_finalize_with_non_dict_ui_logs_and_skips() -> None:
    """A list/number/string in ``ui`` is dropped, not emitted."""

    emitter, rec = _build_emitter()
    await emitter.feed('{"speech":"hi","ui":"oops"}')
    await emitter.finalize()
    assert not any(c["type"] == "ui_payload" for c in rec.calls)


@pytest.mark.asyncio
async def test_finalize_is_idempotent() -> None:
    """A second call to finalize is a no-op."""

    emitter, rec = _build_emitter()
    await emitter.feed('{"speech":"hi","ui":{"component":"X","props":{}}}')
    await emitter.finalize()
    n_payloads_first = len([c for c in rec.calls if c["type"] == "ui_payload"])
    await emitter.finalize()
    n_payloads_second = len([c for c in rec.calls if c["type"] == "ui_payload"])
    assert n_payloads_first == 1
    assert n_payloads_second == n_payloads_first


@pytest.mark.asyncio
async def test_finalize_with_explicit_arguments_overrides_buffer() -> None:
    """``finalize(final_arguments=...)`` wins over the parsed buffer."""

    emitter, rec = _build_emitter()
    await emitter.feed('{"speech":"hi"}')
    await emitter.finalize(
        {"speech": "hi", "ui": {"component": "Markdown", "props": {"content": "y"}}}
    )
    ui_payloads = [c for c in rec.calls if c["type"] == "ui_payload"]
    assert len(ui_payloads) == 1
    assert ui_payloads[0]["ui"]["props"]["content"] == "y"


@pytest.mark.asyncio
async def test_feed_empty_delta_emits_nothing() -> None:
    emitter, rec = _build_emitter()
    await emitter.feed("")
    assert rec.calls == []


@pytest.mark.asyncio
async def test_feed_garbage_emits_nothing() -> None:
    """Non-JSON prefix doesn't crash and emits nothing."""

    emitter, rec = _build_emitter()
    await emitter.feed("not json yet")
    assert not any(c["type"] == "speech_delta" for c in rec.calls)


@pytest.mark.asyncio
async def test_feed_after_finalize_logs_and_ignores() -> None:
    """A late ``feed`` after ``finalize`` is dropped without crashing."""

    emitter, rec = _build_emitter()
    await emitter.feed('{"speech":"hi"}')
    await emitter.finalize()
    rec.calls.clear()
    await emitter.feed("more bytes")
    assert rec.calls == []


@pytest.mark.asyncio
async def test_emit_exception_does_not_crash_feed() -> None:
    """A broken WS forwarder is swallowed (live turn must keep going)."""

    rec = _Recorder()
    rec.raise_on_call = RuntimeError("broken socket")
    emitter = StreamEmitter(msg_id="msg-1", emit=rec)
    # Does not raise.
    await emitter.feed('{"speech":"hi"}')
    await emitter.finalize()


@pytest.mark.asyncio
async def test_correct_frame_sequence_for_full_say_payload() -> None:
    """End-to-end: 5 deltas + 1 ui_payload in order, matching the issue AC."""

    emitter, rec = _build_emitter()
    deltas = [
        '{"speech":"',
        "Bonjour",
        " Tom,",
        " comment",
        " ça va",
        ' ?",',
        '"ui":',
        '{"component":"Markdown",',
        '"props":{"content":"# Hi"}}',
        "}",
    ]
    for delta in deltas:
        await emitter.feed(delta)
    await emitter.finalize()

    types = [c["type"] for c in rec.calls]
    # All speech_deltas appear before the ui_payload.
    assert types[-1] == "ui_payload"
    speech_total = "".join(c["delta"] for c in rec.calls if c["type"] == "speech_delta")
    assert speech_total == "Bonjour Tom, comment ça va ?"
    ui_payload = next(c for c in rec.calls if c["type"] == "ui_payload")
    assert ui_payload["ui"] == {
        "component": "Markdown",
        "props": {"content": "# Hi"},
    }


@pytest.mark.asyncio
async def test_msg_id_threaded_into_every_emit() -> None:
    """Both ``speech_delta`` and ``ui_payload`` carry the construction-time msg_id."""

    emitter, rec = _build_emitter(msg_id="abc-42")
    await emitter.feed('{"speech":"hi","ui":{"component":"X","props":{}}}')
    await emitter.finalize()
    for call in rec.calls:
        assert call["msg_id"] == "abc-42"


@pytest.mark.asyncio
async def test_speech_with_escaped_quotes_streams_correctly() -> None:
    """``\\"`` inside speech doesn't break the incremental delta math."""

    emitter, rec = _build_emitter()
    # Stream byte-by-byte to make sure the escape boundary is hit.
    raw = '{"speech":"He said \\"hi\\"."}'
    for i in range(len(raw)):
        await emitter.feed(raw[i])
    speech_total = "".join(c["delta"] for c in rec.calls if c["type"] == "speech_delta")
    assert speech_total == 'He said "hi".'


@pytest.mark.asyncio
async def test_speech_with_utf8_multibyte_chars() -> None:
    """é / accents flow through; we feed full string but check correctness."""

    emitter, rec = _build_emitter()
    await emitter.feed('{"speech":"héllo, ça va ?"}')
    deltas = [c["delta"] for c in rec.calls if c["type"] == "speech_delta"]
    assert "".join(deltas) == "héllo, ça va ?"
