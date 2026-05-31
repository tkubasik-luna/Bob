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
        "context_length": {"qwen2.5-7b-instruct": 32768}
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

    def as_dict(self) -> dict[str, object]:
        """Serialise to the on-disk / REST JSON shape."""

        return {
            "provider": self.provider,
            "lm_model": self.lm_model,
            "context_length": dict(self.context_length),
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

    return LLMSelection(provider=provider, lm_model=lm_model, context_length=context_length)


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
