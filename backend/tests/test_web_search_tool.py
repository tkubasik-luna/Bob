"""Unit tests for the ``web_search`` / ``web_fetch`` sub-agent tools.

Two layers, mirroring the gmail tool tests:

- :class:`WebSearchArgs` / :class:`WebFetchArgs` Pydantic validation;
- the handlers end-to-end — happy path returns ``WebResults``-shaped props
  that validate against the single ``ui_registry`` schema, and every connector
  exception folds into a structured ``web_search_*`` / ``web_fetch_*`` error
  (the handler never raises through the dispatcher).

The Tavily HTTP layer is stubbed at the connector boundary: the lazily-imported
``bob.connectors.tavily.TavilyClient`` is monkey-patched to a fake whose
``search`` / ``extract`` return canned objects or raise. We never touch httpx.
"""

from __future__ import annotations

from typing import Any, ClassVar

import pytest
from pydantic import ValidationError

from bob.connectors.tavily import (
    ApiUnreachableError,
    MissingApiKeyError,
    RateLimitedError,
    TavilyError,
    UnauthorizedError,
    WebPage,
    WebSearchResult,
    WebSearchResults,
)
from bob.sub_agent.tool_registry import (
    WebFetchArgs,
    WebSearchArgs,
    _web_fetch_handler,
    _web_search_handler,
    build_default_subagent_registry,
    build_web_fetch_tool,
    build_web_search_tool,
    project_web_fetch,
    project_web_search,
)
from bob.ui_registry import validate_component_descriptor


class _StubContext:
    """Minimal :class:`SubAgentToolHandlerContext` implementation for tests."""

    task_id = "task-test"
    state: ClassVar[dict[str, Any]] = {}


def _patch_tavily(
    monkeypatch: pytest.MonkeyPatch,
    *,
    search: WebSearchResults | None = None,
    extract: WebPage | None = None,
    exc: Exception | None = None,
) -> None:
    """Replace the lazily-imported ``TavilyClient`` with a canned fake."""

    class _FakeClient:
        def __init__(self, *args: Any, **kwargs: Any) -> None: ...

        async def search(self, query: str, **kwargs: Any) -> WebSearchResults:
            if exc is not None:
                raise exc
            assert search is not None
            return search

        async def extract(self, url: str) -> WebPage:
            if exc is not None:
                raise exc
            assert extract is not None
            return extract

    monkeypatch.setattr("bob.connectors.tavily.TavilyClient", _FakeClient)


# --- args validation --------------------------------------------------------


def test_web_search_args_defaults() -> None:
    args = WebSearchArgs(query="hello")
    assert args.query == "hello"
    assert args.max_results is None


@pytest.mark.parametrize(
    "kwargs",
    [{"query": ""}, {"query": "x", "max_results": 0}, {"query": "x", "max_results": 11}],
)
def test_web_search_args_invalid(kwargs: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        WebSearchArgs(**kwargs)


def test_web_fetch_args_requires_http_url() -> None:
    assert WebFetchArgs(url="https://x.com").url == "https://x.com"
    assert WebFetchArgs(url="http://x.com").url == "http://x.com"
    for bad in ("ftp://x.com", "notaurl", "", "   "):
        with pytest.raises(ValidationError):
            WebFetchArgs(url=bad)


# --- web_search handler -----------------------------------------------------


async def test_web_search_handler_success(monkeypatch: pytest.MonkeyPatch) -> None:
    results = WebSearchResults(
        query="q", answer="A", results=[WebSearchResult("T", "https://x.com", "snip")]
    )
    _patch_tavily(monkeypatch, search=results)
    outcome = await _web_search_handler(_StubContext(), WebSearchArgs(query="q"))
    assert outcome.status == "ok"
    assert outcome.result["count"] == 1
    # The card the runner ships validates against the SAME ui_registry schema
    # the `say` tool uses — no second hand-written WebResults schema.
    props = outcome.result["props"]
    assert validate_component_descriptor({"component": "WebResults", "props": props}) == []


@pytest.mark.parametrize(
    ("exc", "code"),
    [
        (MissingApiKeyError("no key"), "web_search_missing_key"),
        (UnauthorizedError("bad"), "web_search_unauthorized"),
        (RateLimitedError("slow"), "web_search_rate_limited"),
        (ApiUnreachableError("net"), "web_search_api_unreachable"),
        (TavilyError("boom"), "web_search_failed"),
    ],
)
async def test_web_search_handler_error_mapping(
    monkeypatch: pytest.MonkeyPatch, exc: Exception, code: str
) -> None:
    _patch_tavily(monkeypatch, exc=exc)
    outcome = await _web_search_handler(_StubContext(), WebSearchArgs(query="q"))
    assert outcome.status == "error"
    assert outcome.error_code == code


# --- web_fetch handler ------------------------------------------------------


async def test_web_fetch_handler_success(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_tavily(monkeypatch, extract=WebPage(url="https://x.com", content="page body"))
    outcome = await _web_fetch_handler(_StubContext(), WebFetchArgs(url="https://x.com"))
    assert outcome.status == "ok"
    assert outcome.result == {"url": "https://x.com", "content": "page body"}


@pytest.mark.parametrize(
    ("exc", "code"),
    [
        (MissingApiKeyError("no key"), "web_fetch_missing_key"),
        (UnauthorizedError("bad"), "web_fetch_unauthorized"),
        (RateLimitedError("slow"), "web_fetch_rate_limited"),
        (ApiUnreachableError("net"), "web_fetch_api_unreachable"),
        (TavilyError("boom"), "web_fetch_failed"),
    ],
)
async def test_web_fetch_handler_error_mapping(
    monkeypatch: pytest.MonkeyPatch, exc: Exception, code: str
) -> None:
    _patch_tavily(monkeypatch, exc=exc)
    outcome = await _web_fetch_handler(_StubContext(), WebFetchArgs(url="https://x.com"))
    assert outcome.status == "error"
    assert outcome.error_code == code


# --- registry wiring --------------------------------------------------------


def test_default_registry_exposes_web_tools() -> None:
    names = build_default_subagent_registry().names()
    assert "web_search" in names
    assert "web_fetch" in names


def test_builders_wire_projectors() -> None:
    assert build_web_search_tool().result_projector is project_web_search
    assert build_web_fetch_tool().result_projector is project_web_fetch
