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
    from bob.voice_retention_policy import VoiceRetentionPolicy

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

    # LLM provider selection.
    #
    # ``fake`` (PRD 0016 / issue 0098) is the attestation harness provider: a
    # deterministic, offline, scriptable backend selected by the ``bob attest``
    # CLI's :class:`EphemeralBackend`. It is NEVER the default and requires no
    # LM Studio / Claude CLI configuration — its scripted replies come from
    # :attr:`BOB_FAKE_LLM_SCRIPT`. Production configs only ever use the first
    # two providers; ``fake`` exists purely so the harness can boot an isolated
    # backend with a predictable LLM.
    LLM_PROVIDER: Literal["lm_studio", "claude_cli", "fake"] = "lm_studio"

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

    # Attestation harness scripted-LLM payload (PRD 0016 / issue 0098). A JSON
    # string carrying the scenario's ``fake_llm`` rules — a list of
    # ``{"role", "on_input_contains", "reply"}`` entries. Read ONLY by the
    # ``fake`` provider (:class:`bob.attest.fake_backend.FakeLlmClient`). The
    # ``bob attest`` CLI's :class:`EphemeralBackend` serialises the scenario
    # rules into this env var before booting the isolated subprocess, so the
    # scripted replies travel across the process boundary without a side
    # channel. Empty string ⇒ the fake replies with a generic deterministic
    # line (still attestable). Ignored entirely unless ``LLM_PROVIDER=fake``.
    BOB_FAKE_LLM_SCRIPT: str = ""

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

    # LM Studio inference transport selection (PRD 0017 / issue 0111).
    #
    # ``openai`` (default) routes LM Studio inference through the
    # OpenAI-compatible HTTP endpoint via :class:`bob.llm_client.LMStudioClient`
    # — the shipped behaviour, byte-for-byte unchanged. ``sdk`` routes it
    # through the official ``lmstudio`` Python SDK
    # (:class:`bob.llm.lmstudio_sdk.LMStudioSDKClient`) over the native
    # websocket transport (host derived from ``LLM_BASE_URL`` via
    # :func:`bob.lm_studio_manager.host_from_base_url`). The flag gates BOTH the
    # global and the per-role (PRD 0016) factory paths so the whole LM Studio
    # backend swaps atomically. Defaults to ``openai`` so nothing changes until
    # the SDK transport is validated end-to-end on a real server; flipping it
    # back is an instant rollback (no code change). Claude CLI is unaffected.
    LLM_LMSTUDIO_TRANSPORT: Literal["sdk", "openai"] = "openai"

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

    # Skip the boot-time Kokoro preload + warmup (PRD 0016 / issue 0098). The
    # lifespan normally downloads + warms the TTS pipeline so the first reply is
    # fast; that pulls in espeak-ng / misaki G2P which may be unavailable or
    # native-abort in a headless CI / harness environment. The attestation
    # harness drives only the TEXT path (no audio), so it boots its isolated
    # backend with this true to stay offline + fast. TTS still lazy-loads on the
    # first real synthesis if voice is ever requested. Default false preserves
    # production warm-start behaviour exactly.
    BOB_SKIP_TTS_PRELOAD: bool = False

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
    #         "description_fr": "Donne la météo (prévision) pour un lieu et une date.",
    #         "args": ["place", "date"], # narrowed argument subset
    #         "tags": ["météo", "weather", "temps", "prévision"],  # retrieval (0092)
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

    # Speech-to-text (PRD 0016 / issue 0099 — Jarvis real-time « Listen »).
    # The mic capture path (webview AudioWorklet) downsamples to 16 kHz mono
    # s16le and ships binary WS frames tagged ``0x01``; the backend decodes
    # them and feeds :class:`bob.stt_engine.SttEngine`. whisper.cpp
    # (Metal/CoreML on Apple Silicon) backs the default engine via
    # ``pywhispercpp`` with the ``large-v3-turbo`` model, downloaded lazily on
    # first use (mirrors the Kokoro HF-cache pattern — no manual artifacts, a
    # ``tts_preparing``-style toast is emitted while it downloads).
    #
    # OPTIONAL by design (no model_validator requirement): the backend boots
    # and the suite passes without the native model or ``pywhispercpp``
    # installed. The real engine is only loaded at first mic frame; tests
    # drive a deterministic fake engine behind the same interface. Set
    # ``STT_ENABLED=false`` to refuse mic frames entirely (server stays up).
    #
    # ``STT_SAMPLE_RATE`` is the contract sample rate of the inbound PCM
    # (16 kHz — what the webview worklet resamples to AND what whisper.cpp
    # expects); it is NOT a free dial, it must match Annexe A.1. ``STT_MODEL``
    # selects the whisper.cpp model name. ``STT_PARTIAL_MIN_CHARS`` debounces
    # noisy short partials away from the wire (only emit a partial once the
    # hypothesis grew past this many chars OR the stable prefix advanced).
    # ``STT_DEBUG_TEXT_MAX_CHARS`` caps how much user transcript text reaches
    # the debug ring buffer (the full text always reaches the client; the ring
    # buffer copy is truncated/masked — Privacy note, Annexe A.2).
    STT_ENABLED: bool = True
    STT_ENGINE: Literal["whisper_cpp", "fake"] = "whisper_cpp"
    STT_MODEL: str = "large-v3-turbo"
    STT_LANGUAGE: str = "fr"
    STT_SAMPLE_RATE: int = 16_000
    STT_PARTIAL_MIN_CHARS: int = 1
    STT_DEBUG_TEXT_MAX_CHARS: int = 16
    # Real-time STT bounds (anti-quadratic). whisper.cpp transcribes a BUFFER,
    # not a stream, so the per-turn session re-runs a pass to refresh the live
    # partial. Two knobs keep that cost bounded and independent of how long the
    # user has been talking:
    #
    # ``STT_PARTIAL_INTERVAL_SECONDS`` — minimum wall of NEW audio between two
    # partial passes (the cadence). Decoupled from ``STT_SAMPLE_RATE`` so the
    # refresh rate is a real dial; higher = fewer passes = less load (at the cost
    # of a slightly staler on-screen partial).
    #
    # ``STT_PARTIAL_WINDOW_SECONDS`` — the MAX trailing audio a single PARTIAL
    # pass transcribes. Without it each partial re-transcribed the WHOLE growing
    # buffer, so a long utterance made every pass cost O(utterance length) and
    # the partials fell seconds behind the speaker. Capping the partial to a
    # trailing window makes each pass O(window) regardless of utterance length.
    # The FINAL (frozen) transcript still runs ONE full-buffer pass, so accuracy
    # of the text handed to the say-path is unchanged; only the live partials are
    # windowed. For a normal command-length turn the window covers the whole
    # buffer, so behaviour is identical to before — the cap only bites on long
    # monologues. ``0`` disables the cap (whole-buffer partials, legacy).
    STT_PARTIAL_INTERVAL_SECONDS: float = 1.5
    STT_PARTIAL_WINDOW_SECONDS: float = 14.0

    # Attestation harness only (PRD 0016 / issue 0099): the canned transcript
    # the deterministic ``fake`` STT engine converges to. Injected by the
    # ``bob attest`` ``inject_audio`` path before booting the ephemeral backend
    # so an ``--audio`` scenario asserts a known ``stt_final`` without the
    # native whisper model. Ignored unless ``STT_ENGINE=fake``.
    BOB_FAKE_STT_TRANSCRIPT: str = ""

    # Attestation harness only (PRD 0016 / issue 0104): an end-of-phrase STT
    # REVISION for the fake engine. When non-empty the fake STT streams
    # ``BOB_FAKE_STT_TRANSCRIPT`` as partials (what the live Thinker + Draft see)
    # but freezes to THIS string at finalize — modelling the case a speculative
    # draft must guard against (the pre-written reply was built on the partial,
    # yet the settled clause diverged). Empty (the default) keeps the final equal
    # to the streamed transcript. Ignored unless ``STT_ENGINE=fake``.
    BOB_FAKE_STT_REVISE_TO: str = ""

    # Full-duplex loop — VAD + Endpointer + TurnFsm (PRD 0016 / issue 0100,
    # Annexe B). These drive the real-time turn-taking state machine over the
    # inbound mic frames (the same 16 kHz s16le frames the STT engine sees).
    #
    # ``VAD_*`` configure the energy-threshold :class:`bob.vad.EnergyVad`:
    # ``VAD_SPEECH_RMS`` is the normalised ([0,1]) RMS at/above which a frame
    # counts as speech (fast attack); ``VAD_PAUSE_MS`` is the short trailing
    # silence (within-utterance beat) that emits ``vad_pause`` (a backchannel
    # opportunity in later slices). ``ENDPOINT_SILENCE_MS`` is the *longer*
    # silence floor (~500-700 ms - issue 0100 scope) after which
    # :class:`bob.endpointer.Endpointer` declares ``endpoint`` (end of the
    # user's turn → freeze transcript → Jarvis say-path). The semantic
    # ``user_turn_complete`` endpoint is issue 0103; only the silence floor is
    # wired here. All three are derived against the ~30 ms mic frame size, so
    # they are expressed in ms / normalised units and stay engine-relative.
    VAD_SPEECH_RMS: float = 0.02
    VAD_PAUSE_MS: int = 300
    ENDPOINT_SILENCE_MS: int = 600

    # TTS engine selection (PRD 0016 / issue 0100). ``kokoro`` (default) is the
    # real local engine (:class:`bob.tts_service.KokoroTtsService`). ``fake`` is
    # the attestation-harness engine (:class:`bob.tts_service.FakeTtsService`):
    # a deterministic, offline, native-free generator of a fixed number of
    # silent PCM chunks per call — it lets the ``bob attest --audio`` full-duplex
    # scenario assert the audio-out path (``audio_chunks_gte``, FSM
    # ``bob_speaking``) without Kokoro / espeak-ng, exactly as the ``fake`` LLM /
    # STT engines do for their layers. NEVER the production default. The number
    # of chunks the fake yields per non-empty call is ``BOB_FAKE_TTS_CHUNKS``.
    TTS_ENGINE: Literal["kokoro", "fake"] = "kokoro"
    BOB_FAKE_TTS_CHUNKS: int = 2

    # Barge-in (PRD 0016 / issue 0101, Annexe B + F). While Bob speaks
    # (``bob_speaking``), the user can cut him off; :class:`bob.bargein.BargeInController`
    # requires this many ms of *continuous* user speech (the energy-VAD
    # decision) before confirming the interrupt — short backchannels / noise
    # below the window do NOT cut. Annexe B's band is 200-300 ms; the default
    # sits at the low end for snappy turn-taking (the derived ``bargein_cut_ms``
    # target is <300 ms end-to-end).
    BARGEIN_CONFIRM_MS: int = 200

    # Attestation harness only (PRD 0016 / issue 0101): per-chunk delay (ms) the
    # ``fake`` TTS engine sleeps between outbound chunks. Default 0 = the
    # instant streaming the 0100 audio scenario relies on (no regression). The
    # barge-in scenario raises it so Bob stays in ``bob_speaking`` long enough
    # for the injected confirmation window to land mid-reply (real TTS naturally
    # takes hundreds of ms; the fake otherwise finishes in microseconds, leaving
    # no window to barge into). Ignored unless ``TTS_ENGINE=fake``.
    BOB_FAKE_TTS_CHUNK_MS: int = 0

    # Thinker loop (PRD 0016 / issue 0102, Annexe H). The background
    # :class:`bob.thinker_loop.ThinkerLoop` re-runs its mini reasoning pass on a
    # new ``stt_partial`` but DEBOUNCED: a fresh partial within
    # ``THINKER_DEBOUNCE_MS`` of the last accepted trigger is coalesced, and at
    # most ONE Thinker inference is ever in flight per turn (a partial that
    # arrives while a pass is running is dropped, not queued). 250 ms is the PRD
    # default — long enough to coalesce a burst of partials, short enough that
    # the understanding stays fresh. ``THINKER_CANCEL_GRACE_MS`` is the
    # cooperative-cancel grace (mirrors the sub-agent ``cancel_grace_seconds``):
    # on ``endpoint`` / ``bargein`` / ``voice_stop`` the loop is asked to stop,
    # given this long to unwind a parked inference, then hard-killed via
    # :meth:`asyncio.Task.cancel`.
    THINKER_DEBOUNCE_MS: int = 250
    THINKER_CANCEL_GRACE_MS: int = 2000

    # Backchannels (PRD 0016 / issue 0105, Annexe B + A.2 + F). On a ``vad_pause``
    # during ``user_speaking`` Bob may place a brief acknowledgement ("mm", "ok je
    # vois") — gated by the background Thinker's ``backchannel`` trigger AND a
    # proactivity refractory window (:class:`bob.backchannel.BackchannelDecider`).
    # ``BACKCHANNEL_MIN_INTERVAL_MS`` is that silence-decay window: the minimum gap
    # between two backchannels on one turn, so pauses in a burst yield at most one
    # acknowledgement (not systematic). 0 disables the refractory (every relevant
    # pause is allowed). The backchannel is an ACTION in the pause — it never
    # transitions the floor (no ``bob_speaking``); the derived ``backchannel_ms``
    # (pause→ack) targets <500 ms.
    BACKCHANNEL_MIN_INTERVAL_MS: int = 1500

    # Speculative Draft / anticipation (PRD 0016 / issue 0104, Annexe A.2 + F + G).
    # While the user speaks, the ``draft`` role (a mini fast model) pre-writes the
    # conversational reply on the partial transcript
    # (:class:`bob.speculative_draft.SpeculativeDraft`). At the endpoint a PURE
    # commit gate decides whether to adopt it: a prefix fast-path (the final
    # transcript ≈ a prefix-or-extension of the partial the draft fired on) commits
    # instantly; otherwise a light token-overlap similarity guard commits when the
    # overlap is at/above ``DRAFT_COMMIT_SIMILARITY``; otherwise the draft is
    # discarded and the Speaker regenerates COLD. 0.6 is a forgiving default —
    # high enough to reject a genuinely divergent end-of-phrase, low enough that a
    # paraphrase-grade STT settle still commits. The cadence reuses the Thinker
    # debounce/grace knobs (THINKER_DEBOUNCE_MS / THINKER_CANCEL_GRACE_MS). The
    # derived ``endpoint_to_first_audio_ms`` on a committed draft targets <800 ms
    # (Annexe F). Degradation (Annexe G): when the ``draft`` model is unavailable
    # the WS layer omits the loop entirely → anticipation off, every turn cold.
    DRAFT_COMMIT_SIMILARITY: float = 0.6

    # Voice persistence + retention (PRD 0016 / issue 0109, Annexe E). A
    # finalized full-duplex voice turn is persisted to ``voice_turns`` +
    # ``voice_audio_blobs`` (:mod:`bob.voice_store`): the transcript / spoken
    # text / latency marks as a DB row, the mic-in + tts-out audio as WAV files
    # on disk under ``{BOB_DATA_DIR}/voice_audio/`` (only the path lives in the
    # DB). :class:`bob.voice_retention_policy.VoiceRetentionPolicy` keeps the
    # disk bounded with TWO SEPARATE caps (Annexe E.3): the audio blobs by total
    # SIZE (oldest first, file + row deleted) and the transcript rows by AGE.
    #
    # ``VOICE_PERSIST_ENABLED`` master-switches the whole persist path off (the
    # attest harness flips it for the retention scenario / leaves real captures
    # out of CI). ``VOICE_RETENTION_MAX_AUDIO_BYTES`` is the audio size ceiling
    # (default 1.5 GiB); ``VOICE_RETENTION_MAX_TURN_AGE_DAYS`` is the transcript
    # age window (default 30 days). Either set to 0 disables that dimension's
    # sweep (kept forever) — mirrors the ``None`` no-op of the event policy.
    VOICE_PERSIST_ENABLED: bool = True
    VOICE_RETENTION_MAX_AUDIO_BYTES: int = int(1.5 * 1024 * 1024 * 1024)
    VOICE_RETENTION_MAX_TURN_AGE_DAYS: float = 30.0

    def voice_retention_policy(self) -> VoiceRetentionPolicy:
        """Build the :class:`VoiceRetentionPolicy` from the settings dials.

        A ``0`` (or negative) cap maps to ``None`` (that dimension is not
        enforced — kept forever), matching the nullable-field no-op contract of
        the policy. Imported lazily so the config module never hard-depends on
        the voice store / policy package at import time (the gmail/tavily/mcp
        lazy-import pattern).
        """

        from bob.voice_retention_policy import VoiceRetentionPolicy

        max_bytes = self.VOICE_RETENTION_MAX_AUDIO_BYTES
        max_age_days = self.VOICE_RETENTION_MAX_TURN_AGE_DAYS
        return VoiceRetentionPolicy(
            max_audio_bytes=max_bytes if max_bytes > 0 else None,
            max_turn_age_seconds=(max_age_days * 24 * 60 * 60) if max_age_days > 0 else None,
        )

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
