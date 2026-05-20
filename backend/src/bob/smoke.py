"""Smoke CLI for the LLM client.

Usage::

    uv run python -m bob.smoke "hello"

Loads :class:`Settings`, instantiates :class:`LMStudioClient`, sends a single
user message and prints the raw response. Used to validate connectivity to a
local LM Studio instance without involving the WebSocket stack.
"""

from __future__ import annotations

import asyncio
import sys

from bob.config import get_settings
from bob.llm_client import LMStudioClient


async def _run(prompt: str) -> None:
    settings = get_settings()
    client = LMStudioClient(settings)
    response = await client.chat(messages=[{"role": "user", "content": prompt}])
    print(response)


def main() -> int:
    if len(sys.argv) < 2:
        print('Usage: python -m bob.smoke "<prompt>"', file=sys.stderr)
        return 2
    prompt = sys.argv[1]
    asyncio.run(_run(prompt))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
