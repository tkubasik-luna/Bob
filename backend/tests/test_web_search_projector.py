"""Unit tests for ``project_web_search`` / ``project_web_fetch`` (PRD: web-search).

Locks the projection contract the runner depends on:

- web_search is NON-terminal (it starts a research chain — search → optional
  fetch → synthesise — so the runner must not converge on it);
- the deliverable is a single ``WebResults`` card that VALIDATES against the
  one ``ui_registry`` schema, present even mid-chain so a stall surfaces the
  sources instead of an empty overlay (PRD 0010 anti-stall);
- the digest keeps snippets (the model's working material) but caps them;
- web_fetch emits a Markdown "page I read" card (anti-stall) + a capped digest.
"""

from __future__ import annotations

from typing import Any

from bob.sub_agent.tool_registry import (
    build_web_fetch_tool,
    build_web_search_tool,
    project_web_fetch,
    project_web_search,
)
from bob.ui_registry import validate_component_descriptor


def _props(answer: str | None = "Direct answer.", n: int = 2) -> dict[str, Any]:
    results = [
        {"title": f"T{i}", "url": f"https://s{i}.com", "snippet": f"snippet {i}"} for i in range(n)
    ]
    props: dict[str, Any] = {"query": "python gil", "results": results}
    if answer is not None:
        props["answer"] = answer
    return props


def test_web_search_non_empty_builds_valid_card() -> None:
    proj = project_web_search({"count": 2, "props": _props()})
    # NON-terminal — web_search starts a chain; the runner never converges on it.
    assert proj.terminal is False
    assert proj.deliverable == [{"component": "WebResults", "props": _props()}]
    assert proj.deliverable is not None
    assert validate_component_descriptor(proj.deliverable[0]) == []


def test_web_search_summary_prefers_answer() -> None:
    proj = project_web_search({"count": 2, "props": _props(answer="42 is the answer.")})
    assert proj.summary == "42 is the answer."


def test_web_search_summary_counts_when_no_answer() -> None:
    proj = project_web_search({"count": 2, "props": _props(answer=None)})
    assert "2 résultat" in proj.summary


def test_web_search_empty_has_no_card() -> None:
    proj = project_web_search({"count": 0, "props": {"query": "nope", "results": []}})
    assert proj.deliverable is None
    assert "Aucun résultat" in proj.summary
    assert proj.terminal is False


def test_web_search_digest_keeps_snippets_but_caps() -> None:
    big: dict[str, Any] = {
        "query": "q",
        "results": [
            {"title": f"T{i}", "url": f"https://s{i}.com", "snippet": "x" * 500} for i in range(12)
        ],
    }
    proj = project_web_search({"count": 12, "props": big})
    # Capped to 8 rows…
    assert len(proj.digest["results"]) == 8
    # …count reported faithfully…
    assert proj.digest["count"] == 12
    # …snippet KEPT (web_search is non-terminal, the model reads them) but
    # length-capped at 300 chars + ellipsis.
    assert proj.digest["results"][0]["snippet"].endswith("…")
    assert len(proj.digest["results"][0]["snippet"]) <= 301


def test_web_search_malformed_does_not_crash() -> None:
    proj = project_web_search({"props": "oops"})
    assert proj.deliverable is None
    assert proj.terminal is False


def test_web_fetch_builds_markdown_card() -> None:
    proj = project_web_fetch({"url": "https://x.com", "content": "Hello body."})
    assert proj.terminal is False
    assert proj.deliverable is not None
    section = proj.deliverable[0]
    assert section["component"] == "Markdown"
    assert "https://x.com" in section["props"]["content"]
    assert validate_component_descriptor(section) == []
    assert proj.summary == "Page lue : https://x.com"


def test_web_fetch_digest_caps_and_flags_truncation() -> None:
    proj = project_web_fetch({"url": "https://x.com", "content": "y" * 7000})
    assert proj.digest["truncated"] is True
    assert proj.digest["content"].endswith("…")
    assert len(proj.digest["content"]) <= 6001


def test_web_fetch_builders_wire_projectors() -> None:
    assert build_web_search_tool().result_projector is project_web_search
    assert build_web_fetch_tool().result_projector is project_web_fetch
