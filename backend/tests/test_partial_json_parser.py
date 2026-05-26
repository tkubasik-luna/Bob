"""Contract tests for :class:`bob.streaming.PartialJsonParser` (issue 0049).

These tests pin the behaviour we rely on from the underlying
``partial-json-parser`` library so a future upgrade can be caught in CI.
We exercise four classes of input explicitly named in the issue
acceptance criteria:

- UTF-8 split mid-codepoint (the parser sees a fully decoded ``str`` so
  the test feeds an intermediate code-unit boundary).
- Escaped quotes inside ``speech``.
- Nested objects in ``ui``.
- Trailing-comma tolerance (our small divergence from the upstream
  defaults — see the parser docstring for the rationale).
"""

from __future__ import annotations

import pytest

from bob.streaming.partial_json_parser import PartialJsonParser


def test_empty_buffer_returns_none() -> None:
    parser = PartialJsonParser()
    assert parser.parse("") is None


def test_non_object_root_returns_none() -> None:
    """Strings/numbers/lists at the root are ignored — ``say.args`` is a dict."""

    parser = PartialJsonParser()
    assert parser.parse('"just a string"') is None
    assert parser.parse("42") is None
    assert parser.parse('["a", "b"]') is None


def test_growing_string_yields_growing_prefix() -> None:
    """A streamed ``speech`` field grows by suffix on every tick."""

    parser = PartialJsonParser()
    snapshots: list[str | None] = []
    for buf in [
        '{"speech":',
        '{"speech":"',
        '{"speech":"He',
        '{"speech":"Hel',
        '{"speech":"Hell',
        '{"speech":"Hello',
        '{"speech":"Hello"',
        '{"speech":"Hello","ui":null}',
    ]:
        parsed = parser.parse(buf)
        snapshots.append(parsed.get("speech") if isinstance(parsed, dict) else None)
    # Empty value of ``speech`` shows up as ``""`` once the opening quote
    # has been seen but no content yet; we don't assert on that
    # intermediate exactly because the library is free to skip it.
    assert "He" in snapshots
    assert "Hel" in snapshots
    assert "Hello" in snapshots
    # The full close payload still resolves.
    assert snapshots[-1] == "Hello"


def test_utf8_multibyte_character_in_speech() -> None:
    """Multi-byte UTF-8 (é, à, …) decodes correctly."""

    parser = PartialJsonParser()
    # Truncated mid-word — string is open with no closing quote yet.
    parsed = parser.parse('{"speech":"hé')
    assert parsed is not None
    assert parsed["speech"] == "hé"

    # Completed.
    parsed = parser.parse('{"speech":"héllo"}')
    assert parsed is not None
    assert parsed["speech"] == "héllo"


def test_escaped_quotes_inside_speech() -> None:
    """``\\"`` inside a JSON string round-trips to a literal ``"``."""

    parser = PartialJsonParser()
    parsed = parser.parse('{"speech":"He said \\"hi\\""}')
    assert parsed is not None
    assert parsed["speech"] == 'He said "hi"'


def test_truncated_escape_does_not_corrupt() -> None:
    """A truncated ``\\`` doesn't crash; library returns the resolved prefix."""

    parser = PartialJsonParser()
    parsed = parser.parse('{"speech":"abc\\')
    # The library is allowed to either drop the dangling escape or
    # surface a stub; either is acceptable as long as we don't raise.
    if parsed is not None:
        assert "speech" in parsed
        # The prefix before the escape MUST be visible.
        assert "abc" in str(parsed["speech"])


def test_nested_ui_object_incremental() -> None:
    """``ui`` is parsed as a nested dict on every tick that has it visible."""

    parser = PartialJsonParser()
    parsed = parser.parse('{"speech":"hi","ui":{"component":"Markdown","props":{"content":"abc')
    assert parsed is not None
    assert parsed["speech"] == "hi"
    ui = parsed.get("ui")
    assert isinstance(ui, dict)
    assert ui["component"] == "Markdown"
    assert ui["props"]["content"] == "abc"


def test_complete_payload_parses_unchanged() -> None:
    """Full valid JSON parses identically to :func:`json.loads`."""

    import json

    raw = '{"speech":"hi","ui":{"component":"Markdown","props":{"content":"abc"}}}'
    parser = PartialJsonParser()
    parsed = parser.parse(raw)
    assert parsed == json.loads(raw)


def test_trailing_comma_tolerated_by_default() -> None:
    """Default mode strips a stray trailing comma before ``}`` / ``]``."""

    parser = PartialJsonParser()  # tolerate_trailing_comma=True
    parsed = parser.parse('{"speech":"hi","ui":null,}')
    assert parsed is not None
    assert parsed == {"speech": "hi", "ui": None}


def test_trailing_comma_can_be_strict() -> None:
    """Disabling the divergence falls back to the library's strict mode."""

    parser = PartialJsonParser(tolerate_trailing_comma=False)
    # Library returns ``None`` (or raises) on a trailing comma; our
    # wrapper swallows the raise and returns ``None``.
    parsed = parser.parse('{"speech":"hi",}')
    assert parsed is None


def test_malformed_buffer_returns_none() -> None:
    """A complete garbage prefix doesn't crash; returns ``None``."""

    parser = PartialJsonParser()
    # ``zzz`` doesn't start with ``{`` or ``[`` — library bails.
    assert parser.parse("zzz") is None


def test_speech_value_with_newlines_and_escapes() -> None:
    """Literal ``\\n`` decodes to a real newline."""

    parser = PartialJsonParser()
    parsed = parser.parse('{"speech":"line1\\nline2"}')
    assert parsed is not None
    assert parsed["speech"] == "line1\nline2"


@pytest.mark.parametrize(
    "buf",
    [
        '{"',
        '{"speech',
        '{"speech"',
        '{"speech":',
        '{"speech":"',
        '{"speech":""',
        '{"speech":"x"',
        '{"speech":"x",',
        '{"speech":"x","ui":',
    ],
)
def test_partial_at_every_boundary_is_safe(buf: str) -> None:
    """No exception on any prefix — the parser is "always recoverable"."""

    PartialJsonParser().parse(buf)
