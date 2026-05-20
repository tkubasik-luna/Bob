"""In-memory conversation history store.

Maintains a per-``session_id`` ordered list of chat messages. The store is
intentionally tiny and process-local — it is a V0 stand-in for whatever
persistence layer we may add later. Operations on unknown sessions never
raise: ``get_history`` returns ``[]`` and ``clear`` is a no-op.
"""

from __future__ import annotations

from typing import Literal, TypedDict

Role = Literal["system", "user", "assistant"]


class Message(TypedDict):
    """A single chat message exchanged with the LLM."""

    role: Role
    content: str


class ConversationStore:
    """Thread-unsafe in-memory map of ``session_id -> list[Message]``."""

    def __init__(self) -> None:
        self._store: dict[str, list[Message]] = {}

    def append(self, session_id: str, role: Role, content: str) -> None:
        """Append a message to ``session_id``'s history, creating it if needed."""

        self._store.setdefault(session_id, []).append({"role": role, "content": content})

    def get_history(self, session_id: str) -> list[Message]:
        """Return a *copy* of ``session_id``'s history, or ``[]`` if unknown."""

        return list(self._store.get(session_id, []))

    def clear(self, session_id: str) -> None:
        """Drop ``session_id``'s history. No-op if it does not exist."""

        self._store.pop(session_id, None)


_DEFAULT_STORE = ConversationStore()


def append(session_id: str, role: Role, content: str) -> None:
    """Module-level convenience wrapper around :class:`ConversationStore`."""

    _DEFAULT_STORE.append(session_id, role, content)


def get_history(session_id: str) -> list[Message]:
    """Module-level convenience wrapper around :class:`ConversationStore`."""

    return _DEFAULT_STORE.get_history(session_id)


def clear(session_id: str) -> None:
    """Module-level convenience wrapper around :class:`ConversationStore`."""

    _DEFAULT_STORE.clear(session_id)


def get_default_store() -> ConversationStore:
    """Return the process-wide default :class:`ConversationStore`."""

    return _DEFAULT_STORE
