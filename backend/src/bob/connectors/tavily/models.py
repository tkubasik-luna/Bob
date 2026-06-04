"""Domain models for the Tavily connector + adapters to UI component props.

Internal Tavily JSON shapes never leak past :mod:`bob.connectors.tavily`:
:meth:`TavilyClient.search` returns :class:`WebSearchResults` and
:meth:`TavilyClient.extract` returns :class:`WebPage`, both built by the pure
``from_tavily_*`` factories here. Every factory is defensive — a missing or
malformed field degrades to a sane default rather than raising, so a partial
Tavily response still yields a usable object (PRD 0010 robustness bar).

The :func:`to_web_results_props` adapter projects a search into the dict the
``WebResults`` UI component expects (see ``bob.ui_registry.WEB_RESULTS``).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class WebSearchResult:
    """A single web search hit (one row of the ``WebResults`` card)."""

    title: str
    url: str
    snippet: str
    #: Tavily relevance score in ``[0, 1]`` when present; purely informational.
    score: float | None = None


@dataclass(frozen=True)
class WebSearchResults:
    """The full result of one ``web_search`` call.

    ``answer`` is Tavily's optional LLM-generated direct answer (present when
    ``include_answer`` is set and Tavily could synthesise one); ``None`` when
    absent or blank. ``results`` preserves Tavily's ranking order.
    """

    query: str
    answer: str | None
    results: list[WebSearchResult] = field(default_factory=list)


@dataclass(frozen=True)
class WebPage:
    """Extracted readable content of one URL (``web_fetch``)."""

    url: str
    content: str


def from_tavily_search(payload: dict[str, Any], *, query: str) -> WebSearchResults:
    """Build :class:`WebSearchResults` from a Tavily ``/search`` response.

    ``query`` is the query we sent; it backstops the value Tavily echoes so
    the result always carries a non-empty query. Each result needs a usable
    ``url`` to be kept (a hit with no URL is unrenderable and dropped); the
    title falls back to the URL, and the snippet/score degrade to ``""`` /
    ``None`` when malformed.
    """

    answer_raw = payload.get("answer")
    answer = answer_raw.strip() if isinstance(answer_raw, str) and answer_raw.strip() else None

    raw_results = payload.get("results")
    raw_results = raw_results if isinstance(raw_results, list) else []
    results: list[WebSearchResult] = []
    for item in raw_results:
        if not isinstance(item, dict):
            continue
        url = item.get("url")
        if not isinstance(url, str) or not url:
            continue
        title = item.get("title")
        content = item.get("content")
        score = item.get("score")
        results.append(
            WebSearchResult(
                title=title if isinstance(title, str) and title else url,
                url=url,
                snippet=content if isinstance(content, str) else "",
                score=float(score) if isinstance(score, (int, float)) else None,
            )
        )

    echoed = payload.get("query")
    return WebSearchResults(
        query=echoed if isinstance(echoed, str) and echoed else query,
        answer=answer,
        results=results,
    )


def from_tavily_extract(payload: dict[str, Any], *, url: str) -> WebPage:
    """Build :class:`WebPage` from a Tavily ``/extract`` response.

    Returns the first successfully extracted ``raw_content`` (Tavily returns a
    ``results`` list — we request a single URL so there is at most one). When
    nothing was extracted (the URL landed in ``failed_results``, or the shape
    is unexpected) the content is empty; the client treats that as a failure
    so the handler reports ``web_fetch_failed`` rather than surfacing a blank
    page.
    """

    results = payload.get("results")
    results = results if isinstance(results, list) else []
    for item in results:
        if not isinstance(item, dict):
            continue
        raw = item.get("raw_content")
        if isinstance(raw, str):
            got_url = item.get("url")
            return WebPage(
                url=got_url if isinstance(got_url, str) and got_url else url,
                content=raw,
            )
    return WebPage(url=url, content="")


def to_web_results_props(results: WebSearchResults) -> dict[str, Any]:
    """Project :class:`WebSearchResults` to ``WebResults`` UI component props.

    Mirrors :func:`bob.connectors.gmail.models.to_mail_props`. The shape is the
    contract enforced by ``bob.ui_registry.WEB_RESULTS``: ``query`` + an
    ordered ``results`` array of ``{title, url, snippet}`` + an optional
    ``answer``. ``answer`` is omitted entirely (not sent as ``null``) when
    absent so the strict ``additionalProperties: false`` schema stays happy.
    """

    props: dict[str, Any] = {
        "query": results.query,
        "results": [
            {"title": r.title, "url": r.url, "snippet": r.snippet} for r in results.results
        ],
    }
    if results.answer:
        props["answer"] = results.answer
    return props


__all__ = [
    "WebPage",
    "WebSearchResult",
    "WebSearchResults",
    "from_tavily_extract",
    "from_tavily_search",
    "to_web_results_props",
]
