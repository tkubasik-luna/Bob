"""Tavily HTTP client — wraps the Tavily Search / Extract REST API.

:class:`TavilyClient` is intentionally thin (mirrors
:class:`bob.connectors.gmail.client.GmailClient`): it owns an
``httpx.AsyncClient`` and exposes exactly the two operations the sub-agent web
tools need — :meth:`search` (POST ``/search``) and :meth:`extract`
(POST ``/extract``). Raw Tavily JSON never leaks past this module; every method
returns the domain objects from :mod:`bob.connectors.tavily.models`.

HTTP failures are folded into the :mod:`bob.connectors.tavily.errors` taxonomy
at this boundary so the tool handler stays a thin translator to ``web_search_*``
error codes. Tests inject a fake transport via the ``client_factory`` seam — no
real network, no httpx internals patched (the gmail ``service_factory`` pattern).
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import httpx
import structlog

from bob.connectors.tavily.errors import (
    ApiUnreachableError,
    MissingApiKeyError,
    RateLimitedError,
    TavilyError,
    UnauthorizedError,
)
from bob.connectors.tavily.models import (
    WebPage,
    WebSearchResults,
    from_tavily_extract,
    from_tavily_search,
)

_logger = structlog.get_logger(__name__)

ClientFactory = Callable[[], httpx.AsyncClient]
"""Factory building the ``httpx.AsyncClient`` used for Tavily calls.

Production uses :meth:`TavilyClient._default_client_factory` (configures
``base_url`` + ``timeout``). Tests pass a factory returning a client wired to
an ``httpx.MockTransport`` so the HTTP layer is mocked at the boundary.
"""


class TavilyClient:
    """Async client for the Tavily Search / Extract API.

    Construct with the ``TAVILY_API_KEY`` (may be ``None`` / empty — the call
    then raises :class:`MissingApiKeyError` before any I/O). Authentication is
    a ``Bearer`` header, per Tavily's current REST contract.
    """

    def __init__(
        self,
        api_key: str | None,
        *,
        base_url: str = "https://api.tavily.com",
        timeout: float = 15.0,
        client_factory: ClientFactory | None = None,
    ) -> None:
        self._api_key = (api_key or "").strip()
        self._base_url = base_url.rstrip("/")
        self._timeout = timeout
        self._client_factory = client_factory or self._default_client_factory

    def _default_client_factory(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(base_url=self._base_url, timeout=self._timeout)

    async def search(
        self,
        query: str,
        *,
        max_results: int = 5,
        search_depth: str = "basic",
        include_answer: bool = True,
    ) -> WebSearchResults:
        """Run a web search and return ranked results (+ optional direct answer)."""

        data = await self._post(
            "/search",
            {
                "query": query,
                "max_results": max_results,
                "search_depth": search_depth,
                "include_answer": include_answer,
            },
        )
        return from_tavily_search(data, query=query)

    async def extract(self, url: str) -> WebPage:
        """Extract the readable text content of a single URL.

        Raises :class:`TavilyError` when Tavily returns no extractable content
        (a paywalled / unreachable / bot-blocked page) so the handler reports a
        clean ``web_fetch_failed`` instead of a blank page.
        """

        data = await self._post("/extract", {"urls": [url]})
        page = from_tavily_extract(data, url=url)
        if not page.content.strip():
            raise TavilyError(f"Tavily returned no extractable content for {url}")
        return page

    async def _post(self, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        """POST ``payload`` to ``path`` and return the parsed JSON object.

        Every failure mode is mapped to the connector's error taxonomy here so
        callers never see a raw ``httpx`` exception or status code.
        """

        if not self._api_key:
            raise MissingApiKeyError("TAVILY_API_KEY is not configured")

        headers = {"Authorization": f"Bearer {self._api_key}"}
        try:
            async with self._client_factory() as client:
                response = await client.post(path, json=payload, headers=headers)
        except httpx.RequestError as exc:
            # Timeout, DNS failure, connection reset, … — all transient.
            _logger.warning("tavily.request_error", path=path, error=str(exc))
            raise ApiUnreachableError(f"Tavily request failed: {exc}") from exc

        status = response.status_code
        if status in (401, 403):
            raise UnauthorizedError(f"Tavily rejected the API key (HTTP {status})")
        if status == 429:
            raise RateLimitedError("Tavily rate limit / quota exceeded (HTTP 429)")
        if status >= 500:
            raise ApiUnreachableError(f"Tavily service error (HTTP {status})")
        if status >= 400:
            raise TavilyError(f"Tavily request rejected (HTTP {status})")

        try:
            data: Any = response.json()
        except ValueError as exc:
            raise TavilyError(f"Tavily returned a non-JSON response: {exc}") from exc
        if not isinstance(data, dict):
            raise TavilyError("Tavily returned an unexpected (non-object) response")
        return data


__all__ = ["ClientFactory", "TavilyClient"]
