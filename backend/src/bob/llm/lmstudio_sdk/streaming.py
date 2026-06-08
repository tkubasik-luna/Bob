"""SDK prediction stream → Bob :class:`StreamChunk` adapter (PRD 0017 / M5).

A deep, ISOLATED, separately-testable unit that maps a stream of ``lmstudio``
SDK prediction fragments onto Bob's :class:`bob.llm.types.StreamChunk` surface,
the same surface the OpenAI transport's
:meth:`bob.llm_client.LMStudioClient._consume_chat_stream` produces. Keeping the
mapping a free function (driven by any async iterable of fragments + a final
stats lookup) means the offline suite drives it from a SCRIPTED fragment
iterator with no running LM Studio server and no real websocket.

SDK streaming surface (real ``lmstudio`` 1.5.0 — validated against the installed
package, see the investigation doc):

- ``async for fragment in model.respond_stream(...)`` yields
  :class:`lmstudio.LlmPredictionFragment` with ``content: str``,
  ``tokens_count: int``, ``contains_drafted: bool`` and
  ``reasoning_type: Literal["none", "reasoning", "reasoningStartTag",
  "reasoningEndTag"]``.
- A fragment is REASONING (chain-of-thought, cosmetic) iff its ``reasoning_type``
  is anything other than ``"none"`` — i.e. ``"reasoning"`` or one of the
  reasoning tag markers. Normal content is ``reasoning_type == "none"``.
- After the async iteration drains, the stream exposes the final
  :class:`lmstudio.LlmPredictionStats` via ``stream.stats`` —
  ``time_to_first_token_sec`` / ``tokens_per_second`` /
  ``prompt_tokens_count`` / ``predicted_tokens_count`` / ``total_tokens_count``.
  This is the SDK's own stats shape, NOT the OpenAI ``usage`` block, so the
  ``perf`` chunk is built here from the SDK stats rather than from
  :func:`bob.llm_client._read_usage`.

Reuse by issue 0114 (``stream_complete`` / tool-calls): the per-fragment
``text`` / ``reasoning`` mapping below is exactly the no-tools subset of the
streaming surface. 0114 drives its own event iteration (the SDK
``act``/tool-call channel surfaces ``PredictionToolCallEvent`` alongside
fragments) and reuses :func:`fragment_to_chunk` for the text/reasoning fragments
plus :func:`build_perf_chunk` for the terminal stats, only adding the
``tool_call_*`` arm — the text/reasoning/perf mapping never gets rewritten.
"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING, Any

from bob.llm.types import StreamChunk

if TYPE_CHECKING:
    from collections.abc import AsyncIterator


def is_reasoning_fragment(fragment: Any) -> bool:
    """Is this SDK fragment chain-of-thought (reasoning) rather than content?

    A fragment is reasoning iff its ``reasoning_type`` is anything other than
    ``"none"`` (``"reasoning"`` or the ``reasoningStartTag`` / ``reasoningEndTag``
    markers). A missing / unrecognised attribute defaults to content
    (``reasoning_type == "none"`` semantics) so a malformed fragment degrades to
    plain text rather than vanishing into the cosmetic reasoning channel.
    """

    reasoning_type = getattr(fragment, "reasoning_type", "none")
    return isinstance(reasoning_type, str) and reasoning_type != "none"


def fragment_to_chunk(fragment: Any) -> StreamChunk | None:
    """Map one SDK ``LlmPredictionFragment`` → a single :class:`StreamChunk`.

    - reasoning fragment → ``StreamChunk(kind="reasoning", reasoning_delta=…)``
    - content fragment   → ``StreamChunk(kind="text", text_delta=…)``

    Returns ``None`` for an empty-content fragment so callers never emit a
    no-op chunk (and the byte-identity invariant is unaffected). The cosmetic /
    parse distinction matches the OpenAI transport: reasoning never feeds action
    parsing (issue 0069), only the ``text`` deltas reconstruct the response.
    """

    content = getattr(fragment, "content", None)
    if not isinstance(content, str) or not content:
        return None
    if is_reasoning_fragment(fragment):
        return StreamChunk(kind="reasoning", reasoning_delta=content)
    return StreamChunk(kind="text", text_delta=content)


def build_perf_chunk(
    stats: Any,
    *,
    started: float,
    first_token_at: float | None,
) -> StreamChunk:
    """Build the terminal ``perf`` :class:`StreamChunk` from SDK prediction stats.

    Maps :class:`lmstudio.LlmPredictionStats` (``time_to_first_token_sec`` /
    ``tokens_per_second`` / ``prompt_tokens_count`` / ``predicted_tokens_count``)
    onto the same ``perf`` chunk the OpenAI transport emits via
    :func:`bob.llm_client._build_perf_chunk`, applying the SAME rounding
    conventions (``ttft_s`` to 3 dp, ``tok_s`` to 1 dp).

    The SDK reports ``ttft`` and ``tok/s`` directly, so they are preferred when
    present; we fall back to wall-clock measurement (``first_token_at`` / a local
    generation window) only when the SDK omits a field, so the footer degrades
    softly rather than going blank. ``stats is None`` (stream closed before any
    final result) yields a perf chunk carrying only the measured ``ttft_s``.

    The SDK has no separate reasoning-token count (unlike the OpenAI
    ``completion_tokens_details.reasoning_tokens``), so ``reasoning_tokens`` is
    left ``None``.
    """

    now = time.perf_counter()
    measured_ttft = (first_token_at - started) if first_token_at is not None else None

    tokens_in: int | None = None
    tokens_out: int | None = None
    sdk_ttft: float | None = None
    sdk_tok_s: float | None = None
    if stats is not None:
        tokens_in = getattr(stats, "prompt_tokens_count", None)
        tokens_out = getattr(stats, "predicted_tokens_count", None)
        sdk_ttft = getattr(stats, "time_to_first_token_sec", None)
        sdk_tok_s = getattr(stats, "tokens_per_second", None)

    ttft_s = sdk_ttft if sdk_ttft is not None else measured_ttft

    tok_s = sdk_tok_s
    if tok_s is None and tokens_out and first_token_at is not None:
        gen_window = now - first_token_at
        if gen_window > 0:
            tok_s = tokens_out / gen_window

    return StreamChunk(
        kind="perf",
        tokens_in=tokens_in,
        tokens_out=tokens_out,
        reasoning_tokens=None,
        ttft_s=round(ttft_s, 3) if ttft_s is not None else None,
        tok_s=round(tok_s, 1) if tok_s is not None else None,
    )


async def adapt_prediction_stream(
    fragments: AsyncIterator[Any],
    *,
    stats_getter: Any,
    started: float,
    on_text: Any = None,
) -> AsyncIterator[StreamChunk]:
    """Adapt an SDK fragment stream → Bob ``StreamChunk``s (the M5 core).

    ``fragments`` is any async iterable of SDK ``LlmPredictionFragment``s (the
    SDK ``AsyncPredictionStream`` itself, or a scripted iterator in tests).
    ``stats_getter`` is a zero-arg callable returning the final
    :class:`lmstudio.LlmPredictionStats` (or ``None``) — for the real stream this
    is ``lambda: stream.stats``, only meaningful AFTER the iteration drains.

    Yields, in order: a ``text`` / ``reasoning`` chunk per non-empty fragment,
    then exactly one terminal ``perf`` chunk built from the final stats. The
    first non-empty fragment marks ``ttft`` for the wall-clock fallback.

    ``on_text`` (optional) is invoked with each ``text`` delta string so the
    caller can accumulate the response for its post-stream log WITHOUT
    re-walking the chunks — the byte-identity invariant (concatenated ``text``
    deltas == ``chat()``'s return) holds because both this generator and the
    caller's buffer see the identical content fragments in order.
    """

    first_token_at: float | None = None
    async for fragment in fragments:
        chunk = fragment_to_chunk(fragment)
        if chunk is None:
            continue
        if first_token_at is None:
            first_token_at = time.perf_counter()
        if chunk.kind == "text" and on_text is not None:
            on_text(chunk.text_delta)
        yield chunk

    yield build_perf_chunk(
        stats_getter(),
        started=started,
        first_token_at=first_token_at,
    )


__all__ = [
    "adapt_prediction_stream",
    "build_perf_chunk",
    "fragment_to_chunk",
    "is_reasoning_fragment",
]
