"""Deep module owning LM Studio *model management* (PRD 0012 / issue 0079).

This module is the single boundary onto the official ``lmstudio`` Python SDK.
It is used ONLY for management concerns — listing the locally downloaded
models and their metadata. Inference still runs through :mod:`bob.llm_client`
on the ``openai`` client; the two never share a code path.

Public surface:

- :class:`LMStudioModel` — a value object describing one chat-capable model:
  ``id``, ``quantisation``, ``architecture``, ``max_context_length``,
  ``loaded``.
- :class:`LMStudioManager.list_models` — returns the live list, filtered to
  chat-capable models only (``type`` in ``{"llm", "vlm"}``; embeddings are
  excluded).
- :class:`LMStudioUnavailableError` — a DISTINCT, catchable error raised when
  the LM Studio server is unreachable, so the REST layer can map it to a clean
  HTTP error instead of leaking an SDK traceback / 500.

The SDK is faked at this boundary in tests (see
``tests/test_lm_studio_manager.py``), so the test suite is fully offline and
deterministic — no running LM Studio server is required.
"""

from __future__ import annotations

import contextlib
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Protocol
from urllib.parse import urlsplit

import lmstudio

#: Default LM Studio API host. The SDK falls back to its own discovery when
#: ``None`` is passed; we keep an explicit default so a misconfigured host is
#: visible at the call site.
DEFAULT_LM_STUDIO_HOST = "localhost:1234"


def host_from_base_url(base_url: str | None) -> str:
    """Derive the ``lmstudio`` SDK ``host:port`` from the inference ``LLM_BASE_URL``.

    Inference runs on the ``openai`` client against an OpenAI-compatible URL like
    ``http://192.168.86.21:1234/v1``; the management SDK wants the bare
    ``host:port`` (no scheme, no ``/v1`` path). Both must point at the same server,
    so the manager host is derived from the same setting rather than configured
    twice. Falls back to :data:`DEFAULT_LM_STUDIO_HOST` when the URL is absent or
    has no network location.
    """

    if not base_url:
        return DEFAULT_LM_STUDIO_HOST
    parsed = urlsplit(base_url if "//" in base_url else f"//{base_url}")
    return parsed.netloc or DEFAULT_LM_STUDIO_HOST

#: ``LlmInfo.type`` values we treat as chat-capable. ``vlm`` (vision LLM) is a
#: chat model with image input; ``embedding`` models are excluded.
_CHAT_MODEL_TYPES = frozenset({"llm", "vlm"})


@dataclass(frozen=True)
class LMStudioModel:
    """One chat-capable LM Studio model and its management metadata.

    - ``id`` — the model key LM Studio identifies it by (e.g.
      ``qwen2.5-7b-instruct``).
    - ``quantisation`` — the GGUF quant format (e.g. ``Q4_K_M``), surfaced from
      the SDK ``format`` field; ``None`` when the SDK omits it.
    - ``architecture`` — the model family (e.g. ``qwen2``); ``None`` when absent.
    - ``max_context_length`` — the trained context window in tokens.
    - ``loaded`` — whether the model is currently resident in LM Studio.
    """

    id: str
    quantisation: str | None
    architecture: str | None
    max_context_length: int | None
    loaded: bool

    def as_dict(self) -> dict[str, object]:
        """Serialise to the REST JSON shape."""

        return {
            "id": self.id,
            "quantisation": self.quantisation,
            "architecture": self.architecture,
            "max_context_length": self.max_context_length,
            "loaded": self.loaded,
        }


class LMStudioUnavailableError(RuntimeError):
    """The LM Studio server could not be reached.

    Raised by :meth:`LMStudioManager.list_models` when the underlying SDK
    raises a connection-level error. Distinct from a programming bug so the
    REST layer can map it to an explicit HTTP error response rather than a
    500 traceback.
    """


class LMStudioModelNotFoundError(RuntimeError):
    """The requested model id is not a downloaded LM Studio model.

    Raised by :meth:`LMStudioManager.load` when the SDK reports the target
    ``model_id`` is unknown (no such download). Distinct from a load failure so
    the REST layer maps it to a 404-flavoured error and the swap path keeps the
    previous selection.
    """


class LMStudioLoadError(RuntimeError):
    """Loading the target model failed (e.g. out of memory).

    Raised by :meth:`LMStudioManager.load` when the SDK accepts the model id
    but the load itself fails — most commonly VRAM/RAM exhaustion (OOM). The
    swap path catches this, keeps the previous selection and does NOT persist
    the JSON.
    """


class _SDKModelInfo(Protocol):
    """Structural view of the SDK ``info`` object we read from.

    The real object is a ``msgspec`` struct (``LlmInfo``); the test fake is a
    plain object exposing the same attributes. We only read; never mutate.
    """

    type: str
    model_key: str
    format: str | None
    architecture: str | None
    max_context_length: int | None
    vision: bool


class _SDKDownloadedModel(Protocol):
    """Structural view of one entry returned by ``list_downloaded_models``."""

    info: _SDKModelInfo


class _SDKLoadedModel(Protocol):
    """Structural view of one entry returned by ``list_loaded_models``."""

    identifier: str


class _SDKLlmNamespace(Protocol):
    """The subset of the SDK ``client.llm`` session surface we depend on.

    ``load_new_instance`` loads a downloaded model into memory; ``unload``
    evicts a currently-loaded instance by its identifier. Both are management
    calls — inference still runs through :mod:`bob.llm_client` on ``openai``.
    """

    def load_new_instance(
        self,
        model_key: str,
        *,
        config: dict[str, object] | None = ...,
    ) -> object: ...

    def unload(self, model_identifier: str) -> None: ...


class _SDKClient(Protocol):
    """The subset of the ``lmstudio`` client surface we depend on."""

    @property
    def llm(self) -> _SDKLlmNamespace: ...

    def list_downloaded_models(self) -> Sequence[_SDKDownloadedModel]: ...

    def list_loaded_models(self) -> Sequence[_SDKLoadedModel]: ...

    def close(self) -> None: ...


#: Factory type for the SDK client. The default builds a real
#: ``lmstudio.Client``; tests inject a fake to stay offline.
ClientFactory = Callable[[str], "_SDKClient"]


def _default_client_factory(host: str) -> _SDKClient:
    """Build a real ``lmstudio.Client`` for ``host``."""

    return lmstudio.Client(host)  # type: ignore[return-value]


class LMStudioManager:
    """Management-only view onto a local LM Studio server.

    The constructor takes the API host and an optional client factory (the DI
    seam used by tests to inject a fake SDK client). Inference is out of scope:
    this object only lists models and their metadata.
    """

    def __init__(
        self,
        host: str = DEFAULT_LM_STUDIO_HOST,
        client_factory: ClientFactory | None = None,
    ) -> None:
        self._host = host
        self._client_factory = client_factory or _default_client_factory

    @property
    def host(self) -> str:
        """The configured LM Studio API host."""

        return self._host

    def set_host(self, host: str) -> None:
        """Repoint the manager at a new ``host:port`` (runtime URL swap).

        Each management call opens a fresh client at ``self._host``, so a later
        call simply targets the new host — no persistent connection to rebuild.
        Used by :meth:`bob.llm_swap.LLMSwitcher.swap_base_url`.
        """

        self._host = host

    def probe(self) -> bool:
        """Return whether the LM Studio server is reachable (a real ping).

        A lightweight management round-trip (``list_loaded_models``) that never
        raises: an unreachable server collapses to ``False``. Backs the
        ``GET /api/llm/ping`` health check the picker uses to confirm "online".
        """

        try:
            self.loaded_model_ids()
        except LMStudioUnavailableError:
            return False
        return True

    def list_models(self) -> list[LMStudioModel]:
        """Return the live list of chat-capable downloaded models.

        Filters to ``type`` in ``{"llm", "vlm"}`` (embeddings excluded), and
        marks each model ``loaded`` when its key matches a currently loaded
        model identifier.

        Raises :class:`LMStudioUnavailableError` when the server is unreachable
        — the caller never sees a raw SDK error.
        """

        client = self._open_client()
        try:
            downloaded = client.list_downloaded_models()
            loaded_ids = self._loaded_ids(client)
        except lmstudio.LMStudioError as exc:
            raise LMStudioUnavailableError(
                f"LM Studio server unreachable at {self._host!r}: {exc}"
            ) from exc
        finally:
            self._safe_close(client)

        models: list[LMStudioModel] = []
        for entry in downloaded:
            info = getattr(entry, "info", None)
            if info is None:
                continue
            if getattr(info, "type", None) not in _CHAT_MODEL_TYPES:
                continue
            model_id = getattr(info, "model_key", None)
            if not isinstance(model_id, str) or not model_id:
                continue
            models.append(
                LMStudioModel(
                    id=model_id,
                    quantisation=_opt_str(getattr(info, "format", None)),
                    architecture=_opt_str(getattr(info, "architecture", None)),
                    max_context_length=_opt_int(getattr(info, "max_context_length", None)),
                    loaded=model_id in loaded_ids,
                )
            )
        return models

    def loaded_model_ids(self) -> list[str]:
        """Return the ids of models currently loaded in LM Studio.

        Used for cold-start resolution (issue 0080): when the selection pins no
        model, the boot path prefers an already-loaded model over loading a new
        one. Order follows the SDK's ``list_loaded_models`` order.

        Raises :class:`LMStudioUnavailableError` when the server is unreachable.
        """

        client = self._open_client()
        try:
            loaded = client.list_loaded_models()
        except lmstudio.LMStudioError as exc:
            raise LMStudioUnavailableError(
                f"LM Studio server unreachable at {self._host!r}: {exc}"
            ) from exc
        finally:
            self._safe_close(client)

        ids: list[str] = []
        for handle in loaded:
            identifier = getattr(handle, "identifier", None)
            if isinstance(identifier, str) and identifier:
                ids.append(identifier)
        return ids

    def load(
        self, model_id: str, context_length: int | None = None, *, reload: bool = False
    ) -> None:
        """Load ``model_id`` into LM Studio, offloading every other model first.

        Offload-BEFORE-load at the SDK boundary (the user's stated ask — frees
        VRAM so a fresh load can't OOM against the previous resident model):

        1. Snapshot the currently-loaded model identifiers.
        2. **Already loaded + not a forced reload** → no-op: keep the resident
           instance and simply let the caller pin it as the selection. We do NOT
           offload it ("if a model is already loaded and I select it, don't
           offload it, just select it").
        3. Otherwise unload EVERY currently-loaded model (including the target on
           a forced ``reload``) so memory is freed before the new load.
        4. ``load_new_instance(model_id, config={"contextLength": …})`` — load
           the target (ctx omitted when ``None`` → SDK default).

        ``reload`` forces a fresh load even when the target is already resident —
        used by the ctx-slider Apply path, which must re-load at the new window.

        Errors are surfaced as DISTINCT, catchable types so the swap coordinator
        keeps the previous selection and the REST layer maps them cleanly:

        - server unreachable → :class:`LMStudioUnavailableError`
        - unknown model id → :class:`LMStudioModelNotFoundError`
        - load failed (e.g. OOM) → :class:`LMStudioLoadError`

        TRADEOFF: offloading first means a failed load leaves NO model resident
        (the previous one is already evicted). The caller surfaces the error and
        the user reselects — accepted to kill the OOM/double-load flakiness.
        """

        client = self._open_client()
        try:
            previous = self._loaded_ids(client)

            # Already loaded + plain select → keep it resident, offload nothing.
            if model_id in previous and not reload:
                return

            # Free VRAM BEFORE loading the new model: unload every resident
            # instance (incl. the target on a forced reload). Best-effort —
            # an unload that fails must not abort the load.
            for identifier in previous:
                with contextlib.suppress(lmstudio.LMStudioError):
                    client.llm.unload(identifier)

            config: dict[str, object] | None = None
            if context_length is not None:
                config = {"contextLength": context_length}
            try:
                client.llm.load_new_instance(model_id, config=config)
            except lmstudio.LMStudioModelNotFoundError as exc:
                raise LMStudioModelNotFoundError(
                    f"LM Studio model not found: {model_id!r}: {exc}"
                ) from exc
            except lmstudio.LMStudioError as exc:
                # Server-side load failure — most commonly OOM. Distinct from the
                # unreachable-server case (the connection itself succeeded).
                raise LMStudioLoadError(
                    f"LM Studio failed to load {model_id!r}: {exc}"
                ) from exc
        finally:
            self._safe_close(client)

    # --- internals -----------------------------------------------------------

    def _open_client(self) -> _SDKClient:
        try:
            return self._client_factory(self._host)
        except lmstudio.LMStudioError as exc:
            raise LMStudioUnavailableError(
                f"LM Studio server unreachable at {self._host!r}: {exc}"
            ) from exc

    @staticmethod
    def _loaded_ids(client: _SDKClient) -> set[str]:
        loaded: set[str] = set()
        for handle in client.list_loaded_models():
            identifier = getattr(handle, "identifier", None)
            if isinstance(identifier, str) and identifier:
                loaded.add(identifier)
        return loaded

    @staticmethod
    def _safe_close(client: _SDKClient) -> None:
        close = getattr(client, "close", None)
        if callable(close):
            with contextlib.suppress(Exception):  # pragma: no cover - defensive close
                close()


def _opt_str(value: object) -> str | None:
    return value if isinstance(value, str) and value else None


def _opt_int(value: object) -> int | None:
    if isinstance(value, bool):  # bool is an int subclass — reject it
        return None
    return value if isinstance(value, int) else None
