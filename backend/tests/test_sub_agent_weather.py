"""End-to-end weather acceptance case (PRD 0015 / issue 0095).

Exercises a real single-shot MCP tool through every layer with the weather
*session* mocked at the connector boundary (the ``session_factory`` seam from
issue 0093/0094 — a fake async-context-manager yielding a fake
:class:`MCPSession` with canned ``list_tools`` / ``call_tool``). No real
subprocess, no httpx, no ``mcp`` SDK transport stack is exercised.

The manifest mirrors the shipped example (``.env.example`` / ``config.py``): a
``weather`` server exposing a single ``get_forecast`` tool, narrowed to
``place`` + ``date``, with a French ``description_fr``, weather ``tags`` and
``terminal: true``. Curation is folded by :class:`MCPRuntime` from the manifest
exactly as it is at boot.

Locks the issue 0095 acceptance criteria:

- **full chain (convergence)** — a weather goal surfaces ``get_forecast`` via
  :func:`select_tools`, a single tool call runs through the dispatcher, the
  terminal projection produces a Markdown deliverable card carrying the forecast
  text, and the run converges to ``done`` with a French spoken summary (no second
  LLM turn);
- **full chain (model-driven)** — with convergence disabled the model reads the
  forecast and emits ``done`` with a one-line French forecast as the spoken
  summary;
- **retrieval gating** — the forecast tool is advertised for a weather goal and
  EXCLUDED for a mail / web goal even with weather registered alongside the
  default gmail / web tools;
- **unreachable server** — an absent weather server registers nothing; a
  sub-agent whose forecast call fails (``mcp_*`` error) concludes ``done(failed)``
  with a French « service météo indisponible » sentence, the task is marked
  ``failed``, and no broken overlay is shipped (``result_payload`` stays null).
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
from contextlib import asynccontextmanager
from datetime import timedelta
from typing import Any

import pytest

from bob import ws_events
from bob.connectors.mcp import (
    MCPRuntime,
    MCPServerConfig,
    MCPToolOverride,
)
from bob.context.prompt_fragments import (
    SUB_AGENT_SKILL_PACKS,
    WEATHER_SKILL_PACK,
    select_skill_packs,
)
from bob.db.migrations_runner import apply_migrations, default_migrations_dir
from bob.event_bus import EventBus
from bob.llm.types import LLMResponse, ToolDefinition
from bob.llm_client import LLMClient
from bob.sub_agent import (
    SubAgentPolicy,
    SubAgentRunner,
    build_default_subagent_registry,
)
from bob.sub_agent.tool_registry import SubAgentToolRegistry
from bob.sub_agent.tool_retrieval import select_tools
from bob.task_store import TaskStore

# --- the manifest under test (mirrors the shipped .env.example / config.py) --

_FORECAST_TEXT = "Paris, demain : ensoleillé, 22 °C, vent faible."

_WEATHER_OVERRIDE = MCPToolOverride(
    description_fr="Donne la météo (prévision) pour un lieu et une date.",
    args=("place", "date"),  # narrowed surface — the upstream "units" is dropped
    tags=("météo", "weather", "temps", "prévision"),
    terminal=True,  # single-shot lookup converges
)


def _weather_server() -> MCPServerConfig:
    return MCPServerConfig(
        name="weather",
        transport="stdio",
        command="weather-mcp",
        expose=("get_forecast",),
        tools={"get_forecast": _WEATHER_OVERRIDE},
    )


# --- fakes (mirror tests/connectors/mcp/test_mcp_lifecycle.py) ---------------


class _FakeTool:
    def __init__(self, name: str) -> None:
        self.name = name
        self.description = "Get the weather forecast."
        self.inputSchema = {
            "type": "object",
            "properties": {
                "place": {"type": "string", "description": "City / region."},
                "date": {"type": "string", "description": "ISO date or relative day."},
                "units": {"type": "string", "description": "metric/imperial."},
            },
            "required": ["place"],
        }


class _ListResult:
    def __init__(self, tools: list[_FakeTool]) -> None:
        self.tools = tools


class _CallResult:
    def __init__(self, text: str, *, is_error: bool = False) -> None:
        self.content = [type("_B", (), {"type": "text", "text": text})()]
        self.isError = is_error


class _FakeSession:
    """Canned MCP session — records the args the forecast tool was called with."""

    def __init__(self, tools: list[_FakeTool], *, forecast_text: str) -> None:
        self._tools = tools
        self._forecast_text = forecast_text
        self.calls: list[dict[str, Any]] = []

    async def list_tools(self) -> _ListResult:
        return _ListResult(self._tools)

    async def call_tool(
        self,
        name: str,
        arguments: dict[str, Any] | None = None,
        read_timeout_seconds: timedelta | None = None,
    ) -> Any:
        self.calls.append({"name": name, "arguments": arguments or {}})
        return _CallResult(self._forecast_text)


def _fleet_factory(
    sessions: dict[str, _FakeSession],
    *,
    absent: set[str] | None = None,
) -> Any:
    """Build a fleet ``session_factory`` keyed by server name (absent ⇒ raises)."""

    absent = absent or set()

    @asynccontextmanager
    async def _cm(config: MCPServerConfig) -> Any:
        if config.name in absent:
            raise ConnectionError(f"{config.name} unreachable")
            yield  # pragma: no cover — makes this an async generator
        yield sessions[config.name]

    return lambda config: _cm(config)


async def _registry_with_weather(
    *, absent: bool = False
) -> tuple[SubAgentToolRegistry, _FakeSession | None]:
    """Build the default sub-agent registry + the manifest weather tool.

    Mirrors the boot path: the default gmail / web tools are registered, then
    :class:`MCPRuntime` connects the (mocked) weather server and folds the
    manifest curation onto the discovered ``get_forecast`` tool. When ``absent``
    the weather server is unreachable, so nothing is registered for it.
    """

    registry = build_default_subagent_registry()
    session = _FakeSession([_FakeTool("get_forecast")], forecast_text=_FORECAST_TEXT)
    sessions = {"weather": session}
    runtime = MCPRuntime(
        [_weather_server()],
        session_factory=_fleet_factory(sessions, absent={"weather"} if absent else None),
    )
    summary = await runtime.startup(registry)
    if absent:
        assert summary == {"weather": []}
        return registry, None
    assert summary == {"weather": ["get_forecast"]}
    return registry, session


# --- scripted LLM client (mirrors test_sub_agent_gmail_search.py) ------------


class _ScriptedClient(LLMClient):
    def __init__(self, chat_values: list[str]) -> None:
        self._chat_values = list(chat_values)
        self.calls: list[dict[str, Any]] = []

    async def chat(
        self,
        messages: list[dict[str, Any]],
        schema: dict[str, Any] | None = None,
        session_id: str | None = None,
    ) -> str:
        self.calls.append({"messages": messages, "schema": schema, "session_id": session_id})
        if not self._chat_values:
            raise AssertionError("_ScriptedClient ran out of canned chat() responses")
        return self._chat_values.pop(0)

    async def complete(
        self,
        messages: list[dict[str, Any]],
        tools: list[ToolDefinition] | None = None,
        session_id: str | None = None,
    ) -> LLMResponse:
        raise NotImplementedError("not used")


def _markdown_card_content(payload: list[dict[str, object]] | None) -> str:
    """Return the Markdown content string of the first section (typed for mypy)."""

    assert payload is not None
    first = payload[0]
    assert first["component"] == "Markdown"
    props = first["props"]
    assert isinstance(props, dict)
    content = props["content"]
    assert isinstance(content, str)
    return content


def _make_store() -> TaskStore:
    conn = sqlite3.connect(":memory:")
    apply_migrations(conn, default_migrations_dir())
    return TaskStore(conn)


def _make_running_task(store: TaskStore, goal: str) -> str:
    task_id = store.create_task(title="météo", goal=goal)
    store.update_state(task_id, "running")
    return task_id


def _make_runner(
    *,
    client: LLMClient,
    store: TaskStore,
    registry: SubAgentToolRegistry,
    converge: bool = True,
) -> SubAgentRunner:
    return SubAgentRunner(
        subagent_client=client,
        task_store=store,
        event_bus=EventBus(),
        policy=SubAgentPolicy(
            max_iterations=10,
            wall_clock_seconds=999.0,
            token_cap=999_999,
            converge_on_terminal_result=converge,
        ),
        tool_registry=registry,
    )


async def _run_capturing_ws(runner: SubAgentRunner, task_id: str) -> list[dict[str, Any]]:
    frames: list[dict[str, Any]] = []

    async def _capture(event: dict[str, Any]) -> None:
        frames.append(event)

    ws_events.set_emitter(_capture)
    try:
        await runner.run(task_id)
        for _ in range(5):
            await asyncio.sleep(0)
    finally:
        ws_events.set_emitter(None)
    return frames


# --- skill-pack registration -------------------------------------------------


def test_weather_skill_pack_registered_and_triggers() -> None:
    """The weather pack is in the ordered list and fires on the issue triggers."""

    assert WEATHER_SKILL_PACK in SUB_AGENT_SKILL_PACKS
    for goal in (
        "quel temps fait-il demain à Paris ?",
        "donne-moi la météo de Lyon",
        "prévision pour ce week-end à Lille",
        "what is the weather in London",
    ):
        packs = select_skill_packs(goal)
        assert WEATHER_SKILL_PACK in packs, goal

    # A pure mail goal never loads the weather recipe.
    assert WEATHER_SKILL_PACK not in select_skill_packs("mon dernier email reçu")


# --- retrieval gating (advertise for weather, exclude for mail / web) --------


async def test_forecast_advertised_for_weather_excluded_for_mail_and_web() -> None:
    registry, _ = await _registry_with_weather()

    weather = select_tools(registry, "quel temps demain à Paris", k=8, min_score=1)
    assert any(d.name == "get_forecast" for d in weather)

    mail = select_tools(registry, "mon dernier email reçu de Holyana", k=8, min_score=1)
    assert all(d.name != "get_forecast" for d in mail)

    web = select_tools(registry, "cherche sur internet le prix du bitcoin", k=8, min_score=1)
    assert all(d.name != "get_forecast" for d in web)


# --- full chain (convergence): retrieval → call → projection → French summary


@pytest.mark.asyncio
async def test_weather_runner_e2e_converges() -> None:
    """A single forecast call CONVERGES: card built from the terminal projection,
    French spoken summary, task done — all without a second LLM turn."""

    registry, session = await _registry_with_weather()
    assert session is not None

    # Only the tool call is scripted — convergence ends the run before the model
    # would emit ``done`` (mirrors the gmail converge test).
    script = [
        json.dumps(
            {
                "action": "tool_call",
                "name": "get_forecast",
                "args": {"place": "Paris", "date": "demain"},
            }
        ),
    ]
    client = _ScriptedClient(chat_values=script)
    store = _make_store()
    task_id = _make_running_task(store, "quel temps fait-il demain à Paris ?")

    runner = _make_runner(client=client, store=store, registry=registry, converge=True)
    frames = await _run_capturing_ws(runner, task_id)

    # Converged in ONE LLM call (the tool_call); no second turn was needed.
    assert len(client.calls) == 1
    # The narrowed args surface reached the server (place + date only).
    assert session.calls == [
        {"name": "get_forecast", "arguments": {"place": "Paris", "date": "demain"}}
    ]

    task = store.get_task(task_id)
    assert task.state == "done"

    # A deliverable card was produced (generic Markdown projection, not a typed
    # Weather card — the typed card is an explicit LATER upgrade).
    assert _FORECAST_TEXT in _markdown_card_content(task.result_payload)

    # The persisted result carries the forecast text; the WS task_result frame
    # ships the same to the chat client.
    assert task.result is not None
    assert _FORECAST_TEXT in task.result
    ws_results = [f for f in frames if f.get("type") == "task_result"]
    assert ws_results
    assert _FORECAST_TEXT in ws_results[-1]["result"]


# --- full chain (model-driven): the model synthesises the French forecast line


@pytest.mark.asyncio
async def test_weather_runner_e2e_model_driven_done() -> None:
    """With convergence disabled the model reads the forecast and emits ``done``
    referencing the stored result; the chain still ends with a French forecast
    deliverable + done task (2 LLM turns).

    Note: for a single-shot terminal tool the deterministic projection is the
    authoritative deliverable (anti-stall, PRD 0010) — the runner rebuilds the
    Markdown forecast card from the stored result via ``result_ref`` rather than
    trusting the weak model to reproduce it. So ``task.result`` carries the
    forecast text from the tool, and the model's ``result_summary`` rides along
    as the spoken summary fallback. Both the convergence path (above) and this
    model-driven path therefore surface the SAME French forecast deliverable."""

    registry, session = await _registry_with_weather()
    assert session is not None

    script = [
        json.dumps(
            {
                "action": "tool_call",
                "name": "get_forecast",
                "args": {"place": "Paris", "date": "demain"},
            }
        ),
        json.dumps(
            {
                "action": "done",
                "result_summary": "À Paris demain : ensoleillé, 22 °C.",
                "result_ref": "get_forecast#1",
                "ui_payload": None,
                "status": "complete",
                "reason_code": "ok",
                "cost": {},
            }
        ),
    ]
    client = _ScriptedClient(chat_values=script)
    store = _make_store()
    task_id = _make_running_task(store, "quel temps fait-il demain à Paris ?")

    runner = _make_runner(client=client, store=store, registry=registry, converge=False)
    frames = await _run_capturing_ws(runner, task_id)

    assert len(client.calls) == 2
    task = store.get_task(task_id)
    assert task.state == "done"
    # The forecast deliverable was rebuilt from the stored result (Markdown card,
    # not a typed Weather card) and carries the French forecast text.
    assert _FORECAST_TEXT in _markdown_card_content(task.result_payload)
    assert task.result is not None
    assert _FORECAST_TEXT in task.result
    ws_results = [f for f in frames if f.get("type") == "task_result"]
    assert ws_results
    assert _FORECAST_TEXT in ws_results[-1]["result"]


# --- unreachable server → French failure + task failed -----------------------


class _DeadOnCallSession(_FakeSession):
    """Discovers its tools (so registration succeeds) but every call_tool fails.

    Models a server that connects + lists tools at boot but is dead/wedged by
    the time the sub-agent invokes the forecast: the manager's restart-on-crash
    reconnects to the SAME dead session, the retried call raises again, and the
    failure surfaces as a structured ``mcp_unreachable`` (never a raw transport
    exception) — the boot-green / no-zombie invariant.
    """

    async def call_tool(self, *args: Any, **kwargs: Any) -> Any:
        raise ConnectionError("weather server crashed")


@pytest.mark.asyncio
async def test_weather_runner_unreachable_speaks_french_failure() -> None:
    """Weather server unreachable at call time: the forecast call errors
    (``mcp_*``), the sub-agent concludes ``done(failed)`` with a French « service
    météo indisponible » sentence, the task is marked failed, and no broken
    overlay is shipped (``result_payload`` stays null)."""

    # The tool registers at boot (list_tools succeeds) but every call_tool fails,
    # so the dispatched forecast surfaces a structured ``mcp_*`` error. The model
    # reacts with the pinned French failure speech.
    registry = build_default_subagent_registry()
    session = _DeadOnCallSession([_FakeTool("get_forecast")], forecast_text=_FORECAST_TEXT)
    runtime = MCPRuntime(
        [_weather_server()],
        session_factory=_fleet_factory({"weather": session}),
    )
    await runtime.startup(registry)
    assert registry.get("get_forecast") is not None

    failure_speech = "Le service météo est indisponible pour le moment — réessaie dans un instant."
    script = [
        json.dumps(
            {
                "action": "tool_call",
                "name": "get_forecast",
                "args": {"place": "Paris", "date": "demain"},
            }
        ),
        json.dumps(
            {
                "action": "done",
                "result_summary": failure_speech,
                "ui_payload": None,
                "status": "failed",
                "reason_code": "mcp_unreachable",
                "cost": {},
            }
        ),
    ]
    client = _ScriptedClient(chat_values=script)
    store = _make_store()
    task_id = _make_running_task(store, "quel temps fait-il demain à Paris ?")

    runner = _make_runner(client=client, store=store, registry=registry, converge=True)
    frames = await _run_capturing_ws(runner, task_id)

    task = store.get_task(task_id)
    assert task.state == "failed"

    # The tool's structured error round-tripped to the LLM as a ``tool`` message
    # with an ``mcp_*`` error code (never a raw transport exception).
    tool_msgs = [m for m in store.get_task_messages(task_id) if m.role == "tool"]
    assert tool_msgs
    body = json.loads(tool_msgs[-1].content)
    assert body["tool"] == "get_forecast"
    assert body["status"] == "error"
    assert body["error_code"].startswith("mcp_")

    # Bob speaks the pinned French failure sentence; no broken overlay (the
    # task_result frame carries the speech as text and no result_payload).
    ws_results = [f for f in frames if f.get("type") == "task_result"]
    assert ws_results
    assert ws_results[-1]["result"] == failure_speech
    assert "result_payload" not in ws_results[-1]
