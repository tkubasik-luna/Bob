"""Isolation tests for :class:`bob.speech_pipeline.SpeechStreamPipeline` (issue 0121).

Everything runs against fakes through the pipeline's public boundary: a fake
synthesizer (async chunk generator per sentence), a controllable sink, and —
for the batching tests — a manually-advanced fake clock. No WebSocket, no
Kokoro, no event bus.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Callable
from typing import cast

import pytest

from bob import turn_metrics
from bob.speech_pipeline import ChunkBatchSummary, SpeechStreamPipeline
from bob.tts_service import SynthesisChunk
from bob.turn_metrics import TurnLatencyMetrics

_SAMPLE_RATE = 24_000


def _chunk(n_bytes: int = 10) -> SynthesisChunk:
    return SynthesisChunk(pcm16=b"\x00" * n_bytes, sample_rate=_SAMPLE_RATE)


def _instant_synth(
    chunks_per_sentence: int = 2, n_bytes: int = 10
) -> Callable[[str], AsyncIterator[SynthesisChunk]]:
    """A synthesizer double: ``chunks_per_sentence`` fixed chunks per sentence."""

    def synthesize(sentence: str) -> AsyncIterator[SynthesisChunk]:
        async def _gen() -> AsyncIterator[SynthesisChunk]:
            for _ in range(chunks_per_sentence):
                yield _chunk(n_bytes)

        return _gen()

    return synthesize


async def _settle(iterations: int = 50) -> None:
    """Let the producer/consumer tasks run to their next blocking point."""

    for _ in range(iterations):
        await asyncio.sleep(0)


class _FakeClock:
    def __init__(self, start: float = 100.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now


# --- pipelining (sentence N+1 synthesis overlaps sentence N drain) -----------


async def test_next_sentence_synthesis_starts_while_previous_drains() -> None:
    """With a slow sink, sentence B enters synthesis before sentence A drained."""

    events: list[str] = []
    first_chunk_in_sink = asyncio.Event()
    release_sink = asyncio.Event()
    drained = 0

    def synthesize(sentence: str) -> AsyncIterator[SynthesisChunk]:
        async def _gen() -> AsyncIterator[SynthesisChunk]:
            events.append(f"synth_start:{sentence}")
            for _ in range(2):
                yield _chunk()
            events.append(f"synth_end:{sentence}")

        return _gen()

    async def send_chunk(chunk: SynthesisChunk) -> None:
        nonlocal drained
        first_chunk_in_sink.set()
        await release_sink.wait()
        drained += 1
        events.append(f"drained:{drained}")

    pipeline = SpeechStreamPipeline(
        synthesize=synthesize, send_chunk=send_chunk, queue_max_chunks=8
    )
    run = asyncio.create_task(pipeline.run(["Phrase A.", "Phrase B."]))
    await asyncio.wait_for(first_chunk_in_sink.wait(), 2)
    await _settle()

    # Sentence B was synthesized end-to-end while NOT ONE chunk of sentence A
    # had finished draining — the inter-sentence synthesis gap is gone.
    assert "synth_start:Phrase B." in events
    assert "synth_end:Phrase B." in events
    assert not any(e.startswith("drained:") for e in events)

    release_sink.set()
    await asyncio.wait_for(run, 2)
    assert drained == 4
    assert pipeline.chunks_sent == 4


# --- bounded queue / backpressure ---------------------------------------------


async def test_blocked_sink_stops_production_at_queue_bound() -> None:
    """A blocked sink parks the producer at the bound; resuming re-drains all."""

    produced = 0
    sent = 0
    release_sink = asyncio.Event()
    total_chunks = 50
    bound = 4

    def synthesize(sentence: str) -> AsyncIterator[SynthesisChunk]:
        async def _gen() -> AsyncIterator[SynthesisChunk]:
            nonlocal produced
            for _ in range(total_chunks):
                produced += 1
                yield _chunk()

        return _gen()

    async def send_chunk(chunk: SynthesisChunk) -> None:
        nonlocal sent
        await release_sink.wait()
        sent += 1

    pipeline = SpeechStreamPipeline(
        synthesize=synthesize, send_chunk=send_chunk, queue_max_chunks=bound
    )
    run = asyncio.create_task(pipeline.run(["Une très longue phrase."]))
    await _settle(200)

    # Production stalled: `bound` chunks queued + 1 parked on `put` + 1 held by
    # the blocked sink — nothing else, no matter how long the sink stays stuck.
    assert produced == bound + 2
    await _settle(100)
    assert produced == bound + 2  # no memory growth while the sink is blocked

    release_sink.set()
    await asyncio.wait_for(run, 2)
    assert produced == total_chunks
    assert sent == total_chunks


# --- single cancel() ------------------------------------------------------------


async def test_single_cancel_stops_synthesis_and_drain() -> None:
    """ONE cancel() cuts producer + consumer; no chunk reaches the sink after."""

    produced = 0
    send_attempts = 0
    sends_completed = 0
    first_send = asyncio.Event()
    sink_gate = asyncio.Event()

    def synthesize(sentence: str) -> AsyncIterator[SynthesisChunk]:
        async def _gen() -> AsyncIterator[SynthesisChunk]:
            nonlocal produced
            for _ in range(100):
                produced += 1
                yield _chunk()

        return _gen()

    async def send_chunk(chunk: SynthesisChunk) -> None:
        nonlocal send_attempts, sends_completed
        send_attempts += 1
        first_send.set()
        await sink_gate.wait()
        sends_completed += 1

    pipeline = SpeechStreamPipeline(
        synthesize=synthesize, send_chunk=send_chunk, queue_max_chunks=2
    )
    run = asyncio.create_task(pipeline.run(["Phrase coupée."]))
    await asyncio.wait_for(first_send.wait(), 2)
    await _settle()
    produced_at_cancel = produced

    pipeline.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(run, 2)

    assert pipeline.cancelled is True
    sink_gate.set()  # even a now-unblocked sink gets nothing more
    await _settle()
    assert send_attempts == 1
    assert sends_completed == 0  # the in-flight send was cut, not completed
    assert produced == produced_at_cancel  # synthesis stopped with the same call


async def test_cancel_before_run_raises_and_synthesizes_nothing() -> None:
    produced = 0

    def synthesize(sentence: str) -> AsyncIterator[SynthesisChunk]:
        async def _gen() -> AsyncIterator[SynthesisChunk]:
            nonlocal produced
            produced += 1
            yield _chunk()

        return _gen()

    sent: list[SynthesisChunk] = []

    async def send_chunk(chunk: SynthesisChunk) -> None:
        sent.append(chunk)

    pipeline = SpeechStreamPipeline(synthesize=synthesize, send_chunk=send_chunk)
    pipeline.cancel()
    with pytest.raises(asyncio.CancelledError):
        await pipeline.run(["Jamais dite."])
    assert produced == 0
    assert sent == []


async def test_run_is_single_use() -> None:
    pipeline = SpeechStreamPipeline(
        synthesize=_instant_synth(), send_chunk=_noop_send, queue_max_chunks=4
    )
    await pipeline.run(["Une phrase."])
    with pytest.raises(RuntimeError):
        await pipeline.run(["Une autre."])


async def _noop_send(chunk: SynthesisChunk) -> None:
    return None


# --- tts_first_chunk mark (issue 0117) ------------------------------------------


async def test_pipeline_places_tts_first_chunk_mark() -> None:
    collector = TurnLatencyMetrics()
    turn_metrics.set_default_collector(collector)
    token = turn_metrics.current_metrics_turn_id.set("turn-0121")
    try:
        collector.begin_turn("turn-0121")
        pipeline = SpeechStreamPipeline(synthesize=_instant_synth(), send_chunk=_noop_send)
        await pipeline.run(["Bonjour."])
        summary = collector.finish_turn("turn-0121")
    finally:
        turn_metrics.current_metrics_turn_id.reset(token)
        turn_metrics.set_default_collector(None)
    assert summary is not None
    assert "tts_first_chunk" in cast(dict[str, float], summary["stages_ms"])


async def test_no_mark_outside_a_metered_turn() -> None:
    """Text path (ContextVar unset): the mark is a no-op, never a crash."""

    collector = TurnLatencyMetrics()
    turn_metrics.set_default_collector(collector)
    try:
        pipeline = SpeechStreamPipeline(synthesize=_instant_synth(), send_chunk=_noop_send)
        await pipeline.run(["Bonjour."])
    finally:
        turn_metrics.set_default_collector(None)
    assert collector.aggregates()["turns_measured"] == 0


# --- batched chunk summaries -----------------------------------------------------


async def test_batched_summary_per_window_under_fake_clock() -> None:
    """One summary per elapsed window + one trailing flush — not one per chunk."""

    clock = _FakeClock()
    summaries: list[ChunkBatchSummary] = []

    async def send_chunk(chunk: SynthesisChunk) -> None:
        clock.now += 0.4  # each chunk takes 400 ms of (fake) wall clock to send

    async def on_chunk_batch(summary: ChunkBatchSummary) -> None:
        summaries.append(summary)

    pipeline = SpeechStreamPipeline(
        synthesize=_instant_synth(chunks_per_sentence=5, n_bytes=10),
        send_chunk=send_chunk,
        on_chunk_batch=on_chunk_batch,
        batch_window_ms=1000,
        clock=clock,
    )
    await pipeline.run(["Cinq morceaux."])

    assert [s.count for s in summaries] == [4, 1]
    assert [s.pcm_bytes for s in summaries] == [40, 10]
    assert [s.first_chunk_index for s in summaries] == [0, 4]
    assert all(s.sample_rate == _SAMPLE_RATE for s in summaries)
    assert sum(s.count for s in summaries) == 5  # every chunk accounted exactly once


async def test_final_flush_reports_trailing_partial_window() -> None:
    """A short reply (one window never elapses) still yields exactly one summary."""

    clock = _FakeClock()
    summaries: list[ChunkBatchSummary] = []

    async def on_chunk_batch(summary: ChunkBatchSummary) -> None:
        summaries.append(summary)

    pipeline = SpeechStreamPipeline(
        synthesize=_instant_synth(chunks_per_sentence=3, n_bytes=8),
        send_chunk=_noop_send,
        on_chunk_batch=on_chunk_batch,
        batch_window_ms=1000,
        clock=clock,
    )
    await pipeline.run(["Court."])
    assert [s.count for s in summaries] == [3]
    assert summaries[0].pcm_bytes == 24


async def test_cancel_still_flushes_already_sent_chunks() -> None:
    """The cut path emits a final best-effort summary for what DID go out."""

    summaries: list[ChunkBatchSummary] = []
    first_send_done = asyncio.Event()
    sink_gate = asyncio.Event()
    sent = 0

    async def send_chunk(chunk: SynthesisChunk) -> None:
        nonlocal sent
        if sent >= 1:
            first_send_done.set()
            await sink_gate.wait()
        sent += 1

    async def on_chunk_batch(summary: ChunkBatchSummary) -> None:
        summaries.append(summary)

    pipeline = SpeechStreamPipeline(
        synthesize=_instant_synth(chunks_per_sentence=10),
        send_chunk=send_chunk,
        on_chunk_batch=on_chunk_batch,
        batch_window_ms=60_000,  # the window never elapses — only the final flush
    )
    run = asyncio.create_task(pipeline.run(["Dix morceaux."]))
    await asyncio.wait_for(first_send_done.wait(), 2)
    pipeline.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(run, 2)
    assert [s.count for s in summaries] == [1]


# --- per-sentence synthesis errors ------------------------------------------------


async def test_sentence_synthesis_error_is_reported_and_skipped() -> None:
    errors: list[tuple[int, Exception]] = []
    drained: list[int] = []
    sent = 0

    def synthesize(sentence: str) -> AsyncIterator[SynthesisChunk]:
        async def _gen() -> AsyncIterator[SynthesisChunk]:
            if "boom" in sentence:
                raise RuntimeError("phonemizer")
            yield _chunk()

        return _gen()

    async def send_chunk(chunk: SynthesisChunk) -> None:
        nonlocal sent
        sent += 1

    async def on_sentence_drained(index: int) -> None:
        drained.append(index)

    async def on_sentence_error(index: int, error: Exception) -> None:
        errors.append((index, error))

    pipeline = SpeechStreamPipeline(
        synthesize=synthesize,
        send_chunk=send_chunk,
        on_sentence_drained=on_sentence_drained,
        on_sentence_error=on_sentence_error,
    )
    await pipeline.run(["Ça va.", "boom.", "Toujours là."])

    assert [(index, type(error).__name__) for index, error in errors] == [(1, "RuntimeError")]
    assert drained == [0, 2]
    assert sent == 2


async def test_sink_failure_propagates_and_stops_the_run() -> None:
    """A dead socket is NOT a per-sentence skip: the run aborts with the error."""

    async def send_chunk(chunk: SynthesisChunk) -> None:
        raise ConnectionError("socket closed")

    pipeline = SpeechStreamPipeline(
        synthesize=_instant_synth(chunks_per_sentence=3), send_chunk=send_chunk
    )
    with pytest.raises(ConnectionError):
        await pipeline.run(["Une.", "Deux."])


async def test_empty_or_blank_sentences_are_a_clean_noop() -> None:
    sent: list[SynthesisChunk] = []
    summaries: list[ChunkBatchSummary] = []

    async def send_chunk(chunk: SynthesisChunk) -> None:
        sent.append(chunk)

    async def on_chunk_batch(summary: ChunkBatchSummary) -> None:
        summaries.append(summary)

    pipeline = SpeechStreamPipeline(
        synthesize=_instant_synth(), send_chunk=send_chunk, on_chunk_batch=on_chunk_batch
    )
    await pipeline.run(["   ", ""])
    assert sent == []
    assert summaries == []
