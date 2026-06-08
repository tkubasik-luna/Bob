"""Unified tool-advertisement gate — PRD 0015 / issue 0092.

The sub-agent runner advertises tools through ONE server-side lexical gate
(:func:`bob.sub_agent.tool_retrieval.select_tools`) on EVERY provider. There is
no provider branch: Claude CLI runs the SAME gate, with the SAME config knobs
(``TOOL_RETRIEVAL_K`` / ``TOOL_RETRIEVAL_MIN_SCORE``), as LM Studio — so the
retrieval decision (which tools land in the prompt catalogue) is reproducible
across providers and Claude stays a faithful debug reference.

This replaces the issue-0096 "native Anthropic tool deferral" branch, which was
removed: the ``claude`` CLI exposes no live ``defer_loading`` / ``mcp_toolset``
wire through Bob's invocation (it runs ``--tools ""`` and reads tools only from
the prompt), so "deferred" tools were simply dropped from the catalogue and made
uncallable. The advertised set is a SUBSET of the dispatchable set: a tool the
model calls without its schema being advertised still resolves by name.

These tests assert:

- the runner calls ``select_tools`` once, with the config knobs, when building a
  prompt — for any provider client;
- a mail goal advertises ``gmail_search`` and NOT ``web_search`` (the gate
  filters by goal relevance);
- the advertised catalogue depends only on (registry, goal, knobs), so two
  runners built for DIFFERENT provider clients produce the identical catalogue.
"""

from __future__ import annotations

import sqlite3
from typing import Any

import pytest
from pydantic import BaseModel

from bob.config import Settings
from bob.db.migrations_runner import apply_migrations, default_migrations_dir
from bob.event_bus import EventBus
from bob.llm.types import LLMResponse, ToolDefinition
from bob.llm_client import ClaudeCliClient, LLMClient, LMStudioClient
from bob.sub_agent import (
    SubAgentPolicy,
    SubAgentRunner,
    SubAgentToolDefinition,
    SubAgentToolHandlerOutcome,
    SubAgentToolRegistry,
)
from bob.sub_agent.tool_retrieval import select_tools
from bob.task_store import TaskStore

# ---------------------------------------------------------------------------
# Test doubles + helpers
# ---------------------------------------------------------------------------


class _StubClient(LLMClient):
    """Minimal :class:`LLMClient`. The tests stop at ``_build_messages``, so
    ``chat`` / ``complete`` are never reached — the gate no longer reads any
    capability off the client (it is provider-agnostic)."""

    async def chat(
        self,
        messages: list[dict[str, Any]],
        schema: dict[str, Any] | None = None,
        session_id: str | None = None,
    ) -> str:
        raise NotImplementedError("not exercised — tests stop at _build_messages")

    async def complete(
        self,
        messages: list[dict[str, Any]],
        tools: list[ToolDefinition] | None = None,
        session_id: str | None = None,
    ) -> LLMResponse:
        raise NotImplementedError("not exercised — tests stop at _build_messages")


def _make_store() -> TaskStore:
    conn = sqlite3.connect(":memory:")
    apply_migrations(conn, default_migrations_dir())
    return TaskStore(conn)


def _make_running_task(store: TaskStore, *, goal: str = "Trouve le dernier mail de Paul") -> str:
    task_id = store.create_task(title="t", goal=goal)
    store.update_state(task_id, "running")
    return task_id


async def _ok(_ctx: Any, _args: BaseModel) -> SubAgentToolHandlerOutcome:
    return SubAgentToolHandlerOutcome(status="ok", result={})


class _MailArgs(BaseModel):
    label: str


class _WebArgs(BaseModel):
    query: str


def _two_tool_registry() -> SubAgentToolRegistry:
    """A mail tool + a web tool — neither ``always_on``, so the gate filters."""

    return SubAgentToolRegistry(
        [
            SubAgentToolDefinition(
                name="gmail_search",
                version="v1",
                description="Recherche dans la boîte Gmail.",
                args_model=_MailArgs,
                handler=_ok,
                tags=("mail", "email", "gmail", "inbox"),
            ),
            SubAgentToolDefinition(
                name="web_search",
                version="v1",
                description="Cherche le web.",
                args_model=_WebArgs,
                handler=_ok,
                tags=("web", "internet", "actualité", "météo"),
            ),
        ]
    )


def _runner(client: LLMClient, registry: SubAgentToolRegistry) -> SubAgentRunner:
    return SubAgentRunner(
        subagent_client=client,
        task_store=_make_store(),
        event_bus=EventBus(),
        policy=SubAgentPolicy(max_iterations=1),
        tool_registry=registry,
    )


# ---------------------------------------------------------------------------
# The gate runs on every provider, with the config knobs
# ---------------------------------------------------------------------------


def test_runner_runs_select_tools_with_config_knobs(monkeypatch: pytest.MonkeyPatch) -> None:
    """Building a prompt invokes ``select_tools`` once with the config knobs."""

    registry = _two_tool_registry()
    runner = _runner(_StubClient(), registry)
    task_id = _make_running_task(runner._task_store)

    fake_settings = Settings(
        LLM_PROVIDER="lm_studio",
        LLM_BASE_URL="http://x",
        LLM_MODEL="m",
        LLM_API_KEY="k",
        TOOL_RETRIEVAL_K=8,
        TOOL_RETRIEVAL_MIN_SCORE=1,
    )
    calls: list[dict[str, Any]] = []

    def _spy(
        registry: object,
        goal: str,
        *,
        k: int,
        min_score: int,
        ensure_non_empty: bool = False,
    ) -> list[Any]:
        calls.append({"k": k, "min_score": min_score, "ensure_non_empty": ensure_non_empty})
        return select_tools(
            registry, goal, k=k, min_score=min_score, ensure_non_empty=ensure_non_empty
        )

    import bob.sub_agent.runner as runner_mod

    monkeypatch.setattr(runner_mod, "get_settings", lambda: fake_settings)
    monkeypatch.setattr(runner_mod, "select_tools", _spy)

    runner._build_messages(runner._task_store.get_task(task_id), [])

    assert len(calls) == 1
    # RC-A: the runner always opts into the never-empty safety net.
    assert calls[0] == {"k": 8, "min_score": 1, "ensure_non_empty": True}


def test_mail_goal_advertises_only_gmail_search() -> None:
    """A mail goal advertises ``gmail_search`` and gates ``web_search`` out."""

    registry = _two_tool_registry()
    runner = _runner(_StubClient(), registry)
    task_id = _make_running_task(runner._task_store)

    system = runner._build_messages(runner._task_store.get_task(task_id), [])[0]["content"]

    assert "gmail_search" in system
    assert "web_search" not in system


def test_claude_cli_and_lm_studio_advertise_the_identical_catalogue() -> None:
    """Claude CLI gates exactly like LM Studio — same advertised catalogue.

    The advertised set depends only on (registry, goal, knobs), never on the
    resolved provider, so a runner built for a ``claude_cli`` client and one
    built for an LM-Studio client produce a byte-identical tool catalogue. This
    is the property the user relies on: Claude is a faithful debug reference for
    the local configuration.
    """

    registry = _two_tool_registry()
    goal = "Trouve le dernier mail de Paul"

    cli = ClaudeCliClient(Settings(LLM_PROVIDER="claude_cli", CLAUDE_CLI_BIN="claude"))
    lm = LMStudioClient(
        Settings(LLM_PROVIDER="lm_studio", LLM_BASE_URL="http://x", LLM_MODEL="m", LLM_API_KEY="k")
    )

    cli_runner = _runner(cli, registry)
    cli_task = _make_running_task(cli_runner._task_store, goal=goal)
    cli_system = cli_runner._build_messages(cli_runner._task_store.get_task(cli_task), [])[0][
        "content"
    ]

    lm_runner = _runner(lm, registry)
    lm_task = _make_running_task(lm_runner._task_store, goal=goal)
    lm_system = lm_runner._build_messages(lm_runner._task_store.get_task(lm_task), [])[0]["content"]

    # The tool catalogue block is identical across providers (the surrounding
    # prompt is the same shared template), so the advertised set matches.
    assert "gmail_search" in cli_system
    assert "web_search" not in cli_system
    assert ("gmail_search" in lm_system) == ("gmail_search" in cli_system)
    assert ("web_search" in lm_system) == ("web_search" in cli_system)
