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
"""

from __future__ import annotations

import asyncio
import sys
import uuid

from bob.chat_service import ChatService, get_default_chat_service


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
    if not args or args == ["--repl"]:
        asyncio.run(_run_repl())
        return 0
    prompt = args[0]
    asyncio.run(_run_once(prompt))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
