"""Tests for :mod:`bob.lm_studio_manager` and the GET /api/llm/models endpoint.

The ``lmstudio`` SDK is faked at the system boundary (the client factory) so
the suite is fully offline and deterministic — no running LM Studio server is
required. A fake client replays a scripted ``list_downloaded_models`` /
``list_loaded_models`` pair; an "unreachable server" is modelled by a factory
that raises the SDK's ``LMStudioError``.

Migration note (issue 0107): the ``load``-policy tests were migrated from the
old **offload-first** behaviour (load evicted EVERY resident model first) to the
v2 **multi-load** behaviour (peers stay resident; a model is evicted only by
ref-count via :meth:`LMStudioManager.release_role`). The migrated assertions now
expect ``unloaded == []`` where they used to expect the previous model evicted.
The ``--- v2 multi-load …`` section below adds the new ref-counted +
budget-aware coverage.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, cast

import lmstudio
from fastapi.testclient import TestClient

from bob.config import Settings
from bob.lm_studio_manager import (
    DEFAULT_LM_STUDIO_HOST,
    LMStudioLoadError,
    LMStudioManager,
    LMStudioModelNotFoundError,
    LMStudioUnavailableError,
    ModelBudgetExceededError,
    _SDKClient,
    _SDKDownloadedModel,
    _SDKLoadedModel,
    _SDKModelInfo,
    host_from_base_url,
    is_local_host,
)
from bob.main import app
from bob.model_budget import HostBudget


def _settings(**overrides: object) -> Settings:
    base: dict[str, object] = {
        "LLM_PROVIDER": "lm_studio",
        "LLM_BASE_URL": "http://localhost:1234/v1",
        "LLM_MODEL": "qwen2.5-7b-instruct",
        "LLM_API_KEY": "lm-studio",
    }
    base.update(overrides)
    return Settings(**base)  # type: ignore[arg-type]


# --- SDK fakes ---------------------------------------------------------------


class _FakeInfo:
    """Stand-in for the SDK ``LlmInfo`` / ``EmbeddingModelInfo`` struct."""

    def __init__(
        self,
        *,
        type: str,
        model_key: str,
        format: str | None = "Q4_K_M",
        architecture: str | None = "qwen2",
        max_context_length: int | None = 32768,
        vision: bool = False,
    ) -> None:
        self.type = type
        self.model_key = model_key
        self.format = format
        self.architecture = architecture
        self.max_context_length = max_context_length
        self.vision = vision


class _FakeDownloaded:
    def __init__(self, info: _SDKModelInfo) -> None:
        self.info = info


class _FakeLoaded:
    def __init__(self, identifier: str) -> None:
        self.identifier = identifier


class _FakeLlmNamespace:
    """Stand-in for the SDK ``client.llm`` session surface.

    Records every ``load_new_instance`` / ``unload`` call so tests can assert
    the validate-then-swap order. ``load_error`` (when set) is raised by EVERY
    ``load_new_instance``; ``fail_models`` (issue 0107) raises an OOM for ONLY
    the named model ids so a multi-load test can fail one role's load while the
    rest succeed.
    """

    def __init__(
        self,
        load_error: Exception | None = None,
        fail_models: set[str] | None = None,
    ) -> None:
        self.load_error = load_error
        self.fail_models = fail_models or set()
        self.loaded: list[tuple[str, object]] = []
        self.unloaded: list[str] = []

    def load_new_instance(
        self,
        model_key: str,
        *,
        config: object | None = None,
    ) -> object:
        if self.load_error is not None:
            raise self.load_error
        if model_key in self.fail_models:
            raise lmstudio.LMStudioServerError(f"out of memory loading {model_key}")
        self.loaded.append((model_key, config))
        return object()

    def unload(self, model_identifier: str) -> None:
        self.unloaded.append(model_identifier)


class _FakeClient:
    """Replays a scripted catalogue; records that close() was called."""

    def __init__(
        self,
        downloaded: list[_SDKDownloadedModel],
        loaded: list[_SDKLoadedModel],
        llm: _FakeLlmNamespace | None = None,
    ) -> None:
        self._downloaded = downloaded
        self._loaded = loaded
        self.closed = False
        self.llm = llm or _FakeLlmNamespace()

    def list_downloaded_models(self) -> list[_SDKDownloadedModel]:
        return list(self._downloaded)

    def list_loaded_models(self) -> list[_SDKLoadedModel]:
        return list(self._loaded)

    def close(self) -> None:
        self.closed = True


def _catalogue() -> list[_SDKDownloadedModel]:
    return [
        _FakeDownloaded(
            _FakeInfo(type="llm", model_key="qwen2.5-7b-instruct", max_context_length=32768)
        ),
        _FakeDownloaded(
            _FakeInfo(
                type="vlm",
                model_key="qwen2-vl-7b",
                format="Q5_K_M",
                architecture="qwen2_vl",
                max_context_length=8192,
                vision=True,
            )
        ),
        # An embedding model — MUST be filtered out.
        _FakeDownloaded(
            _FakeInfo(
                type="embedding",
                model_key="nomic-embed-text",
                architecture="nomic-bert",
                max_context_length=2048,
            )
        ),
    ]


# --- LMStudioManager unit tests ---------------------------------------------


def test_host_from_base_url_strips_scheme_and_path() -> None:
    # Inference URL (openai client) → bare host:port for the management SDK.
    assert host_from_base_url("http://192.168.86.21:1234/v1") == "192.168.86.21:1234"
    assert host_from_base_url("http://localhost:1234/v1/") == "localhost:1234"
    assert host_from_base_url("192.168.86.21:1234") == "192.168.86.21:1234"


def test_host_from_base_url_falls_back_when_absent() -> None:
    assert host_from_base_url(None) == DEFAULT_LM_STUDIO_HOST
    assert host_from_base_url("") == DEFAULT_LM_STUDIO_HOST


def test_host_from_base_url_canonicalises_loopback_aliases() -> None:
    # Every loopback spelling collapses to one key so the same local server
    # reached two ways doesn't get two managers (→ duplicate model loads).
    assert host_from_base_url("http://127.0.0.1:1234/v1") == "localhost:1234"
    assert host_from_base_url("http://[::1]:1234/v1") == "localhost:1234"
    assert host_from_base_url("http://0.0.0.0:1234") == "localhost:1234"
    assert host_from_base_url("http://127.0.1.1:1234") == "localhost:1234"
    # Host is lower-cased; a remote host is preserved verbatim (never assumed
    # equal to another distinct host).
    assert host_from_base_url("http://LocalHost:1234/v1") == "localhost:1234"
    assert host_from_base_url("http://192.168.86.21:1234/v1") == "192.168.86.21:1234"


def test_list_models_filters_embeddings_and_exposes_metadata() -> None:
    client = _FakeClient(_catalogue(), loaded=[_FakeLoaded("qwen2.5-7b-instruct")])

    def factory(_host: str) -> _SDKClient:
        return client

    manager = LMStudioManager(host="localhost:1234", client_factory=factory)

    models = manager.list_models()

    ids = [m.id for m in models]
    assert ids == ["qwen2.5-7b-instruct", "qwen2-vl-7b"]  # embedding excluded
    assert "nomic-embed-text" not in ids

    chat = models[0]
    assert chat.id == "qwen2.5-7b-instruct"
    assert chat.quantisation == "Q4_K_M"
    assert chat.architecture == "qwen2"
    assert chat.max_context_length == 32768
    assert chat.loaded is True  # present in list_loaded_models

    vlm = models[1]
    assert vlm.quantisation == "Q5_K_M"
    assert vlm.architecture == "qwen2_vl"
    assert vlm.max_context_length == 8192
    assert vlm.loaded is False  # not loaded

    assert client.closed is True  # manager closes the client


def test_list_models_unreachable_server_raises_distinct_error() -> None:
    def _boom(_host: str) -> _SDKClient:
        raise lmstudio.LMStudioWebsocketError("connection refused")

    manager = LMStudioManager(host="localhost:1234", client_factory=_boom)

    try:
        manager.list_models()
    except LMStudioUnavailableError as exc:
        assert "localhost:1234" in str(exc)
    else:  # pragma: no cover
        raise AssertionError("expected LMStudioUnavailableError")


def test_load_loads_target_and_keeps_previous_resident() -> None:
    # MIGRATED (issue 0107): v2 is multi-load — loading the target NO LONGER
    # evicts the previously-resident model. (Pre-0107 offload-first evicted
    # ``old-model`` before the load; that assertion is now the opposite.)
    llm = _FakeLlmNamespace()
    client = _FakeClient(
        _catalogue(),
        loaded=[_FakeLoaded("old-model")],
        llm=llm,
    )

    def factory(_host: str) -> _SDKClient:
        return client

    manager = LMStudioManager(host="localhost:1234", client_factory=factory)

    manager.load("qwen2.5-7b-instruct", context_length=8192)

    # Loaded the target with the default ctx folded into the SDK config.
    assert llm.loaded == [("qwen2.5-7b-instruct", {"contextLength": 8192})]
    # The previously-loaded peer is KEPT resident — no offload-first eviction.
    assert llm.unloaded == []
    assert client.closed is True


def test_load_already_loaded_plain_select_is_noop() -> None:
    # The target is already resident and this is a plain select (no reload):
    # keep it loaded, offload nothing, load nothing ("just select it").
    llm = _FakeLlmNamespace()
    client = _FakeClient(
        _catalogue(),
        loaded=[_FakeLoaded("qwen2.5-7b-instruct"), _FakeLoaded("other-model")],
        llm=llm,
    )

    manager = LMStudioManager(host="h", client_factory=lambda _h: client)
    manager.load("qwen2.5-7b-instruct")

    assert llm.loaded == []  # no reload
    assert llm.unloaded == []  # the resident target (and peers) left untouched


def test_load_already_loaded_with_reload_reloads_only_target() -> None:
    # MIGRATED (issue 0107): a forced reload (ctx Apply) evicts ONLY the target
    # then reloads it at the new window; OTHER residents stay loaded (pre-0107
    # offload-first freed every peer too — now it does not).
    llm = _FakeLlmNamespace()
    client = _FakeClient(
        _catalogue(),
        loaded=[_FakeLoaded("qwen2.5-7b-instruct"), _FakeLoaded("other-model")],
        llm=llm,
    )

    manager = LMStudioManager(host="h", client_factory=lambda _h: client)
    manager.load("qwen2.5-7b-instruct", context_length=4096, reload=True)

    assert llm.loaded == [("qwen2.5-7b-instruct", {"contextLength": 4096})]
    # Only the target was cycled; ``other-model`` is untouched (multi-load).
    assert llm.unloaded == ["qwen2.5-7b-instruct"]


def test_load_without_context_length_omits_config() -> None:
    llm = _FakeLlmNamespace()
    client = _FakeClient(_catalogue(), loaded=[], llm=llm)

    manager = LMStudioManager(host="h", client_factory=lambda _h: client)
    manager.load("qwen2.5-7b-instruct")

    assert llm.loaded == [("qwen2.5-7b-instruct", None)]
    assert llm.unloaded == []


def test_load_unknown_model_raises_not_found_and_keeps_previous() -> None:
    # MIGRATED (issue 0107): multi-load never offloads peers up front, so a
    # failed load leaves the previously-resident model untouched (pre-0107
    # offload-first had already evicted it).
    llm = _FakeLlmNamespace(load_error=lmstudio.LMStudioModelNotFoundError("no such model"))
    client = _FakeClient(_catalogue(), loaded=[_FakeLoaded("old-model")], llm=llm)

    manager = LMStudioManager(host="h", client_factory=lambda _h: client)

    try:
        manager.load("ghost-model")
    except LMStudioModelNotFoundError:
        pass
    else:  # pragma: no cover
        raise AssertionError("expected LMStudioModelNotFoundError")

    # No offload-first: the previous model is still resident after the failed load.
    assert llm.unloaded == []


def test_load_failure_raises_load_error_and_keeps_previous() -> None:
    # MIGRATED (issue 0107): an OOM at load leaves the previous resident in place
    # (multi-load never pre-offloads). Annexe G: keep previous state on real OOM.
    llm = _FakeLlmNamespace(load_error=lmstudio.LMStudioServerError("out of memory"))
    client = _FakeClient(_catalogue(), loaded=[_FakeLoaded("old-model")], llm=llm)

    manager = LMStudioManager(host="h", client_factory=lambda _h: client)

    try:
        manager.load("qwen2.5-7b-instruct")
    except LMStudioLoadError:
        pass
    else:  # pragma: no cover
        raise AssertionError("expected LMStudioLoadError")

    # No offload-first: previous kept on the failing load.
    assert llm.unloaded == []


def test_loaded_model_ids_returns_identifiers() -> None:
    client = _FakeClient(
        _catalogue(),
        loaded=[_FakeLoaded("a"), _FakeLoaded("b")],
    )

    manager = LMStudioManager(host="h", client_factory=lambda _h: client)
    assert manager.loaded_model_ids() == ["a", "b"]


# --- v2 multi-load + ref-counted offload + budget (issue 0107) ---------------
#
# These exercise the per-role ``assign_role`` / ``release_role`` / ``reconcile``
# surface and the budget refusal. The SDK fake records loads/unloads; the
# ref-count + budget assertions read the manager's in-memory state. A flat
# per-model footprint is injected so the budget arithmetic is exact.


def _fixed_footprint(gib: float) -> Callable[[str, int | None], float]:
    """A ``(model_id, ctx) -> gib`` footprint probe returning a constant."""

    def _probe(_model_id: str, _ctx: int | None) -> float:
        return gib

    return _probe


def _multiload_manager(
    *, ceiling_gib: float | None, footprint_gib: float = 4.0
) -> tuple[LMStudioManager, _FakeLlmNamespace]:
    """A v2 manager wired to a fresh stateful fake + a fixed per-model footprint."""

    llm = _FakeLlmNamespace()
    client = _FakeClient(_catalogue(), loaded=[], llm=llm)
    manager = LMStudioManager(
        host="localhost:1234",
        client_factory=lambda _h: client,
        budget=HostBudget(ceiling_gib=ceiling_gib),
        model_footprint=_fixed_footprint(footprint_gib),
    )
    return manager, llm


def test_is_local_host_classifies_loopback_vs_remote() -> None:
    assert is_local_host("localhost:1234") is True
    assert is_local_host("127.0.0.1:1234") is True
    assert is_local_host("192.168.86.21:1234") is False
    assert is_local_host("studio.lan:1234") is False


def test_assign_two_roles_two_models_both_resident_concurrency() -> None:
    # The headline concurrency invariant: two roles → two DISTINCT models, both
    # resident at once. Multi-load: assigning the second does NOT evict the first.
    manager, llm = _multiload_manager(ceiling_gib=100.0)

    manager.assign_role("jarvis", "modelA")
    manager.assign_role("thinker", "modelB")

    assert manager.resident_model_ids() == frozenset({"modelA", "modelB"})
    assert manager.model_for_role("jarvis") == "modelA"
    assert manager.model_for_role("thinker") == "modelB"
    # Both were loaded; nothing was evicted (no offload-first).
    assert [m for m, _ in llm.loaded] == ["modelA", "modelB"]
    assert llm.unloaded == []


def test_reselecting_loaded_model_for_another_role_does_not_offload() -> None:
    # Two roles pick the SAME model → one resident copy, ref-count 2, no reload,
    # no eviction. (Ref-count: re-selecting a loaded model keeps the peers.)
    manager, llm = _multiload_manager(ceiling_gib=100.0)

    manager.assign_role("jarvis", "modelA")
    manager.assign_role("thinker", "modelB")
    loaded_before = list(llm.loaded)

    manager.assign_role("draft", "modelA")  # already resident

    assert manager.ref_count("modelA") == 2
    assert manager.roles_for("modelA") == frozenset({"jarvis", "draft"})
    # No extra load issued for the re-selection, and modelB stays resident.
    assert llm.loaded == loaded_before
    assert llm.unloaded == []
    assert manager.resident_model_ids() == frozenset({"modelA", "modelB"})


def test_release_role_evicts_only_unreferenced_model() -> None:
    # modelA referenced by two roles; releasing ONE keeps it resident. Releasing
    # the LAST reference evicts it. modelB (single ref) evicts on its release.
    manager, llm = _multiload_manager(ceiling_gib=100.0)
    manager.assign_role("jarvis", "modelA")
    manager.assign_role("draft", "modelA")
    manager.assign_role("thinker", "modelB")

    manager.release_role("jarvis")  # modelA still held by draft
    assert manager.ref_count("modelA") == 1
    assert "modelA" in manager.resident_model_ids()
    assert llm.unloaded == []  # not evicted — still referenced

    manager.release_role("draft")  # last modelA ref
    assert "modelA" not in manager.resident_model_ids()
    assert llm.unloaded == ["modelA"]

    manager.release_role("thinker")  # only modelB ref
    assert manager.resident_model_ids() == frozenset()
    assert llm.unloaded == ["modelA", "modelB"]


def test_reassigning_role_releases_old_model_when_unreferenced() -> None:
    # Re-pointing a role to a new model evicts the OLD one iff no other role
    # holds it (selective ref-counted offload on reassignment, Annexe J step 6).
    manager, llm = _multiload_manager(ceiling_gib=100.0)
    manager.assign_role("jarvis", "modelA")

    manager.assign_role("jarvis", "modelB")  # repoint

    assert manager.model_for_role("jarvis") == "modelB"
    assert manager.resident_model_ids() == frozenset({"modelB"})
    assert llm.unloaded == ["modelA"]  # old model freed (no other ref)
    assert [m for m, _ in llm.loaded] == ["modelA", "modelB"]


def test_assign_role_adopts_server_loaded_model_without_reloading() -> None:
    # The in-process ref map starts empty at boot, but LM Studio may already have
    # JIT-loaded the model. assign_role must ADOPT the resident model (record the
    # ref) rather than issuing a duplicate load — the "model loaded twice" bug.
    llm = _FakeLlmNamespace()
    client = _FakeClient(
        _catalogue(),
        loaded=[_FakeLoaded("qwen2.5-7b-instruct")],  # already resident server-side
        llm=llm,
    )
    manager = LMStudioManager(host="localhost:1234", client_factory=lambda _h: client)

    manager.assign_role("jarvis", "qwen2.5-7b-instruct", context_length=8192)

    assert llm.loaded == []  # NO duplicate SDK load
    assert llm.unloaded == []
    assert manager.ref_count("qwen2.5-7b-instruct") == 1
    assert manager.model_for_role("jarvis") == "qwen2.5-7b-instruct"


def test_assign_role_loads_when_not_resident_anywhere() -> None:
    # The model is neither ref-counted nor loaded server-side → a real load fires.
    llm = _FakeLlmNamespace()
    client = _FakeClient(_catalogue(), loaded=[], llm=llm)
    manager = LMStudioManager(host="localhost:1234", client_factory=lambda _h: client)

    manager.assign_role("jarvis", "qwen2.5-7b-instruct")

    assert [m for m, _ in llm.loaded] == ["qwen2.5-7b-instruct"]
    assert manager.ref_count("qwen2.5-7b-instruct") == 1


def test_assign_role_refuses_third_model_over_budget() -> None:
    # Two 4 GiB models fit a 10 GiB ceiling; the third would make 12 > 10 → the
    # manager REFUSES before any load (Annexe G "Budget dépassé (check)").
    manager, llm = _multiload_manager(ceiling_gib=10.0, footprint_gib=4.0)
    manager.assign_role("jarvis", "modelA")
    manager.assign_role("thinker", "modelB")
    loaded_before = list(llm.loaded)

    try:
        manager.assign_role("draft", "modelC")
    except ModelBudgetExceededError as exc:
        assert "plafond" in str(exc)  # the "dépasse le plafond" refusal message
    else:  # pragma: no cover
        raise AssertionError("expected ModelBudgetExceededError")

    # Nothing loaded for the refused model; the two residents are untouched.
    assert llm.loaded == loaded_before
    assert "modelC" not in manager.resident_model_ids()
    assert manager.model_for_role("draft") is None
    assert manager.resident_model_ids() == frozenset({"modelA", "modelB"})


def test_assign_role_no_budget_never_refuses() -> None:
    # ``ceiling_gib=None`` (remote host, no override) → the check is skipped and
    # any number of models load (the real-load try+catch is the only OOM net).
    manager, _llm = _multiload_manager(ceiling_gib=None, footprint_gib=50.0)
    manager.assign_role("jarvis", "modelA")
    manager.assign_role("thinker", "modelB")
    manager.assign_role("draft", "modelC")
    assert manager.resident_model_ids() == frozenset({"modelA", "modelB", "modelC"})


def test_assign_role_oom_despite_budget_keeps_previous_state() -> None:
    # Annexe G "OOM au load (budget OK mais réel KO)": the budget check passes
    # but the SDK load OOMs. The previous role's model stays resident, the new
    # role gains NO model, and the error propagates (never leaves 0 models for an
    # already-active role).
    llm = _FakeLlmNamespace(fail_models={"modelB"})
    client = _FakeClient(_catalogue(), loaded=[], llm=llm)
    manager = LMStudioManager(
        host="localhost:1234",
        client_factory=lambda _h: client,
        budget=HostBudget(ceiling_gib=100.0),
        model_footprint=_fixed_footprint(4.0),
    )
    manager.assign_role("jarvis", "modelA")  # succeeds

    try:
        manager.assign_role("thinker", "modelB")  # budget OK, SDK OOM
    except LMStudioLoadError:
        pass
    else:  # pragma: no cover
        raise AssertionError("expected LMStudioLoadError")

    # jarvis still has its model; thinker gained nothing; modelB not resident.
    assert manager.model_for_role("jarvis") == "modelA"
    assert manager.model_for_role("thinker") is None
    assert manager.resident_model_ids() == frozenset({"modelA"})
    assert manager.ref_count("modelB") == 0


def test_reconcile_boot_marks_ready_and_offline_per_role() -> None:
    # Annexe J boot: group is already per-host. Two roles load (ready); a third
    # that breaks the budget is marked offline with a reason; a claude_cli role
    # (no model) is ready without a load. One bad role never aborts the peers.
    manager, llm = _multiload_manager(ceiling_gib=10.0, footprint_gib=4.0)

    results = manager.reconcile(
        {
            "jarvis": ("modelA", 16384),
            "thinker": ("modelB", None),
            "draft": ("modelC", None),  # 12 > 10 → refused
            "subagent": (None, None),  # claude_cli — no load
        }
    )

    by_role = {r.role: r for r in results}
    assert by_role["jarvis"].ready is True
    assert by_role["thinker"].ready is True
    assert by_role["draft"].ready is False
    assert "plafond" in by_role["draft"].detail
    assert by_role["subagent"].ready is True and by_role["subagent"].model_id is None
    # The two that fit are resident; the refused one is not.
    assert manager.resident_model_ids() == frozenset({"modelA", "modelB"})
    assert [m for m, _ in llm.loaded] == ["modelA", "modelB"]


def test_reconcile_marks_role_offline_when_host_unreachable() -> None:
    # Annexe G "Host distant injoignable": the SDK client cannot connect → the
    # role is marked offline (not crashed), reason carried through.
    def _boom(_host: str) -> _SDKClient:
        raise lmstudio.LMStudioWebsocketError("connection refused")

    manager = LMStudioManager(
        host="studio.lan:1234",
        client_factory=_boom,
        budget=None,
        model_footprint=_fixed_footprint(4.0),
    )

    results = manager.reconcile({"jarvis": ("modelA", None)})

    assert results[0].ready is False
    assert "studio.lan:1234" in results[0].detail
    assert manager.resident_model_ids() == frozenset()


def test_legacy_load_refuses_over_budget_against_sdk_residents() -> None:
    # The legacy single-selection ``load`` (used by LLMSwitcher) is also
    # budget-aware: a target that would exceed the ceiling alongside the SDK's
    # already-resident models is refused before the load.
    llm = _FakeLlmNamespace()
    client = _FakeClient(
        _catalogue(),
        loaded=[_FakeLoaded("resident-1"), _FakeLoaded("resident-2")],
        llm=llm,
    )
    manager = LMStudioManager(
        host="localhost:1234",
        client_factory=lambda _h: client,
        budget=HostBudget(ceiling_gib=10.0),
        model_footprint=_fixed_footprint(4.0),
    )

    try:
        manager.load("qwen2.5-7b-instruct")  # 4+4+4 = 12 > 10
    except ModelBudgetExceededError:
        pass
    else:  # pragma: no cover
        raise AssertionError("expected ModelBudgetExceededError")

    assert llm.loaded == []  # refused before the SDK load
    assert llm.unloaded == []


# --- GET /api/llm/models endpoint tests -------------------------------------


def test_get_models_endpoint_returns_live_list() -> None:
    from bob import llm_router

    client = _FakeClient(_catalogue(), loaded=[_FakeLoaded("qwen2.5-7b-instruct")])

    def factory(_host: str) -> _SDKClient:
        return client

    manager = LMStudioManager(host="localhost:1234", client_factory=factory)

    llm_router.set_manager_provider(lambda: manager)
    try:
        api = TestClient(app)
        response = api.get("/api/llm/models")
    finally:
        llm_router.reset_manager_provider()

    assert response.status_code == 200
    body = response.json()
    ids = [m["id"] for m in body["models"]]
    assert ids == ["qwen2.5-7b-instruct", "qwen2-vl-7b"]
    assert "nomic-embed-text" not in ids
    first = body["models"][0]
    assert first == {
        "id": "qwen2.5-7b-instruct",
        "quantisation": "Q4_K_M",
        "architecture": "qwen2",
        "max_context_length": 32768,
        "loaded": True,
    }


def test_get_models_endpoint_server_down_returns_503() -> None:
    from bob import llm_router

    def _boom(_host: str) -> _SDKClient:
        raise lmstudio.LMStudioWebsocketError("connection refused")

    manager = LMStudioManager(host="localhost:1234", client_factory=_boom)

    llm_router.set_manager_provider(lambda: manager)
    try:
        api = TestClient(app)
        response = api.get("/api/llm/models")
    finally:
        llm_router.reset_manager_provider()

    assert response.status_code == 503
    body = response.json()
    assert body["error"] == "lm_studio_unavailable"
    assert "localhost:1234" in body["detail"]


# --- GET /api/llm/ping endpoint tests ---------------------------------------


def test_ping_endpoint_reachable_server_returns_true() -> None:
    from bob import llm_router

    client = _FakeClient(_catalogue(), loaded=[_FakeLoaded("qwen2.5-7b-instruct")])
    manager = LMStudioManager(host="localhost:1234", client_factory=lambda _h: client)

    llm_router.set_manager_provider(lambda: manager)
    try:
        api = TestClient(app)
        response = api.get("/api/llm/ping")
    finally:
        llm_router.reset_manager_provider()

    assert response.status_code == 200
    assert response.json() == {"reachable": True, "host": "localhost:1234"}


def test_ping_endpoint_unreachable_server_returns_false_not_error() -> None:
    from bob import llm_router

    def _boom(_host: str) -> _SDKClient:
        raise lmstudio.LMStudioWebsocketError("connection refused")

    manager = LMStudioManager(host="localhost:1234", client_factory=_boom)

    llm_router.set_manager_provider(lambda: manager)
    try:
        api = TestClient(app)
        response = api.get("/api/llm/ping")
    finally:
        llm_router.reset_manager_provider()

    # Always 200 — the picker reads `reachable`, never an error status.
    assert response.status_code == 200
    assert response.json() == {"reachable": False, "host": "localhost:1234"}


# --- PUT /api/llm/selection endpoint tests ----------------------------------
#
# The route delegates to a :class:`bob.llm_swap.LLMSwitcher`. We inject a fake
# switcher through the router DI seam so the test exercises the route's HTTP
# contract (status mapping, body shape) without the orchestrator / SDK. The
# swap coordinator's own behaviour is covered in ``test_llm_swap.py``.


class _FakeSwitcher:
    """Stand-in for :class:`LLMSwitcher` — returns a result or raises."""

    def __init__(self, *, result: object = None, error: Exception | None = None) -> None:
        self._result = result
        self._error = error
        self.calls: list[tuple[str, int | None]] = []

    async def swap_lm_model(self, model_id: str, context_length: int | None = None) -> object:
        self.calls.append((model_id, context_length))
        if self._error is not None:
            raise self._error
        return self._result


def test_put_selection_success_returns_new_selection() -> None:
    from bob import llm_router
    from bob.llm_selection_store import LLMSelection
    from bob.llm_swap import SwapResult

    selection = LLMSelection(
        provider="lm_studio",
        lm_model="target-model",
        context_length={"target-model": 8192},
    )
    switcher = _FakeSwitcher(result=SwapResult(selection=selection))

    llm_router.set_switcher(cast(Any, switcher))
    llm_router.set_settings_provider(lambda: _settings(CLAUDE_CLI_MODEL="claude-opus-4"))
    try:
        api = TestClient(app)
        response = api.put("/api/llm/selection", json={"lm_model": "target-model"})
    finally:
        llm_router.set_switcher(None)
        llm_router.reset_settings_provider()

    assert response.status_code == 200
    assert switcher.calls == [("target-model", None)]
    body = response.json()
    # ``claude_model`` (issue 0081) is now part of the response shape; the
    # model-swap fields are unchanged.
    assert body == {
        "provider": "lm_studio",
        "lm_model": "target-model",
        "context_length": {"target-model": 8192},
        "claude_model": "claude-opus-4",
        # No pinned base_url on the swap result → falls back to LLM_BASE_URL.
        "base_url": "http://localhost:1234/v1",
    }


def test_put_selection_not_found_maps_to_404() -> None:
    from bob import llm_router

    switcher = _FakeSwitcher(error=LMStudioModelNotFoundError("ghost"))
    llm_router.set_switcher(cast(Any, switcher))
    try:
        api = TestClient(app)
        response = api.put("/api/llm/selection", json={"lm_model": "ghost"})
    finally:
        llm_router.set_switcher(None)

    assert response.status_code == 404
    assert response.json()["error"] == "model_not_found"


def test_put_selection_load_failure_maps_to_409() -> None:
    from bob import llm_router

    switcher = _FakeSwitcher(error=LMStudioLoadError("out of memory"))
    llm_router.set_switcher(cast(Any, switcher))
    try:
        api = TestClient(app)
        response = api.put("/api/llm/selection", json={"lm_model": "big-model"})
    finally:
        llm_router.set_switcher(None)

    assert response.status_code == 409
    assert response.json()["error"] == "load_failed"


def test_put_selection_unreachable_maps_to_503() -> None:
    from bob import llm_router

    switcher = _FakeSwitcher(error=LMStudioUnavailableError("server down"))
    llm_router.set_switcher(cast(Any, switcher))
    try:
        api = TestClient(app)
        response = api.put("/api/llm/selection", json={"lm_model": "m"})
    finally:
        llm_router.set_switcher(None)

    assert response.status_code == 503
    assert response.json()["error"] == "lm_studio_unavailable"


def test_put_selection_no_switcher_returns_503() -> None:
    api = TestClient(app)
    response = api.put("/api/llm/selection", json={"lm_model": "m"})
    assert response.status_code == 503
    assert response.json()["error"] == "swap_unavailable"
