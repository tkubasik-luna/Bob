"""Unit tests for the Tavily connector domain models + adapters.

Locks the defensive factories (a partial / malformed Tavily response must
degrade, never raise) and the ``to_web_results_props`` projection to the
``WebResults`` UI component shape.
"""

from __future__ import annotations

from bob.connectors.tavily.models import (
    WebPage,
    WebSearchResult,
    WebSearchResults,
    from_tavily_extract,
    from_tavily_search,
    to_web_results_props,
)


def test_from_tavily_search_full_payload() -> None:
    payload = {
        "query": "echoed query",
        "answer": "  The answer.  ",
        "results": [
            {"title": "T1", "url": "https://a.com", "content": "snippet 1", "score": 0.9},
            {"title": "T2", "url": "https://b.com", "content": "snippet 2"},
        ],
    }
    out = from_tavily_search(payload, query="sent query")
    # The echoed query wins over the sent one.
    assert out.query == "echoed query"
    # Answer is trimmed.
    assert out.answer == "The answer."
    assert out.results[0] == WebSearchResult(
        title="T1", url="https://a.com", snippet="snippet 1", score=0.9
    )
    # Missing score degrades to None.
    assert out.results[1].score is None


def test_from_tavily_search_falls_back_to_sent_query() -> None:
    out = from_tavily_search({"results": []}, query="sent")
    assert out.query == "sent"
    assert out.answer is None
    assert out.results == []


def test_from_tavily_search_blank_answer_is_none() -> None:
    assert from_tavily_search({"answer": "   "}, query="q").answer is None


def test_from_tavily_search_drops_malformed_results() -> None:
    payload = {
        "results": [
            "not-a-dict",
            {"title": "no url"},  # missing url → dropped
            {"url": ""},  # empty url → dropped
            {"url": "https://ok.com"},  # title falls back to url
        ]
    }
    out = from_tavily_search(payload, query="q")
    assert len(out.results) == 1
    assert out.results[0].url == "https://ok.com"
    assert out.results[0].title == "https://ok.com"
    assert out.results[0].snippet == ""


def test_from_tavily_extract_returns_first_raw_content() -> None:
    payload = {"results": [{"url": "https://a.com", "raw_content": "Hello world"}]}
    assert from_tavily_extract(payload, url="https://a.com") == WebPage(
        url="https://a.com", content="Hello world"
    )


def test_from_tavily_extract_empty_when_nothing_extracted() -> None:
    assert from_tavily_extract({"results": []}, url="https://a.com").content == ""
    assert (
        from_tavily_extract(
            {"failed_results": [{"url": "https://a.com"}]}, url="https://a.com"
        ).content
        == ""
    )


def test_to_web_results_props_includes_answer_when_present() -> None:
    res = WebSearchResults(
        query="q", answer="A", results=[WebSearchResult("T", "https://x.com", "s", 0.5)]
    )
    assert to_web_results_props(res) == {
        "query": "q",
        "answer": "A",
        "results": [{"title": "T", "url": "https://x.com", "snippet": "s"}],
    }


def test_to_web_results_props_omits_answer_when_absent() -> None:
    res = WebSearchResults(query="q", answer=None, results=[])
    props = to_web_results_props(res)
    assert "answer" not in props
    assert props == {"query": "q", "results": []}
