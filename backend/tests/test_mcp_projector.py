"""Unit tests for :func:`bob.connectors.mcp.projector.project_mcp_default` (pure).

Locks the generic MCP projection contract reused by every uncurated tool:

- a ``Markdown`` deliverable card that VALIDATES against the one ``ui_registry``
  schema (no new render path);
- the transcript digest caps the tool text + flags truncation;
- a deterministic French summary;
- non-terminal by default (an uncurated MCP tool never converges).
"""

from __future__ import annotations

from bob.connectors.mcp.projector import project_mcp_default
from bob.ui_registry import validate_component_descriptor


def test_builds_valid_markdown_card() -> None:
    proj = project_mcp_default({"tool": "get_weather", "text": "Sunny, 25C."})
    assert proj.terminal is False
    assert proj.deliverable is not None
    card = proj.deliverable[0]
    assert card["component"] == "Markdown"
    assert "get_weather" in card["props"]["content"]
    assert "Sunny, 25C." in card["props"]["content"]
    assert validate_component_descriptor(card) == []


def test_summary_names_the_tool() -> None:
    proj = project_mcp_default({"tool": "get_weather", "text": "x"})
    assert "get_weather" in proj.summary


def test_digest_caps_and_flags_truncation() -> None:
    proj = project_mcp_default({"tool": "t", "text": "y" * 5000})
    assert proj.digest["truncated"] is True
    assert proj.digest["text"].endswith("…")
    # 4000-char cap + the ellipsis.
    assert len(proj.digest["text"]) <= 4001


def test_short_text_is_not_truncated() -> None:
    proj = project_mcp_default({"tool": "t", "text": "short"})
    assert proj.digest["truncated"] is False
    assert proj.digest["text"] == "short"


def test_empty_text_has_no_card() -> None:
    proj = project_mcp_default({"tool": "t", "text": "   "})
    assert proj.deliverable is None
    assert "aucun contenu" in proj.summary
    assert proj.terminal is False


def test_malformed_result_does_not_crash() -> None:
    proj = project_mcp_default({"text": 123, "tool": None})
    # Falls back to a generic tool label and empty text → no card, no raise.
    assert proj.deliverable is None
    assert proj.terminal is False
