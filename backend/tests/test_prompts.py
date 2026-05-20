"""Tests for :mod:`bob.prompts`."""

from __future__ import annotations

from bob import prompts


def test_system_chat_prompt_is_loaded() -> None:
    assert "system_chat" in prompts.available()


def test_render_interpolates_kwargs() -> None:
    rendered = prompts.render("system_chat", components_description="X")
    assert "X" in rendered


def test_render_unknown_prompt_raises() -> None:
    import pytest

    with pytest.raises(KeyError):
        prompts.render("does_not_exist")
