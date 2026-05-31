#!/usr/bin/env python3
"""sdk_stream_probe.py — does the Agent SDK stream thinking_delta?

Runs a query with include_partial_messages=True and extended thinking enabled,
then classifies every StreamEvent delta. Proves whether reasoning tokens stream
(unlike the bare CLI -p path, which never emits thinking_delta).

Run:  /tmp/sdk_probe_venv/bin/python scripts/sdk_stream_probe.py
"""
import anyio
from claude_agent_sdk import query, ClaudeAgentOptions

PROMPT = "A farmer has 17 sheep, all but 9 die. How many are left? Reason carefully step by step."

C = {"text": "\033[0m", "think": "\033[35m", "tool": "\033[36m", "dim": "\033[2m"}
counts = {}


def bump(k):
    counts[k] = counts.get(k, 0) + 1


async def main():
    options = ClaudeAgentOptions(
        include_partial_messages=True,
        # Extended thinking: ask for a thinking budget via the underlying CLI.
        extra_args={"thinking": "enabled"},
        max_turns=1,
    )

    print(f"{C['dim']}prompt: {PROMPT}\033[0m\n")
    async for msg in query(prompt=PROMPT, options=options):
        cls = type(msg).__name__
        # StreamEvent carries the raw API event under .event
        ev = getattr(msg, "event", None)
        if ev is None:
            bump(cls)
            print(f"\n{C['dim']}[{cls}]\033[0m")
            continue

        etype = ev.get("type")
        if etype == "content_block_start" and ev.get("content_block", {}).get("type") == "tool_use":
            print(f"\n{C['tool']}🔧 tool_use START: {ev['content_block'].get('name')}\033[0m")
        elif etype == "content_block_delta":
            d = ev.get("delta", {})
            dt = d.get("type")
            bump(dt)
            if dt == "text_delta":
                print(d.get("text", ""), end="", flush=True)
            elif dt == "thinking_delta":
                print(f"{C['think']}{d.get('thinking', '')}\033[0m", end="", flush=True)
            elif dt == "input_json_delta":
                print(f"{C['tool']}{d.get('partial_json', '')}\033[0m", end="", flush=True)

    print("\n\n" + "─" * 44)
    print("delta counts:", counts)
    print(f"{C['think']}magenta=thinking_delta\033[0m  "
          f"{C['tool']}cyan=tool/json\033[0m  plain=text")


anyio.run(main)
