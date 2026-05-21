"""Tests for :mod:`bob.spoken_text_cleaner` (issue 0012).

Pure-logic markdown → spoken-text transform. URL policy: strip.
"""

from __future__ import annotations

from bob.spoken_text_cleaner import clean_for_speech


def test_blank_in_blank_out() -> None:
    assert clean_for_speech("") == ""
    assert clean_for_speech("   \n\t  ") == ""


def test_plain_prose_idempotent() -> None:
    prose = "Hello world. How are you today?"
    assert clean_for_speech(prose) == prose


def test_strips_fenced_code_block_entirely() -> None:
    text = "Voici le code:\n```python\nprint('hi')\nfor x in y:\n    pass\n```\nFin."
    out = clean_for_speech(text)
    assert "print" not in out
    assert "pass" not in out
    assert "```" not in out
    assert "Voici le code:" in out
    assert "Fin." in out


def test_strips_emphasis_markers() -> None:
    assert clean_for_speech("This is **emphasis** here.") == "This is emphasis here."
    assert clean_for_speech("Or _italic_ word.") == "Or italic word."
    assert clean_for_speech("And ~~struck~~ out.") == "And struck out."
    assert clean_for_speech("Mixed *one* and **two** done.") == "Mixed one and two done."


def test_strips_headings_and_inline_backticks() -> None:
    text = "# Title\nSome `code_var` here."
    assert clean_for_speech(text) == "Title\nSome code_var here."


def test_bullets_become_prose() -> None:
    text = "- premier item\n- second item\n* troisième\n1. quatrième\n2. cinquième"
    out = clean_for_speech(text)
    assert "-" not in out
    assert "*" not in out
    # Items become bare lines, no "dash"/"asterisk" injected.
    assert "premier item" in out
    assert "cinquième" in out


def test_urls_stripped() -> None:
    text = "Voir https://example.com/foo?bar=1 pour plus."
    out = clean_for_speech(text)
    assert "http" not in out
    assert "example.com" not in out
    assert "Voir" in out and "pour plus." in out


def test_mixed_paragraph_preserves_paragraph_breaks() -> None:
    text = (
        "# Plan\n"
        "Voici **les étapes**:\n\n"
        "- étape une\n"
        "- étape deux\n\n"
        "```\nignored code\n```\n\n"
        "Conclusion finale."
    )
    out = clean_for_speech(text)
    assert "ignored code" not in out
    assert "**" not in out and "#" not in out
    # Paragraph break preserved between the intro and the conclusion.
    assert "\n\n" in out
    assert "Conclusion finale." in out
