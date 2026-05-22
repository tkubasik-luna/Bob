"""Smoke CLI for the chat stack.

Two modes:

* One-shot::

      uv run python -m bob.smoke "hello"

  Sends a single user message and prints the parsed response, then exits.

* Interactive REPL (no positional arg)::

      uv run python -m bob.smoke

  Each line is sent through :class:`ChatService.handle_user_message`; the
  pretty-printed :class:`ParsedResponse` is shown after each turn. Use this
  to validate multi-turn history against a real LM Studio instance.

The CLI bypasses the FastAPI lifespan, so it has to bootstrap the SQLite
connection + :class:`JarvisStore` singleton itself (same steps as
:func:`bob.main.lifespan`).
"""

from __future__ import annotations

import asyncio
import sqlite3
import sys
import uuid
from contextlib import contextmanager
from typing import TYPE_CHECKING

from bob import jarvis_store as jarvis_store_module
from bob.chat_service import ChatService, get_default_chat_service
from bob.config import get_settings
from bob.db.migrations_runner import apply_migrations, default_migrations_dir
from bob.jarvis_prompt_loader import load_jarvis_prompt
from bob.jarvis_store import JarvisStore

if TYPE_CHECKING:
    from collections.abc import Iterator


@contextmanager
def _bootstrap_runtime() -> Iterator[None]:
    """Prime the JarvisStore singleton so :class:`ChatService` can be built."""

    settings = get_settings()
    data_dir = settings.BOB_DATA_DIR
    data_dir.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(data_dir / "bob.db"), check_same_thread=False)
    apply_migrations(conn, default_migrations_dir())
    jarvis_store_module.set_default_store(JarvisStore(conn))
    load_jarvis_prompt(data_dir)
    try:
        yield
    finally:
        jarvis_store_module.set_default_store(None)
        conn.close()


async def _run_once(prompt: str) -> None:
    service = get_default_chat_service()
    session_id = uuid.uuid4().hex
    parsed = await service.handle_user_message(session_id, prompt)
    print(parsed.model_dump_json(indent=2))


async def _run_repl() -> None:
    service: ChatService = get_default_chat_service()
    session_id = uuid.uuid4().hex
    print(f"Bob REPL — session {session_id}. Ctrl-D / Ctrl-C to quit.")
    loop = asyncio.get_running_loop()
    while True:
        try:
            line = await loop.run_in_executor(None, sys.stdin.readline)
        except KeyboardInterrupt:
            print()
            return
        if not line:
            print()
            return
        line = line.rstrip("\n")
        if not line.strip():
            continue
        parsed = await service.handle_user_message(session_id, line)
        print(parsed.model_dump_json(indent=2))


def main() -> int:
    args = sys.argv[1:]
    with _bootstrap_runtime():
        if not args or args == ["--repl"]:
            asyncio.run(_run_repl())
            return 0
        prompt = args[0]
        asyncio.run(_run_once(prompt))
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
