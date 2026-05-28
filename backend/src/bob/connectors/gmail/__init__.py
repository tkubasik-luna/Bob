"""Gmail connector package — authenticate, query, translate.

Encapsulates everything Bob needs to read mail from Gmail:

- :mod:`bob.connectors.gmail.auth` — OAuth2 installed-app flow (runtime
  refresh + one-shot CLI bootstrap).
- :mod:`bob.connectors.gmail.client` — :class:`GmailClient` (thin wrapper
  over ``googleapiclient`` returning domain objects).
- :mod:`bob.connectors.gmail.models` — :class:`EmailMessage` +
  :class:`Attachment` dataclasses, pure-function payload factory, and the
  :func:`to_mail_props` adapter to the ``Mail`` UI component props.
- :mod:`bob.connectors.gmail.query_builder` — pure :func:`build_query`
  function that maps structured args to Gmail search syntax.

The package is independent of :mod:`bob.tools` and :mod:`bob.ui_registry`;
wiring happens in the sub-agent tool layer (issue 0055), not here.
"""

from __future__ import annotations

from bob.connectors.gmail import auth
from bob.connectors.gmail.auth import (
    BootstrapRequiredError,
    GmailAuthError,
    MissingCredentialsError,
    RefreshFailedError,
)
from bob.connectors.gmail.client import GmailClient
from bob.connectors.gmail.models import (
    Attachment,
    EmailMessage,
    from_gmail_payload,
    to_mail_props,
)
from bob.connectors.gmail.query_builder import QueryBuilderError, build_query

__all__ = [
    "Attachment",
    "BootstrapRequiredError",
    "EmailMessage",
    "GmailAuthError",
    "GmailClient",
    "MissingCredentialsError",
    "QueryBuilderError",
    "RefreshFailedError",
    "auth",
    "build_query",
    "from_gmail_payload",
    "to_mail_props",
]
