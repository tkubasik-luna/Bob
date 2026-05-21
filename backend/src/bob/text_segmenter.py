"""Sentence segmenter for streaming TTS.

Pure logic, no I/O, no external dependencies. Splits text into sentences on
terminal punctuation (``.``, ``!``, ``?``) followed by whitespace / end-of-stream,
or on a double newline (``\\n\\n``).

Two APIs are exposed:

- :func:`segment` — one-shot: split a full string into sentences.
- :class:`SentenceBuffer` — incremental: feed chunks via :meth:`SentenceBuffer.push`,
  it returns any newly completed sentences as soon as a boundary appears. Call
  :meth:`SentenceBuffer.flush` at end-of-stream to retrieve any trailing
  remainder (which may have no terminal punctuation).

Design note (issue 0011). The PRD envisions LLM delta tokens flowing through
this segmenter and being synthesized as soon as each sentence completes. The
current ``bob.llm_client`` does NOT expose a token-delta stream, and the
assistant response is structured JSON validated against a schema — so true
mid-LLM-stream segmentation is not yet wired. For now the WS router feeds the
already-parsed ``speech`` field into ``segment`` (one-shot) and pipelines TTS
across sentences with ``asyncio.create_task``. The incremental
:class:`SentenceBuffer` is ready for a future LLM-client refactor that exposes
deltas.

No markdown / code-block cleanup is performed here — that is issue 0012.
"""

from __future__ import annotations

from dataclasses import dataclass, field

_TERMINALS: frozenset[str] = frozenset(".!?")


def segment(text: str) -> list[str]:
    """Split ``text`` into sentences.

    A sentence ends at:

    - a terminal punctuation char (``.``, ``!``, ``?``) that is followed by
      whitespace OR is the very last non-whitespace character of the input;
    - a double newline (``\\n\\n``) — the preceding content is emitted as one
      sentence (without the newlines).

    Whitespace at the boundaries of each sentence is trimmed. Empty / whitespace-
    only fragments are skipped. Terminal punctuation is preserved on the
    sentence it closes.
    """

    buf = SentenceBuffer()
    sentences = buf.push(text)
    sentences.extend(buf.flush())
    return sentences


@dataclass
class SentenceBuffer:
    """Incremental sentence accumulator.

    Feed arbitrary text chunks via :meth:`push`. Each call returns the list of
    sentences that became complete *because of* that chunk (often empty).
    When the producing stream is exhausted, call :meth:`flush` to drain any
    trailing remainder as the final sentence.
    """

    _pending: str = field(default="", init=False)

    def push(self, chunk: str) -> list[str]:
        """Append ``chunk`` to the internal buffer and return newly completed sentences.

        A sentence is emitted as soon as a terminal punctuation is followed by
        whitespace, or a ``\\n\\n`` boundary is observed. The character that
        triggered the boundary (whitespace / second newline) is consumed and
        does not appear in any sentence; terminal punctuation is preserved.
        """

        if not chunk:
            return []
        self._pending += chunk

        out: list[str] = []
        start = 0
        i = 0
        text = self._pending
        n = len(text)
        while i < n:
            ch = text[i]
            if ch in _TERMINALS:
                # Need a following whitespace char to confirm boundary mid-stream.
                if i + 1 < n and text[i + 1].isspace():
                    sentence = text[start : i + 1].strip()
                    if sentence:
                        out.append(sentence)
                    # Skip the whitespace separator.
                    i += 2
                    start = i
                    continue
                # Terminal at very end of buffer — defer; might be followed by
                # whitespace in a future chunk, or be the final char on flush.
            elif ch == "\n" and i + 1 < n and text[i + 1] == "\n":
                sentence = text[start:i].strip()
                if sentence:
                    out.append(sentence)
                # Skip both newlines.
                i += 2
                # Also skip any further consecutive whitespace.
                while i < n and text[i].isspace():
                    i += 1
                start = i
                continue
            i += 1

        self._pending = text[start:]
        return out

    def flush(self) -> list[str]:
        """Return any trailing remainder as a final sentence (or an empty list).

        After this call the buffer is empty.
        """

        remainder = self._pending.strip()
        self._pending = ""
        if remainder:
            return [remainder]
        return []
