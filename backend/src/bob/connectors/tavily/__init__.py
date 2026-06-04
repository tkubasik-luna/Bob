"""Tavily connector package — search the web + extract page content.

Backs the sub-agent ``web_search`` / ``web_fetch`` tools (PRD: web-search):

- :mod:`bob.connectors.tavily.client` — :class:`TavilyClient`, a thin async
  wrapper over the Tavily Search / Extract REST API.
- :mod:`bob.connectors.tavily.models` — :class:`WebSearchResults` /
  :class:`WebSearchResult` / :class:`WebPage` domain objects, pure
  ``from_tavily_*`` factories, and the :func:`to_web_results_props` adapter to
  the ``WebResults`` UI component props.
- :mod:`bob.connectors.tavily.errors` — failure taxonomy the tool handler maps
  to structured ``web_search_*`` / ``web_fetch_*`` error codes.

The package is independent of :mod:`bob.tools` and :mod:`bob.ui_registry`;
wiring happens in the sub-agent tool layer, not here (mirrors the gmail
connector boundary).
"""

from __future__ import annotations

from bob.connectors.tavily.client import ClientFactory, TavilyClient
from bob.connectors.tavily.errors import (
    ApiUnreachableError,
    MissingApiKeyError,
    RateLimitedError,
    TavilyError,
    UnauthorizedError,
)
from bob.connectors.tavily.models import (
    WebPage,
    WebSearchResult,
    WebSearchResults,
    from_tavily_extract,
    from_tavily_search,
    to_web_results_props,
)

__all__ = [
    "ApiUnreachableError",
    "ClientFactory",
    "MissingApiKeyError",
    "RateLimitedError",
    "TavilyClient",
    "TavilyError",
    "UnauthorizedError",
    "WebPage",
    "WebSearchResult",
    "WebSearchResults",
    "from_tavily_extract",
    "from_tavily_search",
    "to_web_results_props",
]
