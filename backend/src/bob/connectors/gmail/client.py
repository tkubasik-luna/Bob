"""Gmail HTTP client — wraps ``googleapiclient`` and returns domain objects.

:class:`GmailClient` is intentionally thin: it owns the discovery-built
``service`` and exposes exactly two operations needed by the sub-agent
``gmail_search`` tool. Internal Gmail JSON shapes never leak past this
module — every public method returns :class:`EmailMessage` instances built
via :func:`bob.connectors.gmail.models.from_gmail_payload`.

Tests cover this module by injecting a fake ``service`` via the
``service_factory`` constructor seam — no patching of googleapiclient
internals required.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from bob.connectors.gmail.models import EmailMessage, from_gmail_payload

ServiceFactory = Callable[[Any], Any]
"""Type alias for the factory used to build the Gmail service object.

In production this is :func:`_default_service_factory`, which calls
``googleapiclient.discovery.build``. Tests replace it with a callable that
returns a fake service exposing the same chained-method API.
"""


def _default_service_factory(credentials: Any) -> Any:
    """Build the real ``googleapiclient`` service.

    Imported lazily so module import does not depend on the heavy
    ``googleapiclient`` install — keeps unit tests that stub the factory
    free of the dependency at import time.
    """

    from googleapiclient.discovery import build

    return build(
        "gmail",
        "v1",
        credentials=credentials,
        cache_discovery=False,
    )


class GmailClient:
    """Read-only Gmail client.

    Construct with refreshed :class:`Credentials` (see
    :func:`bob.connectors.gmail.auth.get_credentials`). The default
    ``service_factory`` builds the real ``googleapiclient`` service; tests
    pass a lambda returning a stubbed service so the HTTP layer is mocked
    cleanly at the boundary (PRD 0007 user story #40).
    """

    def __init__(
        self,
        credentials: Any,
        *,
        user_id: str = "me",
        service_factory: ServiceFactory | None = None,
    ) -> None:
        factory = service_factory or _default_service_factory
        self._service = factory(credentials)
        self._user_id = user_id

    def search_messages(
        self,
        query: str,
        max_results: int = 1,
    ) -> list[EmailMessage]:
        """Search the user's mailbox and return matching messages.

        ``query`` is Gmail search syntax (see
        :func:`bob.connectors.gmail.query_builder.build_query`). Returns a
        list of :class:`EmailMessage` in the order Gmail returns them
        (most-recent-first by default). When no messages match, returns an
        empty list — the caller decides how to surface "no result" to the
        user.
        """

        if max_results < 1:
            return []

        list_response = (
            self._service.users()
            .messages()
            .list(userId=self._user_id, q=query, maxResults=max_results)
            .execute()
        )
        message_refs = list_response.get("messages") or []

        out: list[EmailMessage] = []
        for ref in message_refs[:max_results]:
            msg_id = ref.get("id")
            if not isinstance(msg_id, str):
                continue
            out.append(self.get_message(msg_id))
        return out

    def get_message(self, message_id: str) -> EmailMessage:
        """Fetch a single message by ID and translate to :class:`EmailMessage`.

        Uses ``format=full`` so we receive headers + body + parts in one
        round-trip; this is what :func:`from_gmail_payload` expects.
        """

        payload: dict[str, Any] = (
            self._service.users()
            .messages()
            .get(userId=self._user_id, id=message_id, format="full")
            .execute()
        )
        return from_gmail_payload(payload)


__all__ = ["GmailClient", "ServiceFactory"]
