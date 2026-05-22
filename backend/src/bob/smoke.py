"""Smoke CLI for the chat stack.

Two modes:

* One-shot::

      uv run python -m bob.smoke "hello"

  Sends a single user message and prints the parsed response, then exits.

* Interactive REPL (no positional arg)::

      uv run python -m bob.smoke

  Each line is sent through :meth:`Orchestrator.process_user_message`; the
  pretty-printed response is shown after each turn. Use this to validate
  multi-turn history against a real LM Studio instance.

The CLI bypasses the FastAPI lifespan, so it has to bootstrap the SQLite
connection + :class:`JarvisStore` / :class:`TaskStore` singletons itself
(same steps as :func:`bob.main.lifespan`).
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
import sys
import uuid
from contextlib import contextmanager
from typing import TYPE_CHECKING

from bob import jarvis_store as jarvis_store_module
from bob import task_store as task_store_module
from bob.config import get_settings
from bob.db.migrations_runner import apply_migrations, default_migrations_dir
from bob.jarvis_prompt_loader import load_jarvis_prompt
from bob.jarvis_store import JarvisStore
from bob.orchestrator import Orchestrator, OrchestratorResponse, get_default_orchestrator
from bob.task_store import TaskStore

if TYPE_CHECKING:
    from collections.abc import Iterator


@contextmanager
def _bootstrap_runtime() -> Iterator[None]:
    """Prime the JarvisStore + TaskStore singletons so :class:`Orchestrator` works."""

    settings = get_settings()
    data_dir = settings.BOB_DATA_DIR
    data_dir.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(data_dir / "bob.db"), check_same_thread=False)
    apply_migrations(conn, default_migrations_dir())
    jarvis_store_module.set_default_store(JarvisStore(conn))
    task_store_module.set_default_store(TaskStore(conn))
    load_jarvis_prompt(data_dir)
    try:
        yield
    finally:
        task_store_module.set_default_store(None)
        jarvis_store_module.set_default_store(None)
        conn.close()


def _format_response(response: OrchestratorResponse) -> str:
    return json.dumps(
        {
            "speech": response.speech,
            "ui": [c.model_dump() for c in response.ui],
            "spawned_task_ids": response.spawned_task_ids,
        },
        indent=2,
        ensure_ascii=False,
    )


async def _run_once(prompt: str) -> None:
    orchestrator = get_default_orchestrator()
    session_id = uuid.uuid4().hex
    response = await orchestrator.process_user_message(session_id, prompt)
    print(_format_response(response))


async def _run_repl() -> None:
    orchestrator: Orchestrator = get_default_orchestrator()
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
        response = await orchestrator.process_user_message(session_id, line)
        print(_format_response(response))


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
