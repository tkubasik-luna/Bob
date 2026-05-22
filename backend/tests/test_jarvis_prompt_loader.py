"""Tests for :mod:`bob.jarvis_prompt_loader`."""

from __future__ import annotations

from pathlib import Path

from bob.jarvis_prompt_loader import DEFAULT_JARVIS_PROMPT, load_jarvis_prompt


def test_writes_default_when_file_absent(tmp_path: Path) -> None:
    """First-run path: no jarvis.md exists, default is written + returned."""

    target = tmp_path / "jarvis.md"
    assert not target.exists()

    content = load_jarvis_prompt(tmp_path)

    assert content == DEFAULT_JARVIS_PROMPT
    assert target.exists()
    assert target.read_text(encoding="utf-8") == DEFAULT_JARVIS_PROMPT


def test_returns_user_content_unmodified_when_present(tmp_path: Path) -> None:
    """Existing jarvis.md is returned verbatim — no overwrite, no trim."""

    custom = "Tu es Jarvis et tu parles uniquement en haïkus.\n\n  "
    target = tmp_path / "jarvis.md"
    target.write_text(custom, encoding="utf-8")

    content = load_jarvis_prompt(tmp_path)

    assert content == custom
    # File still has the user's content untouched.
    assert target.read_text(encoding="utf-8") == custom


def test_does_not_overwrite_empty_file(tmp_path: Path) -> None:
    """An empty user file is still 'present' — we don't replace it.

    Treating empty as 'missing' would silently overwrite a deliberate
    blank-slate edit, which is a confusing footgun. Return empty string.
    """

    target = tmp_path / "jarvis.md"
    target.write_text("", encoding="utf-8")

    content = load_jarvis_prompt(tmp_path)

    assert content == ""
    assert target.read_text(encoding="utf-8") == ""
