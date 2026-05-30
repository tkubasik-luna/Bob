"""Live smoke for PRD 0009 (tool result store + convergence) against a real LLM.

NOT a pytest test — it needs the network and a running LM Studio / OpenAI-
compatible server, so it stays out of CI. Run it by hand:

    uv run python scripts/live_smoke_result_store.py
    LIVE_SMOKE_MODELS="google/gemma-4-e4b,qwen/qwen3.6-35b-a3b" \
        uv run python scripts/live_smoke_result_store.py

It drives a REAL :class:`SubAgentRunner` against the configured model with a
STUB ``gmail_search`` tool that returns a canned result (the TestFlight mail
from the 2026-05-30 investigation) — so no Google credentials are needed. It
then checks the exact failure the user reported:

    request → task → tool call → DATA DISPLAYED

i.e. that a weak local model, given the slimmed Gmail skill pack, emits a valid
``gmail_search`` tool call, and that the runner then CONVERGES to a terminal
``done`` whose ``result_payload`` is the Mail card built deterministically from
the stored result — the overlay is populated even though the model never
hand-built (or, on a stall, never emitted) the descriptor.

For each configured model it prints: the raw action(s) the model emitted, the
LLM-call count (1 = converged on the first tool call), the compact transcript
digest (asserting the body never leaked), and the final task state +
``result_payload``. A PASS means a populated Mail card; the per-model table at
the end is the evidence for "ça marche sur les LLM locaux".
"""

from __future__ import annotations

import asyncio
import json
import os
import sqlite3
import time
from typing import Any

from bob.config import Settings
from bob.db.migrations_runner import apply_migrations, default_migrations_dir
from bob.event_bus import EventBus
from bob.llm_client import LMStudioClient
from bob.sub_agent import (
    GmailSearchArgs,
    SubAgentPolicy,
    SubAgentRunner,
    SubAgentToolDefinition,
    SubAgentToolHandlerOutcome,
    SubAgentToolRegistry,
)
from bob.sub_agent.tool_registry import project_gmail_search
from bob.task_store import TaskStore

BASE_URL = os.environ.get("LIVE_SMOKE_BASE_URL", "http://192.168.86.21:1234/v1")
GOAL = os.environ.get(
    "LIVE_SMOKE_GOAL",
    "Rechercher et afficher le dernier email reçu dans la boîte de réception",
)
DEFAULT_MODELS = (
    "qwen/qwen3.6-35b-a3b",
    "mistralai/devstral-small-2-2512",
    "google/gemma-4-e4b",
)
MODELS = tuple(
    m.strip()
    for m in os.environ.get("LIVE_SMOKE_MODELS", ",".join(DEFAULT_MODELS)).split(",")
    if m.strip()
)

# The exact mail from the 2026-05-30 investigation (TestFlight / KiLi), shaped
# like ``to_mail_props`` output so the projector builds a schema-valid card.
CANNED_RESULT: dict[str, Any] = {
    "query": "label:INBOX",
    "count": 1,
    "messages": [
        {
            "from": {
                "name": "l'école des loisirs via TestFlight",
                "email": "testflight_no_reply@email.apple.com",
            },
            "receivedAt": "2026-05-29T15:04:47Z",
            "subject": "KiLi DEV 2.9.0 (205) for iOS is now available to test.",
            "bodyPreview": "KiLi DEV 2.9.0 (205) is ready to test on iOS. …",
            "flags": [],
            "attachments": [],
            "threadId": "19e744436f754b1f",
            "messageId": "19e744436f754b1f",
            "gmailWebUrl": "https://mail.google.com/mail/u/0/#inbox/19e744436f754b1f",
        }
    ],
}


class _CountingClient:
    """Wraps a real client, tallying ``chat`` calls + capturing raw replies."""

    def __init__(self, inner: LMStudioClient) -> None:
        self._inner = inner
        self.calls: list[dict[str, Any]] = []

    def supports_guided_json(self) -> bool:
        return self._inner.supports_guided_json()

    async def chat(
        self,
        messages: list[dict[str, Any]],
        schema: dict[str, Any] | None = None,
        session_id: str | None = None,
    ) -> str:
        raw = await self._inner.chat(messages, schema=schema, session_id=session_id)
        self.calls.append({"messages": messages, "raw": raw})
        return raw


def _build_stub_registry(handler_calls: list[Any]) -> SubAgentToolRegistry:
    async def _handler(_ctx: Any, args: Any) -> SubAgentToolHandlerOutcome:
        handler_calls.append(args)
        return SubAgentToolHandlerOutcome(status="ok", result=CANNED_RESULT)

    return SubAgentToolRegistry(
        [
            SubAgentToolDefinition(
                name="gmail_search",
                version="v1",
                description=(
                    "Recherche dans la boîte Gmail de l'utilisateur en combinant des "
                    "filtres structurés (expéditeur, sujet, dates, etc.) et renvoie "
                    "la liste des messages correspondants. ``max_results`` ≤ 5."
                ),
                args_model=GmailSearchArgs,
                handler=_handler,
                result_projector=project_gmail_search,
            )
        ]
    )


async def _run_one(model: str) -> dict[str, Any]:
    settings = Settings(
        LLM_BASE_URL=BASE_URL,
        LLM_MODEL=model,
        LLM_API_KEY="lm-studio",
        LLM_TOOL_MODE="auto",
    )
    client = _CountingClient(LMStudioClient(settings))
    handler_calls: list[Any] = []
    registry = _build_stub_registry(handler_calls)

    conn = sqlite3.connect(":memory:")
    apply_migrations(conn, default_migrations_dir())
    store = TaskStore(conn)
    task_id = store.create_task(title="dernier mail", goal=GOAL)
    store.update_state(task_id, "running")

    runner = SubAgentRunner(
        # ``_CountingClient`` is a structural stand-in (duck-typed to the two
        # methods the runner uses); it is not an ``LLMClient`` subclass.
        subagent_client=client,  # type: ignore[arg-type]
        task_store=store,
        event_bus=EventBus(),
        policy=SubAgentPolicy(max_iterations=8, wall_clock_seconds=120.0, token_cap=200_000),
        tool_registry=registry,
    )

    started = time.perf_counter()
    error: str | None = None
    try:
        await runner.run(task_id)
    except Exception as exc:
        error = f"{type(exc).__name__}: {exc}"
    elapsed = time.perf_counter() - started

    task = store.get_task(task_id)
    tool_msgs = [m for m in store.get_task_messages(task_id) if m.role == "tool"]
    tool_body = json.loads(tool_msgs[-1].content) if tool_msgs else None
    payload = task.result_payload
    card_ok = isinstance(payload, dict) and payload.get("component") == "Mail"
    digest_clean = bool(tool_body) and "bodyPreview" not in json.dumps(
        tool_body, ensure_ascii=False
    )

    return {
        "model": model,
        "error": error,
        "llm_calls": len(client.calls),
        "handler_calls": len(handler_calls),
        "raw_actions": [c["raw"][:240] for c in client.calls],
        "tool_transcript": tool_body,
        "state": task.state,
        "result": (task.result or "")[:160],
        "result_payload": payload,
        "card_ok": card_ok,
        "digest_clean": digest_clean,
        "elapsed_s": round(elapsed, 1),
        # PASS = the data is displayed: task done + a populated Mail card +
        # the body never leaked into the transcript digest.
        "pass": task.state == "done" and card_ok and digest_clean,
    }


async def main() -> None:
    print("\nLive smoke — PRD 0009 result store + convergence")
    print(f"server : {BASE_URL}")
    print(f"goal   : {GOAL}")
    print(f"models : {', '.join(MODELS)}\n")

    results: list[dict[str, Any]] = []
    for model in MODELS:
        print(f"━━━ {model} ━━━")
        res = await _run_one(model)
        results.append(res)
        if res["error"]:
            print(f"  ERROR: {res['error']}")
        for i, raw in enumerate(res["raw_actions"], 1):
            print(f"  action {i}: {raw}")
        print(
            f"  llm_calls={res['llm_calls']}  handler_calls={res['handler_calls']}  "
            f"elapsed={res['elapsed_s']}s"
        )
        print(f"  tool transcript: {json.dumps(res['tool_transcript'], ensure_ascii=False)}")
        print(
            f"  state={res['state']}  card_ok={res['card_ok']}  digest_clean={res['digest_clean']}"
        )
        print(f"  result_payload={json.dumps(res['result_payload'], ensure_ascii=False)[:200]}")
        print(f"  => {'PASS ✅' if res['pass'] else 'FAIL ❌'}\n")

    print("━━━ summary ━━━")
    for res in results:
        flag = "PASS ✅" if res["pass"] else "FAIL ❌"
        print(
            f"  {flag}  {res['model']:38s} calls={res['llm_calls']} "
            f"converged={'yes' if res['llm_calls'] <= 2 else 'no'} "
            f"card={'yes' if res['card_ok'] else 'NO'} {res['elapsed_s']}s"
        )
    n_pass = sum(1 for r in results if r["pass"])
    print(f"\n{n_pass}/{len(results)} models PASS\n")


if __name__ == "__main__":
    asyncio.run(main())
