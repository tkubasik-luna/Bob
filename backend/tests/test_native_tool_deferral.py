"""Native-Anthropic tool deferral gate — PRD 0015 / issue 0096.

Optional, provider-gated upgrade layered on top of the server-side retrieval
gate (issue 0092). When the resolved provider is native Anthropic
(``claude_cli``) the sub-agent runner delegates tool discovery to the platform's
deferral (``defer_loading`` / ``mcp_toolset``) and SKIPS the server-side
:func:`bob.sub_agent.tool_retrieval.select_tools` retrieval; when the resolved
provider is OpenAI-compatible (LM Studio) the behaviour is BYTE-FOR-BYTE the
issue-0092 path (server-side retrieval, no deferral params).

The choice is keyed off the RESOLVED provider via the client capability query
:meth:`bob.llm_client.LLMClient.supports_native_tool_deferral`, exactly mirroring
how :meth:`supports_guided_json` gates the guided-JSON envelope (issue 0060).

These tests assert:

- the pure :func:`build_tool_deferral_plan` split + request-shaped params;
- the real :class:`bob.llm_client.ClaudeCliClient` / :class:`LMStudioClient`
  capability values (the resolved-provider keying);
- Anthropic → ``select_tools`` is NOT invoked, deferral params are attached;
- LM Studio → ``select_tools`` IS invoked, no deferral params (issue 0092);
- a provider switch at runtime flips the path on the SAME registry.
"""

from __future__ import annotations

import sqlite3
from typing import Any
from unittest.mock import patch

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
from bob.sub_agent.tool_retrieval import (
    ToolDeferralPlan,
    build_tool_deferral_plan,
    select_tools,
)
from bob.task_store import TaskStore

# ---------------------------------------------------------------------------
# Test doubles
# ---------------------------------------------------------------------------


class _DeferralClient(LLMClient):
    """Minimal :class:`LLMClient` with a switchable deferral capability.

    The ONLY behaviour these tests need from a client is what it reports for
    :meth:`supports_native_tool_deferral` — the runner reads it once at
    construction to pick the tool-advertisement path. ``chat`` / ``complete`` are
    never reached because the tests stop at :meth:`SubAgentRunner._build_messages`.
    """

    def __init__(self, *, deferral: bool) -> None:
        self._deferral = deferral

    def supports_native_tool_deferral(self) -> bool:
        return self._deferral

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


def _two_tool_registry(*, mail_always_on: bool = False) -> SubAgentToolRegistry:
    """A mail tool + a web tool. ``mail_always_on`` marks the core kept-loaded."""

    return SubAgentToolRegistry(
        [
            SubAgentToolDefinition(
                name="gmail_search",
                version="v1",
                description="Recherche dans la boîte Gmail.",
                args_model=_MailArgs,
                handler=_ok,
                tags=("mail", "email", "gmail", "inbox"),
                always_on=mail_always_on,
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
    store = _make_store()
    # The store/task are created per-call so each runner has a fresh task to
    # build a prompt from; the helper returns the runner ready to build.
    runner = SubAgentRunner(
        subagent_client=client,
        task_store=store,
        event_bus=EventBus(),
        policy=SubAgentPolicy(max_iterations=1),
        tool_registry=registry,
    )
    return runner


# ---------------------------------------------------------------------------
# Pure deferral-plan builder
# ---------------------------------------------------------------------------


def test_build_tool_deferral_plan_splits_loaded_vs_deferred() -> None:
    """``always_on`` tools are kept-loaded; every other tool is deferred."""

    registry = _two_tool_registry(mail_always_on=True)
    plan = build_tool_deferral_plan(registry)

    assert isinstance(plan, ToolDeferralPlan)
    assert plan.loaded == ("gmail_search",)
    assert plan.deferred == ("web_search",)
    # Request-shaped params carry the deferred set under ``defer_loading``.
    assert plan.params == {"defer_loading": ["web_search"]}


def test_build_tool_deferral_plan_is_deterministic_and_sorted() -> None:
    """Names are sorted so the plan is byte-stable regardless of registration order."""

    async def _h(_ctx: Any, _a: BaseModel) -> SubAgentToolHandlerOutcome:
        return SubAgentToolHandlerOutcome(status="ok", result={})

    def _def(name: str, *, always_on: bool) -> SubAgentToolDefinition:
        return SubAgentToolDefinition(
            name=name,
            version="v1",
            description=name,
            args_model=_WebArgs,
            handler=_h,
            always_on=always_on,
        )

    # Registered out of alphabetical order on purpose.
    registry = SubAgentToolRegistry(
        [
            _def("zeta", always_on=False),
            _def("core_b", always_on=True),
            _def("alpha", always_on=False),
            _def("core_a", always_on=True),
        ]
    )
    plan = build_tool_deferral_plan(registry)
    assert plan.loaded == ("core_a", "core_b")
    assert plan.deferred == ("alpha", "zeta")
    assert plan.params == {"defer_loading": ["alpha", "zeta"]}


def test_build_tool_deferral_plan_omits_defer_loading_when_nothing_deferred() -> None:
    """An all-always-on registry attaches NO ``defer_loading`` key (minimal request)."""

    registry = SubAgentToolRegistry(
        [
            SubAgentToolDefinition(
                name="gmail_search",
                version="v1",
                description="x",
                args_model=_MailArgs,
                handler=_ok,
                always_on=True,
            ),
        ]
    )
    plan = build_tool_deferral_plan(registry)
    assert plan.deferred == ()
    assert plan.params == {}


# ---------------------------------------------------------------------------
# Resolved-provider keying — the real clients
# ---------------------------------------------------------------------------


def test_claude_cli_client_supports_native_tool_deferral() -> None:
    """``claude_cli`` resolves to native Anthropic → deferral capable."""

    client = ClaudeCliClient(Settings(LLM_PROVIDER="claude_cli", CLAUDE_CLI_BIN="claude"))
    assert client.supports_native_tool_deferral() is True


def test_lm_studio_client_does_not_support_native_tool_deferral() -> None:
    """LM Studio (OpenAI-compatible) → NO native deferral; stays on retrieval."""

    client = LMStudioClient(
        Settings(
            LLM_PROVIDER="lm_studio",
            LLM_BASE_URL="http://x",
            LLM_MODEL="m",
            LLM_API_KEY="k",
        )
    )
    assert client.supports_native_tool_deferral() is False


def test_deferral_capability_is_orthogonal_to_guided_json() -> None:
    """The two capabilities do not collapse: CLI defers but is NOT guided-JSON."""

    cli = ClaudeCliClient(Settings(LLM_PROVIDER="claude_cli", CLAUDE_CLI_BIN="claude"))
    assert cli.supports_native_tool_deferral() is True
    assert cli.supports_guided_json() is False


# ---------------------------------------------------------------------------
# The gate — Anthropic skips select_tools; LM Studio runs it (issue 0092)
# ---------------------------------------------------------------------------


def test_anthropic_path_skips_select_tools_and_attaches_deferral() -> None:
    """Provider = native Anthropic → ``select_tools`` is NOT invoked; plan built."""

    registry = _two_tool_registry(mail_always_on=True)
    runner = _runner(_DeferralClient(deferral=True), registry)
    task_id = _make_running_task(runner._task_store)

    with patch("bob.sub_agent.runner.select_tools") as mock_select:
        system = runner._build_messages(runner._task_store.get_task(task_id), [])[0]["content"]

    # Server-side retrieval is NOT run on the native path.
    mock_select.assert_not_called()

    # The platform deferral plan was built + stashed for the request seam.
    plan = runner.last_deferral_plan
    assert plan is not None
    assert plan.loaded == ("gmail_search",)
    assert plan.deferred == ("web_search",)
    assert plan.params == {"defer_loading": ["web_search"]}

    # The kept-loaded core is advertised in the prompt; the deferred tool's
    # schema is NOT pre-loaded into the catalogue (the platform loads it lazily).
    assert "gmail_search" in system
    assert "web_search" not in system


def test_lm_studio_path_runs_select_tools_and_attaches_no_deferral() -> None:
    """Provider = LM Studio → ``select_tools`` IS invoked; NO deferral plan (0092)."""

    registry = _two_tool_registry()
    runner = _runner(_DeferralClient(deferral=False), registry)
    task_id = _make_running_task(runner._task_store)

    with patch("bob.sub_agent.runner.select_tools", wraps=select_tools) as mock_select:
        system = runner._build_messages(runner._task_store.get_task(task_id), [])[0]["content"]

    # Server-side retrieval runs exactly as in issue 0092.
    mock_select.assert_called_once()
    # No deferral plan is ever built on the OpenAI-compatible path.
    assert runner.last_deferral_plan is None

    # Byte-for-byte issue-0092 outcome: a mail goal advertises only gmail_search.
    assert "gmail_search" in system
    assert "web_search" not in system


def test_lm_studio_path_uses_retrieval_knobs() -> None:
    """LM Studio path passes the config retrieval knobs to ``select_tools`` (0092)."""

    registry = _two_tool_registry()
    runner = _runner(_DeferralClient(deferral=False), registry)
    task_id = _make_running_task(runner._task_store)

    fake_settings = Settings(
        LLM_PROVIDER="lm_studio",
        LLM_BASE_URL="http://x",
        LLM_MODEL="m",
        LLM_API_KEY="k",
        TOOL_RETRIEVAL_K=8,
        TOOL_RETRIEVAL_MIN_SCORE=1,
    )
    with (
        patch("bob.sub_agent.runner.get_settings", return_value=fake_settings),
        patch("bob.sub_agent.runner.select_tools", return_value=[]) as mock_select,
    ):
        runner._build_messages(runner._task_store.get_task(task_id), [])

    mock_select.assert_called_once()
    _args, kwargs = mock_select.call_args
    assert kwargs["k"] == 8
    assert kwargs["min_score"] == 1


# ---------------------------------------------------------------------------
# Runtime provider switch flips the path
# ---------------------------------------------------------------------------


def test_provider_switch_flips_the_path() -> None:
    """The picker swaps the client per task; the new runner picks the new path.

    The live switch (issues 0078-0081) rebuilds the sub-agent client and the
    runner reads its capability at construction — so a runner built for an
    LM-Studio client runs retrieval, and a runner built for a CLI client defers,
    over the SAME registry.
    """

    registry = _two_tool_registry(mail_always_on=True)

    # First: LM Studio is active → server-side retrieval, no plan.
    lm_runner = _runner(_DeferralClient(deferral=False), registry)
    lm_task = _make_running_task(lm_runner._task_store)
    with patch("bob.sub_agent.runner.select_tools", return_value=[]) as mock_select_lm:
        lm_runner._build_messages(lm_runner._task_store.get_task(lm_task), [])
    mock_select_lm.assert_called_once()
    assert lm_runner.last_deferral_plan is None

    # Switch to Claude CLI → the rebuilt runner defers, retrieval not invoked.
    cli_runner = _runner(_DeferralClient(deferral=True), registry)
    cli_task = _make_running_task(cli_runner._task_store)
    with patch("bob.sub_agent.runner.select_tools") as mock_select_cli:
        cli_runner._build_messages(cli_runner._task_store.get_task(cli_task), [])
    mock_select_cli.assert_not_called()
    assert cli_runner.last_deferral_plan is not None
    assert cli_runner.last_deferral_plan.params == {"defer_loading": ["web_search"]}


@pytest.mark.asyncio
async def test_full_lm_studio_config_unchanged_end_to_end() -> None:
    """Full-LM-Studio config: deferral layer is invisible (zero behavioural change).

    A non-deferral client builds a prompt identical to one built when the
    deferral feature did not exist — same advertised catalogue, no plan, and the
    same call to the server-side retrieval gate. This is the hard requirement of
    issue 0096: the local path is byte-for-byte the issue-0092 behaviour.
    """

    registry = _two_tool_registry()
    runner = _runner(_DeferralClient(deferral=False), registry)
    task_id = _make_running_task(runner._task_store)

    messages = runner._build_messages(runner._task_store.get_task(task_id), [])
    system = messages[0]["content"]

    assert runner.last_deferral_plan is None
    assert "defer_loading" not in system  # no deferral params leak into the prompt
    assert "gmail_search" in system
    assert "web_search" not in system
