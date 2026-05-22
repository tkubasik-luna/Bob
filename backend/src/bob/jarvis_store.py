"""SQLite-backed store for the singleton Jarvis conversation thread.

Replaces the legacy in-memory per-session conversation store. Bob is a
desktop solo single-user app — there is exactly one Jarvis thread, persisted
to ``{BOB_DATA_DIR}/bob.db``. No per-session map; ``session_id`` from the WS
layer is purely informational from this slice onward.

The store talks straight to a :class:`sqlite3.Connection`; the boot path in
:mod:`bob.main` opens that connection (with ``check_same_thread=False`` so
FastAPI threads can share it) and runs migrations before priming the
singleton via :func:`set_default_store`.
"""

from __future__ import annotations

import sqlite3
import threading
from typing import Literal, TypedDict

Role = Literal["system", "user", "assistant", "tool"]
Action = Literal["done", "ask_user", "progress"]


class Message(TypedDict, total=False):
    """A single message in the Jarvis thread.

    ``role`` and ``content`` are always present; ``action`` is optional and
    only set for sub-agent tool/result entries (later slices). Keeping the
    optional key on the TypedDict means downstream layers can read it without
    a guard while still being valid when absent.
    """

    role: Role
    content: str
    action: Action | None


class JarvisStore:
    """Persistent, single-thread message store backed by SQLite.

    Thread-safety: the underlying connection is opened with
    ``check_same_thread=False``; a module-level lock serialises writes so
    multiple FastAPI request workers cannot interleave statements.
    """

    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn
        self._lock = threading.Lock()

    def append(self, role: Role, content: str, action: Action | None = None) -> None:
        """Append a message to the Jarvis thread."""

        with self._lock, self._conn:
            self._conn.execute(
                "INSERT INTO jarvis_messages(role, content, action) VALUES (?, ?, ?)",
                (role, content, action),
            )

    def history(self) -> list[Message]:
        """Return every persisted message in insertion order.

        We expose ``role`` + ``content`` (+ ``action`` when set) so callers can
        feed the list directly to :meth:`LLMClient.chat`. ``id``,
        ``created_at`` are not surfaced — they're persistence-internal.
        """

        with self._lock:
            cursor = self._conn.execute(
                "SELECT role, content, action FROM jarvis_messages ORDER BY id ASC"
            )
            rows = cursor.fetchall()

        messages: list[Message] = []
        for role, content, action in rows:
            msg: Message = {"role": role, "content": content}
            if action is not None:
                msg["action"] = action
            messages.append(msg)
        return messages

    def clear(self) -> None:
        """Drop every persisted message. Resets the AUTOINCREMENT counter."""

        with self._lock, self._conn:
            self._conn.execute("DELETE FROM jarvis_messages")
            # sqlite_sequence may not exist if the table has never been
            # written to — guard via OR IGNORE pattern.
            self._conn.execute("DELETE FROM sqlite_sequence WHERE name = 'jarvis_messages'")


# --- Singleton plumbing -------------------------------------------------------
#
# The boot path in :mod:`bob.main` opens the SQLite connection, runs migrations
# and then calls :func:`set_default_store` with the wired store. Test code and
# the smoke CLI can do the same against a tmp DB.

_DEFAULT_STORE: JarvisStore | None = None


def set_default_store(store: JarvisStore | None) -> None:
    """Install (or clear) the process-wide singleton :class:`JarvisStore`."""

    global _DEFAULT_STORE
    _DEFAULT_STORE = store


def get_default_store() -> JarvisStore:
    """Return the process-wide singleton, raising if it hasn't been primed."""

    if _DEFAULT_STORE is None:
        raise RuntimeError(
            "JarvisStore default singleton not initialised. Did the app lifespan (bob.main) run?"
        )
    return _DEFAULT_STORE
