"""JSON-backed store owning the runtime LLM selection.

This is a deep module: callers only see :class:`LLMSelectionStore` and the
:class:`LLMSelection` value object. All file plumbing — read/parse/seed/write,
defensive decoding of a hand-edited or legacy file — lives behind those
boundaries.

The store owns a single JSON file at ``{BOB_DATA_DIR}/llm_selection.json`` with
shape::

    {
        "provider": "lm_studio",
        "lm_model": "qwen2.5-7b-instruct",
        "context_length": {"qwen2.5-7b-instruct": 32768},
        "base_url": "http://192.168.1.20:1234/v1"
    }

Precedence / seeding (PRD 0012 / issue 0078):

- First boot with NO JSON file: seed from ``.env`` settings
  (``LLM_PROVIDER`` / ``LLM_MODEL``) and persist the JSON. From then on the
  JSON is the source of truth.
- Later boots: the JSON wins; ``.env`` is consulted only when the JSON is
  absent.

Decoding is defensive — a corrupt / partial / hand-edited file never raises:
missing or wrong-typed keys fall back to the seed defaults, and the
``context_length`` map keeps only ``str -> int`` entries.

Threading model mirrors :mod:`bob.jarvis_store`: a per-store
:class:`threading.Lock` serialises reads/writes so FastAPI request workers
cannot interleave with a concurrent persist.
"""

from __future__ import annotations

import json
import threading
from dataclasses import dataclass, field
from pathlib import Path

from bob.config import Settings

#: Filename of the selection JSON under ``BOB_DATA_DIR``.
LLM_SELECTION_FILENAME = "llm_selection.json"


@dataclass(frozen=True)
class LLMSelection:
    """The current LLM selection.

    ``provider`` is the active backend (``lm_studio`` / ``claude_cli``).
    ``lm_model`` is the selected model id (may be ``None`` when unset, e.g. a
    ``claude_cli`` selection that does not pin an LM Studio model).
    ``context_length`` maps a model id to its context window in tokens; it
    round-trips through write/read so a later slice can budget against it.
    """

    provider: str
    lm_model: str | None
    context_length: dict[str, int] = field(default_factory=dict)
    #: The OpenAI-compatible inference base URL for the LM Studio provider (e.g.
    #: ``http://192.168.1.20:1234/v1``). Drives BOTH the inference ``openai``
    #: client (via the factory) and the management SDK host (derived host:port).
    #: ``None`` falls back to ``settings.LLM_BASE_URL``. Runtime-swappable via the
    #: picker's URL field (``PUT /api/llm/selection {base_url}``).
    base_url: str | None = None

    def as_dict(self) -> dict[str, object]:
        """Serialise to the on-disk / REST JSON shape."""

        return {
            "provider": self.provider,
            "lm_model": self.lm_model,
            "context_length": dict(self.context_length),
            "base_url": self.base_url,
        }


class LLMSelectionStore:
    """Persistent JSON store owning the runtime LLM selection.

    The constructor takes the backing file path (its handle), consistent with
    the deep-module pattern used by :class:`bob.jarvis_store.JarvisStore` and
    :class:`bob.task_store.TaskStore`. The boot path in :mod:`bob.main` resolves
    the path under ``BOB_DATA_DIR`` and seeds from settings; tests point it at a
    ``tmp_path``.
    """

    def __init__(self, path: Path) -> None:
        self._path = path
        self._lock = threading.Lock()

    @property
    def path(self) -> Path:
        """The backing JSON file path."""

        return self._path

    def seed_from_settings(self, settings: Settings) -> LLMSelection:
        """Return the current selection, seeding from ``settings`` on first boot.

        - When the JSON file is absent: build the selection from ``.env``
          (``LLM_PROVIDER`` / ``LLM_MODEL``), persist it, and return it.
        - When the JSON file exists: ignore ``.env`` and return the persisted
          selection (decoded defensively).

        This is the single entry point the boot path calls; it guarantees the
        file exists afterwards so every later ``read`` is a pure load.
        """

        with self._lock:
            existing = self._read_unlocked()
            if existing is not None:
                return existing
            seeded = LLMSelection(
                provider=settings.LLM_PROVIDER,
                lm_model=settings.LLM_MODEL,
                context_length={},
                base_url=settings.LLM_BASE_URL or None,
            )
            self._write_unlocked(seeded)
            return seeded

    def read(self) -> LLMSelection | None:
        """Return the persisted selection, or ``None`` when the file is absent.

        Decoding is defensive: a corrupt / partial file is read as far as it
        parses, with missing keys defaulting to ``lm_studio`` / ``None`` /
        empty map. Returns ``None`` only when the file does not exist — that is
        the "first boot, not yet seeded" signal for callers.
        """

        with self._lock:
            return self._read_unlocked()

    def write(self, selection: LLMSelection) -> None:
        """Persist ``selection`` to the JSON file (atomic-ish full rewrite)."""

        with self._lock:
            self._write_unlocked(selection)

    # --- internals -----------------------------------------------------------

    def _read_unlocked(self) -> LLMSelection | None:
        if not self._path.exists():
            return None
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            # A corrupt or unreadable file collapses to defaults rather than
            # crashing boot — the selection is a runtime preference, never
            # load-bearing for the LLM call path (the client still has .env).
            raw = {}
        return _decode_selection(raw)

    def _write_unlocked(self, selection: LLMSelection) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._path.write_text(
            json.dumps(selection.as_dict(), indent=2, ensure_ascii=False),
            encoding="utf-8",
        )


def _decode_selection(raw: object) -> LLMSelection:
    """Decode an arbitrary parsed JSON value into an :class:`LLMSelection`.

    DEFENSIVE: never raises. A non-object, missing keys, or wrong-typed values
    fall back to ``lm_studio`` / ``None`` / ``{}``. The ``context_length`` map
    keeps only ``str -> int`` entries (bools are rejected — ``bool`` is an
    ``int`` subclass).
    """

    data = raw if isinstance(raw, dict) else {}

    provider_raw = data.get("provider")
    provider = provider_raw if isinstance(provider_raw, str) and provider_raw else "lm_studio"

    lm_model_raw = data.get("lm_model")
    lm_model = lm_model_raw if isinstance(lm_model_raw, str) and lm_model_raw else None

    ctx_raw = data.get("context_length")
    context_length: dict[str, int] = {}
    if isinstance(ctx_raw, dict):
        for key, value in ctx_raw.items():
            if isinstance(key, str) and isinstance(value, int) and not isinstance(value, bool):
                context_length[key] = value

    base_url_raw = data.get("base_url")
    base_url = base_url_raw if isinstance(base_url_raw, str) and base_url_raw else None

    return LLMSelection(
        provider=provider,
        lm_model=lm_model,
        context_length=context_length,
        base_url=base_url,
    )


# --- Singleton plumbing -------------------------------------------------------
#
# Mirrors :mod:`bob.jarvis_store` / :mod:`bob.task_store`. The boot path in
# :mod:`bob.main` builds the store under ``BOB_DATA_DIR``, seeds it from
# settings, then primes the singleton via :func:`set_default_store`. The REST
# router resolves it through :func:`get_default_store`.

_DEFAULT_STORE: LLMSelectionStore | None = None


def set_default_store(store: LLMSelectionStore | None) -> None:
    """Install (or clear) the process-wide singleton :class:`LLMSelectionStore`."""

    global _DEFAULT_STORE
    _DEFAULT_STORE = store


def get_default_store() -> LLMSelectionStore:
    """Return the process-wide singleton, raising if it hasn't been primed."""

    if _DEFAULT_STORE is None:
        raise RuntimeError(
            "LLMSelectionStore default singleton not initialised. "
            "Did the app lifespan (bob.main) run?"
        )
    return _DEFAULT_STORE


# =============================================================================
# Per-role selection — schema_version 2 (PRD 0016 / issue 0106, Annexe D)
# =============================================================================
#
# The realtime full-duplex agent (PRD 0016) drives FOUR distinct LLM roles, each
# of which may pick a DIFFERENT provider / server / model:
#
# - ``jarvis``   — the user-facing Speaker turn (was the single global selection)
# - ``thinker``  — the always-warm reasoning loop (consumed by a later slice)
# - ``draft``    — the speculative drafter (consumed by a later slice)
# - ``subagent`` — the autonomous sub-agent runner
#
# :class:`RoleSelection` is the v2 value object: a ``{role -> LLMSelection}`` map
# (reusing the flat :class:`LLMSelection` per role so the per-role provider /
# base_url / model / context_length contract is shared verbatim) plus an STT
# selection and a budget block. :class:`RoleSelectionStore` owns the SAME
# ``{BOB_DATA_DIR}/llm_selection.json`` file as :class:`LLMSelectionStore`, but
# in the ``schema_version: 2`` shape, and migrates an old flat v1 file forward
# on first read (the flat selection seeds ALL FOUR roles identically).
#
# Decoding stays as defensive as the v1 store: a corrupt / partial / hand-edited
# file never raises — missing or wrong-typed keys fall back to defaults, and
# ``budget.ceiling_gib: null`` is preserved as ``None`` ("detect later", S11).

#: The four LLM roles, in stable order. ``jarvis`` and ``subagent`` are live
#: today; ``thinker`` / ``draft`` exist in the map but are consumed by later
#: slices (S6 / S8). Anything outside this set is dropped on decode.
ROLES: tuple[str, ...] = ("jarvis", "thinker", "draft", "subagent")

#: Schema version written by :class:`RoleSelectionStore`. A file with a lower
#: (or absent) version is migrated forward; see :func:`_decode_role_selection`.
ROLE_SELECTION_SCHEMA_VERSION = 2

#: STT defaults (Annexe D). whisper.cpp with the large-v3-turbo model.
DEFAULT_STT_ENGINE = "whisper_cpp"
DEFAULT_STT_MODEL = "large-v3-turbo"

#: Budget defaults (Annexe D). ``ceiling_gib=None`` means "detect later" (the
#: local RAM probe in S11); ``reserve_gib`` is the OS head-room margin.
DEFAULT_BUDGET_RESERVE_GIB = 8.0


@dataclass(frozen=True)
class SttSelection:
    """The speech-to-text engine selection (Annexe D ``stt`` block)."""

    engine: str = DEFAULT_STT_ENGINE
    model: str = DEFAULT_STT_MODEL

    def as_dict(self) -> dict[str, object]:
        """Serialise to the on-disk / REST JSON shape."""

        return {"engine": self.engine, "model": self.model}


@dataclass(frozen=True)
class BudgetSelection:
    """The model-budget block (Annexe D ``budget`` block).

    ``ceiling_gib`` is the per-host RAM ceiling in GiB; ``None`` means "not set,
    detect later" (the local-RAM probe in S11 — kept distinct from ``0.0``).
    ``reserve_gib`` is the OS head-room margin subtracted from a detected
    ceiling. ``per_host_override`` maps a host (``host:port`` or base URL) to an
    explicit ceiling for remote servers whose RAM cannot be probed.
    """

    ceiling_gib: float | None = None
    reserve_gib: float = DEFAULT_BUDGET_RESERVE_GIB
    per_host_override: dict[str, float] = field(default_factory=dict)

    def as_dict(self) -> dict[str, object]:
        """Serialise to the on-disk / REST JSON shape."""

        return {
            "ceiling_gib": self.ceiling_gib,
            "reserve_gib": self.reserve_gib,
            "per_host_override": dict(self.per_host_override),
        }


@dataclass(frozen=True)
class RoleSelection:
    """The full per-role selection (``schema_version: 2``).

    ``roles`` maps each of :data:`ROLES` to its own flat :class:`LLMSelection`
    (provider / base_url / lm_model / context_length). ``stt`` and ``budget``
    carry the speech and model-budget blocks. The dataclass guarantees a value
    for every role: :meth:`RoleSelectionStore` always decodes into a complete
    map, defaulting any missing role to ``lm_studio`` / unpinned.
    """

    roles: dict[str, LLMSelection]
    stt: SttSelection = field(default_factory=SttSelection)
    budget: BudgetSelection = field(default_factory=BudgetSelection)
    schema_version: int = ROLE_SELECTION_SCHEMA_VERSION

    def role(self, role: str) -> LLMSelection:
        """Return the :class:`LLMSelection` for ``role``.

        Raises :class:`KeyError` for an unknown role — callers use the fixed
        :data:`ROLES` vocabulary, so a miss is a programming error, not a
        runtime-data condition (the store guarantees all four are present).
        """

        return self.roles[role]

    def with_role(self, role: str, selection: LLMSelection) -> RoleSelection:
        """Return a copy with ``role`` replaced by ``selection`` (others kept).

        The whole point of the per-role swap (issue 0106): mutating one role
        leaves the other three byte-for-byte unchanged so only the changed
        role's client is rebuilt.
        """

        if role not in ROLES:
            raise KeyError(f"Unknown LLM role: {role!r}")
        next_roles = dict(self.roles)
        next_roles[role] = selection
        return RoleSelection(
            roles=next_roles,
            stt=self.stt,
            budget=self.budget,
            schema_version=self.schema_version,
        )

    def as_dict(self) -> dict[str, object]:
        """Serialise to the on-disk / REST JSON shape (Annexe D)."""

        return {
            "schema_version": self.schema_version,
            "roles": {role: self.roles[role].as_dict() for role in ROLES},
            "stt": self.stt.as_dict(),
            "budget": self.budget.as_dict(),
        }


class RoleSelectionStore:
    """Persistent JSON store owning the per-role LLM selection (``schema_version: 2``).

    Mirrors :class:`LLMSelectionStore` (deep module, path injected, per-store
    :class:`threading.Lock`) but owns the v2 role-map shape. The store reads and
    writes the SAME ``{BOB_DATA_DIR}/llm_selection.json`` file; on first read of
    an old flat v1 file it migrates forward (the flat selection seeds all four
    roles) and rewrites the file in v2 shape.
    """

    def __init__(self, path: Path) -> None:
        self._path = path
        self._lock = threading.Lock()

    @property
    def path(self) -> Path:
        """The backing JSON file path."""

        return self._path

    def seed_from_settings(self, settings: Settings) -> RoleSelection:
        """Return the current selection, seeding from ``settings`` on first boot.

        - No JSON file: build a flat ``.env`` selection (``LLM_PROVIDER`` /
          ``LLM_MODEL`` / ``LLM_BASE_URL``), fan it out across all four roles,
          persist the v2 file, and return it.
        - A flat v1 JSON file: migrate it (the flat selection seeds all four
          roles), persist the v2 file, return it.
        - A v2 JSON file: return it decoded defensively.

        Single entry point for the boot path; guarantees a v2 file afterwards so
        every later :meth:`read` is a pure load.
        """

        with self._lock:
            raw = self._read_raw_unlocked()
            if raw is None:
                seeded = _seed_role_selection_from_settings(settings)
                self._write_unlocked(seeded)
                return seeded
            decoded = _decode_role_selection(raw)
            # Re-persist so a migrated v1 file (or a partial v2 file) is rewritten
            # in canonical v2 shape; the boot path then reads a clean file.
            self._write_unlocked(decoded)
            return decoded

    def read(self) -> RoleSelection | None:
        """Return the persisted selection, or ``None`` when the file is absent.

        Decoding is defensive: a corrupt / partial v2 file falls back to
        defaults key-by-key; a flat v1 file is migrated to the four-role map.
        ``None`` only when the file does not exist (the "not yet seeded" signal).
        """

        with self._lock:
            raw = self._read_raw_unlocked()
            if raw is None:
                return None
            return _decode_role_selection(raw)

    def write(self, selection: RoleSelection) -> None:
        """Persist ``selection`` to the JSON file (atomic-ish full rewrite)."""

        with self._lock:
            self._write_unlocked(selection)

    # --- internals -----------------------------------------------------------

    def _read_raw_unlocked(self) -> object | None:
        if not self._path.exists():
            return None
        try:
            parsed: object = json.loads(self._path.read_text(encoding="utf-8"))
            return parsed
        except (OSError, json.JSONDecodeError):
            # A corrupt / unreadable file collapses to ``{}`` so decode falls
            # back to all-default roles rather than crashing boot (the selection
            # is a runtime preference, never load-bearing for the call path).
            return {}

    def _write_unlocked(self, selection: RoleSelection) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._path.write_text(
            json.dumps(selection.as_dict(), indent=2, ensure_ascii=False),
            encoding="utf-8",
        )


def _seed_role_selection_from_settings(settings: Settings) -> RoleSelection:
    """Build the first-boot v2 selection from ``.env`` settings.

    The flat ``.env`` selection (provider / model / base_url) seeds ALL FOUR
    roles identically — the same fan-out the v1→v2 migration applies — so a
    fresh install behaves like the single-backend default until the picker
    splits roles apart. ``stt`` / ``budget`` take their defaults.
    """

    flat = LLMSelection(
        provider=settings.LLM_PROVIDER,
        lm_model=settings.LLM_MODEL,
        context_length={},
        base_url=settings.LLM_BASE_URL or None,
    )
    return RoleSelection(roles={role: flat for role in ROLES})


def _decode_role_selection(raw: object) -> RoleSelection:
    """Decode an arbitrary parsed JSON value into a :class:`RoleSelection`.

    DEFENSIVE: never raises. Dispatch on ``schema_version``:

    - ``2`` (or any ``roles`` map present) → decode the v2 shape, defaulting any
      missing / wrong-typed role, plus ``stt`` / ``budget``.
    - anything else (the flat v1 shape, or junk) → MIGRATE: decode the flat
      selection via :func:`_decode_selection` and fan it out across all four
      roles; ``stt`` / ``budget`` take defaults.
    """

    data = raw if isinstance(raw, dict) else {}

    version = data.get("schema_version")
    roles_raw = data.get("roles")
    is_v2 = (isinstance(version, int) and version >= 2) or isinstance(roles_raw, dict)

    if not is_v2:
        # Migration 1 -> 2: the flat selection seeds all four roles identically.
        flat = _decode_selection(data)
        return RoleSelection(roles={role: flat for role in ROLES})

    roles_map: dict[str, object] = roles_raw if isinstance(roles_raw, dict) else {}
    roles: dict[str, LLMSelection] = {}
    for role in ROLES:
        # Each role decodes through the SAME defensive flat decoder, so a
        # missing or mistyped per-role block collapses to lm_studio / unpinned.
        roles[role] = _decode_selection(roles_map.get(role))

    return RoleSelection(
        roles=roles,
        stt=_decode_stt(data.get("stt")),
        budget=_decode_budget(data.get("budget")),
    )


def _decode_stt(raw: object) -> SttSelection:
    """Decode the ``stt`` block, defaulting missing / wrong-typed fields."""

    data = raw if isinstance(raw, dict) else {}
    engine_raw = data.get("engine")
    engine = engine_raw if isinstance(engine_raw, str) and engine_raw else DEFAULT_STT_ENGINE
    model_raw = data.get("model")
    model = model_raw if isinstance(model_raw, str) and model_raw else DEFAULT_STT_MODEL
    return SttSelection(engine=engine, model=model)


def _decode_budget(raw: object) -> BudgetSelection:
    """Decode the ``budget`` block defensively.

    ``ceiling_gib`` keeps ``None`` ("detect later") when absent or null; a
    numeric value (int / float, not bool) is coerced to ``float``. ``reserve_gib``
    falls back to the default. ``per_host_override`` keeps only ``str -> number``
    entries (bools rejected — ``bool`` is an ``int`` subclass).
    """

    data = raw if isinstance(raw, dict) else {}

    ceiling_raw = data.get("ceiling_gib")
    ceiling_gib: float | None
    if isinstance(ceiling_raw, (int, float)) and not isinstance(ceiling_raw, bool):
        ceiling_gib = float(ceiling_raw)
    else:
        ceiling_gib = None

    reserve_raw = data.get("reserve_gib")
    if isinstance(reserve_raw, (int, float)) and not isinstance(reserve_raw, bool):
        reserve_gib = float(reserve_raw)
    else:
        reserve_gib = DEFAULT_BUDGET_RESERVE_GIB

    override_raw = data.get("per_host_override")
    per_host_override: dict[str, float] = {}
    if isinstance(override_raw, dict):
        for key, value in override_raw.items():
            if (
                isinstance(key, str)
                and isinstance(value, (int, float))
                and not isinstance(value, bool)
            ):
                per_host_override[key] = float(value)

    return BudgetSelection(
        ceiling_gib=ceiling_gib,
        reserve_gib=reserve_gib,
        per_host_override=per_host_override,
    )


# --- Per-role singleton plumbing ---------------------------------------------
#
# Mirrors the v1 singleton above. The boot path builds the store under
# ``BOB_DATA_DIR``, seeds it from settings, then primes the singleton. The
# per-role REST router resolves it through :func:`get_default_role_store`.

_DEFAULT_ROLE_STORE: RoleSelectionStore | None = None


def set_default_role_store(store: RoleSelectionStore | None) -> None:
    """Install (or clear) the process-wide singleton :class:`RoleSelectionStore`."""

    global _DEFAULT_ROLE_STORE
    _DEFAULT_ROLE_STORE = store


def get_default_role_store() -> RoleSelectionStore:
    """Return the process-wide per-role singleton, raising if not primed."""

    if _DEFAULT_ROLE_STORE is None:
        raise RuntimeError(
            "RoleSelectionStore default singleton not initialised. "
            "Did the app lifespan (bob.main) run?"
        )
    return _DEFAULT_ROLE_STORE
