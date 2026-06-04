"""Application configuration loaded from environment / `.env`.

Two LLM backends are supported, selected by ``LLM_PROVIDER``:

- ``lm_studio`` (default): OpenAI-compatible HTTP endpoint. Requires
  ``LLM_BASE_URL``, ``LLM_MODEL``, ``LLM_API_KEY``.
- ``claude_cli``: subprocess call to the ``claude`` CLI in ``-p`` mode.
  Requires ``claude`` on ``PATH`` (or set ``CLAUDE_CLI_BIN``).
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

if TYPE_CHECKING:
    from bob.connectors.mcp.models import MCPServerConfig

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent.parent
_ENV_FILE = _REPO_ROOT / ".env"


class Settings(BaseSettings):
    """Immutable application settings.

    Loaded from environment variables, optionally backed by a ``.env`` file
    located next to the process working directory.
    """

    model_config = SettingsConfigDict(
        env_file=str(_ENV_FILE),
        env_file_encoding="utf-8",
        frozen=True,
        extra="ignore",
    )

    # LLM provider selection
    LLM_PROVIDER: Literal["lm_studio", "claude_cli"] = "lm_studio"

    # LM Studio / OpenAI-compatible backend (required when LLM_PROVIDER=lm_studio)
    LLM_BASE_URL: str | None = None
    LLM_MODEL: str | None = None
    LLM_API_KEY: str | None = None
    LLM_TIMEOUT_SECONDS: float = 3600.0

    # Claude CLI backend (used when LLM_PROVIDER=claude_cli)
    CLAUDE_CLI_BIN: str = "claude"
    CLAUDE_CLI_MODEL: str | None = None
    # Per-call wall-clock cap for the ``claude`` CLI subprocess. Bumped
    # 120 -> 600 because long autonomous sub-agent generations (e.g. a full
    # written exposé / chronology) routinely exceed several minutes on the
    # first call and were dying with ``llm_failed`` at iteration 0. Jarvis'
    # own turns are short so the higher ceiling only ever helps the sub-agent
    # path. Tune via .env.
    CLAUDE_CLI_TIMEOUT_SECONDS: float = 600.0

    # Spawn the ``claude`` CLI in an isolated environment so the user's
    # personal ``~/.claude`` config does not bleed into Bob's backend calls.
    # When True the client adds ``--strict-mcp-config`` (no inherited MCP
    # servers) and ``--setting-sources ""`` (no user/project/local settings,
    # so SessionStart hooks — e.g. a "caveman mode" plugin — cannot inject a
    # competing system prompt on top of Bob's Jarvis persona) and runs the
    # subprocess from :attr:`BOB_DATA_DIR` so the repo's ``CLAUDE.md`` is not
    # auto-discovered. Keychain/OAuth auth is preserved (unlike ``--bare``,
    # which forces ``ANTHROPIC_API_KEY``). Set False to inherit the full
    # user environment (e.g. when authenticating via an ``apiKeyHelper`` that
    # lives in settings.json).
    CLAUDE_CLI_ISOLATED: bool = True

    # Orchestrator backends — slice #0018.
    # When unset they fall back to ``LLM_PROVIDER`` so callers can route the
    # Jarvis role and the sub-agent role to different backends if they want
    # (e.g. fast local LM Studio for Jarvis, claude-cli for sub-agents) while
    # the default keeps everything on a single backend.
    JARVIS_BACKEND: str | None = None
    SUBAGENT_BACKEND: str | None = None

    # Tool-calling wire-format selection (PRD 0008 / issue 0058).
    # ``auto`` (default) lets :func:`bob.llm.tooling.select_codec` pick the
    # most robust codec the backend declares it supports (native function
    # calling for LM Studio today). The explicit values force one codec and
    # raise loudly if the backend does not support it, so a misconfiguration
    # surfaces immediately instead of silently degrading. ``guided`` / ``hermes``
    # are accepted now but their codecs land in issues 0060 / 0061; selecting
    # them today raises ``CodecNotAvailableError``. No long-lived feature flag:
    # this is a capability override, not an on/off switch.
    LLM_TOOL_MODE: Literal["auto", "native", "guided", "hermes"] = "auto"

    # Implicit cap on concurrent running sub-tasks. The real cap + queue land
    # in slice #0020; this field exists now so callers can reference it
    # without breaking the config contract when the cap is wired up.
    MAX_RUNNING_TASKS: int = 3

    @model_validator(mode="after")
    def _validate_provider_requirements(self) -> Settings:
        if self.LLM_PROVIDER == "lm_studio":
            missing = [
                name
                for name, value in (
                    ("LLM_BASE_URL", self.LLM_BASE_URL),
                    ("LLM_MODEL", self.LLM_MODEL),
                    ("LLM_API_KEY", self.LLM_API_KEY),
                )
                if not value
            ]
            if missing:
                raise ValueError(f"LLM_PROVIDER=lm_studio requires: {', '.join(missing)}")
        return self

    # Backend
    BACKEND_HOST: str = "127.0.0.1"
    BACKEND_PORT: int = 8000

    # Logging
    LOG_LEVEL: str = "INFO"

    # When true, the debug event producer (:mod:`bob.debug_log`) also appends
    # every emitted event as a JSON line to ``logs/orchestration.jsonl`` so the
    # full orchestration trace survives the process and can be read / grepped
    # offline. The WS debug feed only lives while a client is connected and the
    # in-memory ring buffer is bounded; this file is the durable record.
    ORCHESTRATION_LOG_ENABLED: bool = True

    # Persistence — Jarvis thread + future task data live under this directory.
    # Resolved lazily so tests can override via ``BOB_DATA_DIR`` env var with a
    # tmp path; the boot path in :mod:`bob.main` ensures the directory exists.
    BOB_DATA_DIR: Path = Path.home() / ".bob"

    # When true, the lifespan wipes ``{BOB_DATA_DIR}/bob.db`` before opening it
    # so every server start sees a fresh Jarvis thread + empty task list.
    # ``jarvis.md`` (personality) and ``logs/*.jsonl`` (audit) are preserved.
    # Tests set this to ``false`` in ``conftest.py`` to keep existing fixtures.
    BOB_CLEAR_ON_START: bool = True

    # Kokoro TTS — local engine via the upstream ``kokoro`` (KPipeline) package.
    # Model weights are downloaded by Hugging Face's cache the first time the
    # pipeline is instantiated (``hexgrad/Kokoro-82M``); no manual artifacts.
    # ``KOKORO_LANG_CODE`` is the single-letter pipeline language ('f' = French,
    # 'a' = American English, 'b' = British English, etc.) used by KPipeline +
    # misaki G2P. Sample rate is a model constant exposed by
    # :data:`bob.tts_service.KOKORO_SAMPLE_RATE` — not a settings dial.
    KOKORO_LANG_CODE: str = "f"
    KOKORO_DEFAULT_VOICE: str = "ff_siwis"
    KOKORO_DEFAULT_SPEED: float = 1.0
    KOKORO_HF_REPO_ID: str = "hexgrad/Kokoro-82M"

    # Gmail connector (PRD 0007) — paths to the OAuth client secrets file
    # downloaded from the user's GCP project and the cached user token
    # persisted after the first interactive consent. Both default under
    # ``~/.bob/gmail/`` (kept separate from ``BOB_DATA_DIR``'s SQLite store
    # so the user can wipe the chat DB without losing their Gmail token).
    # Environment overrides: ``GMAIL_CREDENTIALS_PATH`` and
    # ``GMAIL_TOKEN_PATH``.
    GMAIL_CREDENTIALS_PATH: Path = Path.home() / ".bob" / "gmail" / "credentials.json"
    GMAIL_TOKEN_PATH: Path = Path.home() / ".bob" / "gmail" / "token.json"

    # Tavily web search — backs the ``web_search`` / ``web_fetch`` sub-agent
    # tools (Tavily Search / Extract REST endpoints). ``TAVILY_API_KEY`` is a
    # free-tier key from https://app.tavily.com. It is intentionally OPTIONAL
    # (no model_validator requirement): when unset the tool handlers return an
    # actionable ``web_search_missing_key`` / ``web_fetch_missing_key`` error
    # instead of crashing, so the backend boots and the suite passes without a
    # key (it is only needed at call time). ``TAVILY_BASE_URL`` is overridable
    # for a proxy / self-host; ``TAVILY_TIMEOUT_SECONDS`` bounds each outbound
    # HTTP call; ``WEB_SEARCH_MAX_RESULTS`` caps results when a call omits its
    # own ``max_results``.
    TAVILY_API_KEY: str | None = None
    TAVILY_BASE_URL: str = "https://api.tavily.com"
    TAVILY_TIMEOUT_SECONDS: float = 15.0
    WEB_SEARCH_MAX_RESULTS: int = 5

    # Tool retrieval gating (PRD 0015 / issue 0092). The sub-agent runner
    # advertises only the most goal-relevant tools to the model instead of the
    # whole registry, via :func:`bob.sub_agent.tool_retrieval.select_tools`.
    # ``TOOL_RETRIEVAL_K`` caps the number of *relevance-retrieved* tools shown
    # (``always_on`` core tools are always shown on top of this and do not count
    # against the cap); ``TOOL_RETRIEVAL_MIN_SCORE`` is the minimum lexical
    # relevance score a tool must reach to be advertised. Dispatch is unaffected:
    # a registered-but-not-advertised tool still resolves when the model calls it
    # by name. Defaults are generous enough that today's 3-tool registry is fully
    # advertised for a matching goal; they only bite once an MCP fleet lands.
    TOOL_RETRIEVAL_K: int = 8
    TOOL_RETRIEVAL_MIN_SCORE: int = 1

    # MCP server manifest (PRD 0015 / issue 0094). A first-order, config-driven
    # list of MCP servers Bob connects to as a *client* at boot. A developer
    # branches a new tool by adding an entry here — no code. Each entry is a dict:
    #
    #   {
    #     "name": "weather",            # stable id; tool refs + logs key on it
    #     "transport": "stdio",         # "stdio" (subprocess) | "http" (remote)
    #     "command": "uvx", "args": [...],   # stdio invocation
    #     "url": "https://...",          # http endpoint
    #     "env": {"API_KEY": "..."},
    #     "expose": ["get_forecast"],   # allowlist — ONLY these tools are wrapped
    #     "tools": {                      # per-tool curation overrides
    #       "get_forecast": {
    #         "description_fr": "Donne la météo d'une ville.",
    #         "args": ["city"],          # narrowed argument subset
    #         "tags": ["météo", "weather", "temps"],   # boost retrieval (0092)
    #         "terminal": true           # single-shot lookup converges
    #       }
    #     }
    #   }
    #
    # Mirrors how ``TAVILY_API_KEY`` gates Tavily: the manifest is OPTIONAL and
    # boot-green. Empty / absent ⇒ no MCP tools. A server that is down / absent at
    # boot is logged actionably and registers nothing while its peers register
    # normally — the boot never crashes. Set via env as a JSON list
    # (``MCP_SERVERS=[{"name": ...}]``). Parse into typed configs with
    # :meth:`mcp_server_configs`. ``MCP_CALL_TIMEOUT_SECONDS`` bounds each
    # outbound MCP tool call so a slow / wedged server surfaces a structured
    # ``mcp_unreachable`` error instead of hanging the sub-agent.
    MCP_SERVERS: list[dict[str, Any]] = Field(default_factory=list)
    MCP_CALL_TIMEOUT_SECONDS: float = 30.0

    def mcp_server_configs(self) -> tuple[MCPServerConfig, ...]:
        """Parse :attr:`MCP_SERVERS` into typed :class:`MCPServerConfig` records.

        Lenient (see :func:`bob.connectors.mcp.models.parse_mcp_servers`): a
        malformed entry is dropped rather than crashing the boot. Imported lazily
        so the config module never pulls in the MCP connector package (the
        gmail/tavily lazy-import pattern).
        """

        from bob.connectors.mcp.models import parse_mcp_servers

        return parse_mcp_servers(self.MCP_SERVERS)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return a cached :class:`Settings` instance.

    Raises :class:`pydantic.ValidationError` at first call if a required
    variable is missing — crashing the process early as designed.
    """

    return Settings()
