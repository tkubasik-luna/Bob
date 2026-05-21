"""Tests for :mod:`bob.text_segmenter`.

Minimal coverage of boundary cases. Issue 0012 will extend the segmenter with
markdown / code-block cleanup and add the corresponding tests.
"""

from __future__ import annotations

from bob.text_segmenter import SentenceBuffer, segment


def test_segment_splits_on_terminal_punctuation() -> None:
    assert segment("Hello world. How are you? Fine!") == [
        "Hello world.",
        "How are you?",
        "Fine!",
    ]


def test_segment_flushes_trailing_remainder_without_punctuation() -> None:
    assert segment("First sentence. Trailing fragment no dot") == [
        "First sentence.",
        "Trailing fragment no dot",
    ]


def test_segment_splits_on_double_newline() -> None:
    assert segment("Paragraph one\n\nParagraph two") == [
        "Paragraph one",
        "Paragraph two",
    ]


def test_segment_counting_example() -> None:
    # Mirrors the acceptance example: 5 short sentences separated by space.
    text = "Un. Deux. Trois. Quatre. Cinq."
    assert segment(text) == ["Un.", "Deux.", "Trois.", "Quatre.", "Cinq."]


def test_sentence_buffer_incremental() -> None:
    buf = SentenceBuffer()
    # Boundary not yet seen — terminal char without trailing whitespace.
    assert buf.push("Hello world.") == []
    # Space arrives in next chunk, sentence completes.
    assert buf.push(" Next") == ["Hello world."]
    # Another sentence completes mid-chunk.
    assert buf.push(" bit. And more") == ["Next bit."]
    # Flush the dangling remainder.
    assert buf.flush() == ["And more"]
    assert buf.flush() == []
