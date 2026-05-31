"""Tests for date-awareness helpers.

Covers the two pieces added to stop local models hallucinating a stale year
in ``gmail_search`` (and answering "quel jour on est ?" badly):

- :func:`bob.sub_agent.tool_registry._resolve_relative_date` — resolves
  ``today`` / ``hier`` tokens server-side against an injected reference.
- :func:`bob.context.prompt_fragments.temporal_context_fragment` — the
  current-date line injected into the Jarvis and sub-agent system prompts.
"""

from __future__ import annotations

from datetime import date, datetime

import pytest

from bob.context.prompt_fragments import temporal_context_fragment
from bob.sub_agent.tool_registry import _resolve_relative_date

_REF = date(2026, 5, 30)


@pytest.mark.parametrize(
    ("token", "expected"),
    [
        ("today", "2026-05-30"),
        ("Today", "2026-05-30"),
        ("aujourd'hui", "2026-05-30"),
        ("AUJOURDHUI", "2026-05-30"),
        ("yesterday", "2026-05-29"),
        ("hier", "2026-05-29"),
        ("  hier  ", "2026-05-29"),
    ],
)
def test_resolve_relative_tokens(token: str, expected: str) -> None:
    assert _resolve_relative_date(token, today=_REF) == expected


def test_resolve_passes_absolute_date_through() -> None:
    assert _resolve_relative_date("2026-01-15", today=_REF) == "2026-01-15"


def test_resolve_passes_iso_datetime_through() -> None:
    # Not a relative token → returned verbatim; query builder strips the time.
    assert _resolve_relative_date("2026-05-30T00:00:00Z", today=_REF) == ("2026-05-30T00:00:00Z")


def test_resolve_none_is_none() -> None:
    assert _resolve_relative_date(None, today=_REF) is None


def test_temporal_fragment_carries_iso_and_french_date() -> None:
    moment = datetime(2026, 5, 30, 14, 0, 0)
    fragment = temporal_context_fragment(now=moment)
    assert "2026-05-30" in fragment  # ISO date for tool args
    assert "samedi 30 mai 2026" in fragment  # human form for "quel jour ?"
