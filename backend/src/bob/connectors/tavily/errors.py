"""Error taxonomy for the Tavily connector.

Mirrors the gmail connector's exception hierarchy: a single base
(:class:`TavilyError`) with specific subclasses the sub-agent tool handler
maps to structured ``web_search_*`` / ``web_fetch_*`` error codes. Keeping the
classification here — at the HTTP boundary — means the handler stays a thin
translator rather than re-deriving "what went wrong" from raw status codes.
"""

from __future__ import annotations


class TavilyError(Exception):
    """Base class for every Tavily connector failure.

    A bare ``TavilyError`` (not one of the specific subclasses below) is the
    catch-all the handler maps to ``web_search_failed`` / ``web_fetch_failed``:
    a 4xx rejection, a malformed response, or a page with no extractable
    content.
    """


class MissingApiKeyError(TavilyError):
    """No ``TAVILY_API_KEY`` configured — the call cannot even be attempted.

    Raised before any network I/O so the handler can tell the user to set the
    key rather than reporting a generic search failure.
    """


class UnauthorizedError(TavilyError):
    """Tavily rejected the API key (HTTP 401 / 403)."""


class RateLimitedError(TavilyError):
    """Tavily usage cap / quota exceeded (HTTP 429)."""


class ApiUnreachableError(TavilyError):
    """Network failure, timeout, or Tavily 5xx — transient, worth a retry."""


__all__ = [
    "ApiUnreachableError",
    "MissingApiKeyError",
    "RateLimitedError",
    "TavilyError",
    "UnauthorizedError",
]
