"""SpeechStreamPipeline — pipelined TTS + bounded send buffer (PRD 0018 / issue 0121).

The say-path used to be strictly sequential: synthesize sentence N fully,
drain its PCM chunks to the WebSocket, then start sentence N+1 — paying a
~250 ms synthesis gap between every pair of sentences. This module owns the
"stream of sentences in → stream of PCM chunks out" path as ONE object:

- a **producer task** feeds each sentence to the synthesizer and pushes the
  resulting chunks into a **bounded** :class:`asyncio.Queue`, so sentence N+1
  enters synthesis while sentence N's chunks are still draining to the client;
- a **consumer task** pops chunks off the queue and hands them to the sink
  (the WebSocket write). A slow client applies backpressure to the queue —
  the producer parks on ``put`` at the bound — never unbounded memory growth;
- :meth:`SpeechStreamPipeline.cancel` is the ONE kill-switch: a single call
  cuts synthesis AND drain together (the barge-in surface for issue 0119).
  After ``cancel()`` no further chunk reaches the sink and :meth:`run` raises
  :class:`asyncio.CancelledError` (matching the say-path task-cancel contract);
- the pipeline places the ``tts_first_chunk`` mark (PRD 0018 / issue 0117) the
  moment the synthesizer produces the turn's first PCM block — before any
  network write, exactly the stage the metric describes;
- per-audio-chunk debug events are replaced by a periodic **batched summary**
  (:class:`ChunkBatchSummary`: count + bytes per window), cutting the event
  volume of a long reply from one-event-per-chunk to one-per-window.

Sentence-level synthesis errors (Kokoro phonemizer hiccups) are routed through
the queue in order and reported via ``on_sentence_error`` — the pipeline skips
to the next sentence, preserving the historical "one bad sentence never kills
the reply" behaviour. Sink errors (dead socket) are NOT swallowed: they abort
the run and propagate to the caller.

The pipeline is transport-agnostic and engine-agnostic: it sees an async
``synthesize(sentence)`` chunk iterator and an async ``send_chunk`` sink, so
isolation tests drive it with fakes and a fake clock (``clock`` injectable,
batching only). The queue bound and the batch window are settings
(``SPEECH_PIPELINE_QUEUE_MAX_CHUNKS`` / ``SPEECH_PIPELINE_BATCH_WINDOW_MS``).
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncIterator, Awaitable, Callable, Sequence
from dataclasses import dataclass

import structlog

from bob import turn_metrics
from bob.tts_service import SynthesisChunk

_logger = structlog.get_logger(__name__)

#: Default bound of the producer→consumer chunk queue. One Kokoro chunk is
#: ~250 ms of 24 kHz s16le PCM (~12 KiB), so 16 queued chunks ≈ 4 s of audio /
#: ~200 KiB — enough run-ahead to absorb synthesis jitter, small enough that a
#: blocked client stops production almost immediately.
DEFAULT_QUEUE_MAX_CHUNKS = 16

#: Default batched-summary window (ms): at most one ``audio_chunk_batch``
#: observation per second of wall clock while chunks flow.
DEFAULT_BATCH_WINDOW_MS = 1000


@dataclass(frozen=True)
class ChunkBatchSummary:
    """One windowed audio-out observation (replaces per-chunk debug events)."""

    count: int
    """Number of PCM chunks the sink accepted during the window."""

    pcm_bytes: int
    """Total PCM payload bytes across those chunks."""

    first_chunk_index: int
    """Zero-based index (within the whole run) of the window's first chunk."""

    sample_rate: int
    """Sample rate of the last chunk in the window (constant per engine)."""


@dataclass(frozen=True)
class _Chunk:
    """A synthesized PCM block queued for the sink."""

    sentence_index: int
    chunk: SynthesisChunk


@dataclass(frozen=True)
class _SentenceDone:
    """All chunks of ``sentence_index`` are queued ahead of this marker."""

    sentence_index: int


@dataclass(frozen=True)
class _SentenceFailed:
    """Synthesis of ``sentence_index`` raised; the pipeline skipped it."""

    sentence_index: int
    error: Exception


#: ``None`` is the end-of-stream marker (same convention as the TTS engine's
#: internal queue), so the item type is a small closed union.
_QueueItem = _Chunk | _SentenceDone | _SentenceFailed | None


class SpeechStreamPipeline:
    """Single-use pipelined "sentences in → PCM chunks out" runner.

    Construct one per reply, call :meth:`run` once with the segmented
    sentences, and (optionally) :meth:`cancel` from anywhere to cut the whole
    stream with one call. All callbacks fire on the event loop in stream
    order; ``send_chunk`` is the only required sink.
    """

    def __init__(
        self,
        *,
        synthesize: Callable[[str], AsyncIterator[SynthesisChunk]],
        send_chunk: Callable[[SynthesisChunk], Awaitable[None]],
        on_sentence_drained: Callable[[int], Awaitable[None]] | None = None,
        on_sentence_error: Callable[[int, Exception], Awaitable[None]] | None = None,
        on_chunk_batch: Callable[[ChunkBatchSummary], Awaitable[None]] | None = None,
        queue_max_chunks: int = DEFAULT_QUEUE_MAX_CHUNKS,
        batch_window_ms: int = DEFAULT_BATCH_WINDOW_MS,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._synthesize = synthesize
        self._send_chunk = send_chunk
        self._on_sentence_drained = on_sentence_drained
        self._on_sentence_error = on_sentence_error
        self._on_chunk_batch = on_chunk_batch
        self._batch_window_s = max(0, batch_window_ms) / 1000.0
        self._clock = clock
        self._queue: asyncio.Queue[_QueueItem] = asyncio.Queue(maxsize=max(1, queue_max_chunks))
        self._producer: asyncio.Task[None] | None = None
        self._consumer: asyncio.Task[None] | None = None
        self._started = False
        self._cancelled = False
        self._first_chunk_produced = False
        # Consumer-side accumulators (event-loop confined — no locking needed).
        self._chunks_sent = 0
        self._batch_count = 0
        self._batch_bytes = 0
        self._batch_first_index = 0
        self._batch_sample_rate = 0
        self._window_started_at = 0.0

    # -- public surface ----------------------------------------------------------

    @property
    def cancelled(self) -> bool:
        """True once :meth:`cancel` has been called (sticky)."""

        return self._cancelled

    @property
    def chunks_sent(self) -> int:
        """Number of chunks the sink has accepted so far."""

        return self._chunks_sent

    def cancel(self) -> None:
        """Cut synthesis AND drain with ONE call (the barge-in surface, 0119).

        Idempotent and callable from any task at any point of the lifecycle
        (before, during or after :meth:`run`). After this returns no further
        chunk is handed to ``send_chunk`` and :meth:`run` raises
        :class:`asyncio.CancelledError` — the same observable contract as
        cancelling the enclosing say-path task, so the existing cancelling
        paths (``_cancel_active_tts``, the full-duplex loop) compose with it.
        """

        self._cancelled = True
        if self._producer is not None:
            self._producer.cancel()
        if self._consumer is not None:
            self._consumer.cancel()

    async def run(self, sentences: Sequence[str]) -> None:
        """Synthesize and stream ``sentences``; return when fully drained.

        Single-use. Raises :class:`asyncio.CancelledError` when cut (either
        :meth:`cancel` or cancellation of the awaiting task) and re-raises the
        first real failure of the sink path. Per-sentence synthesis errors do
        NOT raise — they surface through ``on_sentence_error`` and the
        pipeline moves on to the next sentence.
        """

        if self._started:
            raise RuntimeError("SpeechStreamPipeline.run() is single-use")
        self._started = True
        if self._cancelled:
            raise asyncio.CancelledError
        todo = [sentence for sentence in sentences if sentence.strip()]
        if not todo:
            return

        producer = asyncio.create_task(self._produce(todo), name="speech_pipeline.synthesize")
        consumer = asyncio.create_task(self._drain(), name="speech_pipeline.drain")
        self._producer = producer
        self._consumer = consumer

        failure: BaseException | None = None
        try:
            # FIRST_EXCEPTION so a wholesale producer crash (a bug — per-sentence
            # synth errors ride the queue) can never park the consumer on an
            # EOS that will not come: we wake up, tear down, and surface it.
            await asyncio.wait({producer, consumer}, return_when=asyncio.FIRST_EXCEPTION)
        finally:
            producer.cancel()
            consumer.cancel()
            results = await asyncio.gather(producer, consumer, return_exceptions=True)
            for result in results:
                if isinstance(result, asyncio.CancelledError):
                    continue
                if isinstance(result, BaseException):
                    failure = result
                    break
            # Final flush: chunks already sent stay observable even on a cut.
            await self._flush_batch(best_effort=True)
        if failure is not None:
            raise failure
        if self._cancelled:
            raise asyncio.CancelledError

    # -- producer ----------------------------------------------------------------

    async def _produce(self, sentences: list[str]) -> None:
        """Feed sentences to the synthesizer; queue chunks + ordered markers.

        Starts sentence N+1 as soon as sentence N's chunks are queued — the
        bounded ``put`` is the only pacing, so synthesis overlaps the drain.
        """

        for index, sentence in enumerate(sentences):
            try:
                async for chunk in self._synthesize(sentence):
                    if not self._first_chunk_produced:
                        self._first_chunk_produced = True
                        # PRD 0018 / issue 0117 — the synthesizer produced the
                        # turn's first PCM block (before any network write).
                        # No-op outside a metered voice turn.
                        turn_metrics.mark_current("tts_first_chunk")
                    await self._queue.put(_Chunk(sentence_index=index, chunk=chunk))
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                _logger.error(
                    "speech_pipeline.sentence_synthesis_failed",
                    sentence_index=index,
                    error=repr(exc),
                )
                await self._queue.put(_SentenceFailed(sentence_index=index, error=exc))
                continue
            await self._queue.put(_SentenceDone(sentence_index=index))
        await self._queue.put(None)

    # -- consumer ----------------------------------------------------------------

    async def _drain(self) -> None:
        """Pop queue items in order: send chunks, fire the per-sentence hooks."""

        while True:
            item = await self._queue.get()
            if item is None:
                return
            if isinstance(item, _SentenceDone):
                if self._on_sentence_drained is not None:
                    await self._on_sentence_drained(item.sentence_index)
                continue
            if isinstance(item, _SentenceFailed):
                if self._on_sentence_error is not None:
                    await self._on_sentence_error(item.sentence_index, item.error)
                continue
            await self._send_chunk(item.chunk)
            self._record_sent(item.chunk)
            if (
                self._batch_count
                and self._clock() - self._window_started_at >= self._batch_window_s
            ):
                await self._flush_batch()

    def _record_sent(self, chunk: SynthesisChunk) -> None:
        if self._batch_count == 0:
            self._window_started_at = self._clock()
            self._batch_first_index = self._chunks_sent
        self._batch_count += 1
        self._batch_bytes += len(chunk.pcm16)
        self._batch_sample_rate = chunk.sample_rate
        self._chunks_sent += 1

    async def _flush_batch(self, *, best_effort: bool = False) -> None:
        """Emit the accumulated window as one :class:`ChunkBatchSummary`.

        Accumulators are reset BEFORE the callback so a failing/cancelling
        emitter can never double-report a window. ``best_effort`` (the final
        flush on the teardown path) swallows emitter errors — observability
        must never turn a clean cut into a crash.
        """

        if self._batch_count == 0 or self._on_chunk_batch is None:
            return
        summary = ChunkBatchSummary(
            count=self._batch_count,
            pcm_bytes=self._batch_bytes,
            first_chunk_index=self._batch_first_index,
            sample_rate=self._batch_sample_rate,
        )
        self._batch_count = 0
        self._batch_bytes = 0
        try:
            await self._on_chunk_batch(summary)
        except asyncio.CancelledError:
            raise
        except Exception:
            if not best_effort:
                raise
            _logger.warning("speech_pipeline.batch_summary_emit_failed", exc_info=True)


__all__ = [
    "DEFAULT_BATCH_WINDOW_MS",
    "DEFAULT_QUEUE_MAX_CHUNKS",
    "ChunkBatchSummary",
    "SpeechStreamPipeline",
]
