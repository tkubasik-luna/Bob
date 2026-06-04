"""Unit tests for :class:`TavilyClient` — HTTP boundary + error taxonomy.

The HTTP layer is mocked at the transport boundary via ``httpx.MockTransport``
injected through the ``client_factory`` seam — no real network, no httpx
internals patched (mirrors the gmail ``service_factory`` test seam).
"""

from __future__ import annotations

from collections.abc import Callable

import httpx
import pytest

from bob.connectors.tavily import (
    ApiUnreachableError,
    MissingApiKeyError,
    RateLimitedError,
    TavilyClient,
    TavilyError,
    UnauthorizedError,
)

Handler = Callable[[httpx.Request], httpx.Response]


def _client(handler: Handler, *, key: str = "test-key") -> TavilyClient:
    transport = httpx.MockTransport(handler)
    return TavilyClient(
        key,
        base_url="https://api.tavily.com",
        client_factory=lambda: httpx.AsyncClient(
            transport=transport, base_url="https://api.tavily.com"
        ),
    )


async def test_search_success_and_bearer_header() -> None:
    seen: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["auth"] = request.headers.get("authorization", "")
        seen["path"] = request.url.path
        return httpx.Response(
            200,
            json={
                "query": "q",
                "answer": "A",
                "results": [{"title": "T", "url": "https://x.com", "content": "s"}],
            },
        )

    out = await _client(handler).search("q", max_results=3)
    assert seen["auth"] == "Bearer test-key"
    assert seen["path"] == "/search"
    assert out.answer == "A"
    assert out.results[0].url == "https://x.com"


async def test_extract_success() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/extract"
        return httpx.Response(
            200, json={"results": [{"url": "https://x.com", "raw_content": "body"}]}
        )

    page = await _client(handler).extract("https://x.com")
    assert page.content == "body"


@pytest.mark.parametrize(
    ("status", "exc"),
    [
        (401, UnauthorizedError),
        (403, UnauthorizedError),
        (429, RateLimitedError),
        (500, ApiUnreachableError),
        (503, ApiUnreachableError),
        (400, TavilyError),
    ],
)
async def test_status_error_mapping(status: int, exc: type[Exception]) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, json={"error": "x"})

    with pytest.raises(exc):
        await _client(handler).search("q")


async def test_network_error_maps_to_api_unreachable() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("boom", request=request)

    with pytest.raises(ApiUnreachableError):
        await _client(handler).search("q")


async def test_missing_key_raises_before_io() -> None:
    # No transport is ever hit — the guard fires first (None and blank both).
    with pytest.raises(MissingApiKeyError):
        await TavilyClient(None).search("q")
    with pytest.raises(MissingApiKeyError):
        await TavilyClient("   ").extract("https://x.com")


async def test_non_json_response_maps_to_tavily_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="not json")

    with pytest.raises(TavilyError):
        await _client(handler).search("q")


async def test_extract_no_content_raises() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, json={"results": [], "failed_results": [{"url": "https://x.com"}]}
        )

    with pytest.raises(TavilyError):
        await _client(handler).extract("https://x.com")
