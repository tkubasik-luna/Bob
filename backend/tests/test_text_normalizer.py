# ruff: noqa: RUF001
"""Tests for the FR-aware TTS text normalizer."""

from __future__ import annotations

import pytest

from bob.text_normalizer import normalize_for_tts


def test_empty_returns_empty() -> None:
    assert normalize_for_tts("") == ""
    assert normalize_for_tts("   \n\t  ") == ""


def test_preserves_plain_french() -> None:
    src = "Bonjour, je m'appelle Bob et j'aime le café."
    assert normalize_for_tts(src) == src


def test_collapses_typographic_quotes() -> None:
    src = "Il a dit « bonjour » à l’élève."
    out = normalize_for_tts(src)
    # Guillemets become ASCII double quotes (then stripped because " is not in
    # safe set); curly apostrophe becomes ASCII '.
    assert "«" not in out and "»" not in out
    assert "’" not in out
    assert "l'élève" in out
    assert "bonjour" in out


def test_em_dash_becomes_comma() -> None:
    out = normalize_for_tts("Un mot — puis un autre")
    assert "—" not in out
    assert "," in out
    assert "Un mot" in out
    assert "puis un autre" in out


def test_ellipsis_collapses_to_period() -> None:
    assert normalize_for_tts("Attends…") == "Attends."
    assert normalize_for_tts("Hmm... bon.") == "Hmm. bon."


def test_strips_emoji_and_weird_symbols() -> None:
    out = normalize_for_tts("Salut 😀 ✨ Bob")
    assert "😀" not in out
    assert "✨" not in out
    assert "Salut" in out and "Bob" in out


def test_keeps_accented_letters() -> None:
    src = "À l'été prochain, après le déjeuner çà et là."
    assert normalize_for_tts(src) == src


def test_keeps_currency() -> None:
    assert "€" in normalize_for_tts("Ça coûte 3,14 €.")


def test_collapses_nbsp() -> None:
    # Non-breaking space + narrow NBSP should both collapse to single ASCII space.
    src = "Bob : bonjour"
    out = normalize_for_tts(src)
    assert " " not in out
    assert " " not in out
    assert out == "Bob : bonjour"


def test_collapses_multiple_dots() -> None:
    assert normalize_for_tts("ok.... fini") == "ok. fini"


def test_idempotent() -> None:
    src = "Hé !! C'était « génial » — vraiment 😀…"
    once = normalize_for_tts(src)
    twice = normalize_for_tts(once)
    assert once == twice


@pytest.mark.parametrize(
    "src",
    [
        "Bonjour.",
        "Une phrase simple sans rien de bizarre !",
        "Il a payé 12,50 € pour ça.",
    ],
)
def test_safe_inputs_unchanged(src: str) -> None:
    assert normalize_for_tts(src) == src


def test_math_symbols_stripped() -> None:
    # `+`, `=`, `<`, `>` are dropped: they're noise for prose TTS and espeak
    # FR doesn't pronounce them consistently. The numbers/letters survive.
    out = normalize_for_tts("12 + 3 = 15")
    assert "+" not in out and "=" not in out
    assert "12" in out and "3" in out and "15" in out
