"""Retrieval read-path stub over sealed epochs.

PRD 0006 / issue 0051. Real RAG implementation is explicitly out of
scope; this slice ships the *call site* so the read path is observable
from day one. Without an active read path the sealed-epoch logic
silently rots — the PRD calls this out as the central motivation for
shipping the stub now.

:meth:`RetrievalAPI.recall` therefore:

* returns ``[]`` unconditionally (no embeddings, no FTS, no SQL filter
  beyond the implicit "nothing").
* emits a structured ``retrieval.recall_called`` log line with the
  query metadata so the call site is grep-able and asserted-on in
  tests.

Future v2 implementation will swap the body for a real retrieval
(BM25 over sealed turns, dense embeddings, hybrid…) without changing
this signature or the log event — the contract is the read path.
"""

from __future__ import annotations

from collections.abc import Sequence

import structlog

from bob.context.entry import ContextEntry

#: Stable structured-log event name for the recall call site. Asserted
#: by ``test_retrieval_api.py`` and by the long-session integration
#: test.
RECALL_EVENT = "retrieval.recall_called"

_logger = structlog.get_logger(__name__)


class RetrievalAPI:
    """Read-path stub over sealed epochs.

    The signature is the contract for the v2 retrieval implementation;
    do not narrow or widen it without coordinated changes to call
    sites.
    """

    def recall(self, query: str, *, limit: int = 5) -> Sequence[ContextEntry]:
        """Return entries relevant to ``query`` from sealed epochs.

        v1 stub — always returns an empty sequence. Every call logs a
        structured event so call sites are observable. The ``limit``
        argument is recorded but otherwise unused at this slice.

        Callers MUST handle the empty result without crashing. The
        contract test ``test_retrieval_api.py`` asserts the smoke
        behavior.
        """

        query_text = query if isinstance(query, str) else ""
        _logger.info(
            RECALL_EVENT,
            query=query_text,
            query_length=len(query_text),
            limit=limit,
            result_count=0,
        )
        return []
