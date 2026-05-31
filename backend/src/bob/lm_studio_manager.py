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

import lmstudio

#: Default LM Studio API host. The SDK falls back to its own discovery when
#: ``None`` is passed; we keep an explicit default so a misconfigured host is
#: visible at the call site.
DEFAULT_LM_STUDIO_HOST = "localhost:1234"

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


class _SDKClient(Protocol):
    """The subset of the ``lmstudio`` client surface we depend on."""

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
