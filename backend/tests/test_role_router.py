"""HTTP-level tests for the per-role selection endpoints (PRD 0016 / issue 0106).

``GET /api/llm/roles`` returns the full role map; ``PUT /api/llm/roles/{role}``
swaps one role. The PUT route is a thin shell over
:class:`bob.llm_swap.RoleLLMSwitcher`; we inject a FAKE switcher through the
router's ``set_role_switcher`` seam so the test never rebuilds a real client. We
assert the route's contract: GET shape, dispatch + body decode, and the
validation → HTTP-status mapping.
"""

from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from bob import llm_router
from bob.config import Settings
from bob.llm_selection_store import (
    ROLES,
    LLMSelection,
    RoleSelection,
    RoleSelectionStore,
)
from bob.main import app


def _settings(**overrides: object) -> Settings:
    base: dict[str, object] = {
        "LLM_PROVIDER": "lm_studio",
        "LLM_BASE_URL": "http://localhost:1234/v1",
        "LLM_MODEL": "qwen2.5-7b-instruct",
        "LLM_API_KEY": "lm-studio",
        "CLAUDE_CLI_MODEL": "claude-opus-4",
    }
    base.update(overrides)
    return Settings(**base)  # type: ignore[arg-type]


class _FakeRoleSwitcher:
    """Records swap_role calls; returns a scripted resulting map."""

    def __init__(self, result: RoleSelection) -> None:
        self._result = result
        self.calls: list[tuple[str, LLMSelection]] = []

    async def swap_role(self, role: str, selection: LLMSelection) -> RoleSelection:
        self.calls.append((role, selection))
        return self._result


def _seeded_store(tmp_path: Path) -> RoleSelectionStore:
    store = RoleSelectionStore(tmp_path / "llm_selection.json")
    store.write(
        RoleSelection(
            roles={
                "jarvis": LLMSelection(
                    provider="lm_studio",
                    lm_model="modelA",
                    context_length={"modelA": 16384},
                    base_url="http://localhost:1234/v1",
                ),
                "thinker": LLMSelection(provider="lm_studio", lm_model="t", context_length={}),
                "draft": LLMSelection(provider="lm_studio", lm_model="d", context_length={}),
                "subagent": LLMSelection(provider="claude_cli", lm_model=None, context_length={}),
            }
        )
    )
    return store


def test_get_roles_returns_full_map(tmp_path: Path) -> None:
    store = _seeded_store(tmp_path)
    llm_router.set_role_store_provider(lambda: store)
    llm_router.set_settings_provider(lambda: _settings())
    try:
        client = TestClient(app)
        response = client.get("/api/llm/roles")
    finally:
        llm_router.reset_role_store_provider()
        llm_router.reset_settings_provider()

    assert response.status_code == 200
    body = response.json()
    assert body["schema_version"] == 2
    assert set(body["roles"]) == set(ROLES)
    assert body["roles"]["jarvis"] == {
        "provider": "lm_studio",
        "lm_model": "modelA",
        "context_length": {"modelA": 16384},
        "base_url": "http://localhost:1234/v1",
    }
    assert body["roles"]["subagent"]["provider"] == "claude_cli"
    assert body["stt"] == {"engine": "whisper_cpp", "model": "large-v3-turbo"}
    assert body["budget"]["reserve_gib"] == 8.0
    assert body["budget"]["ceiling_gib"] is None
    assert body["claude_model"] == "claude-opus-4"


def test_put_role_dispatches_and_returns_updated_map(tmp_path: Path) -> None:
    result = RoleSelection(
        roles={
            "jarvis": LLMSelection(provider="claude_cli", lm_model=None, context_length={}),
            "thinker": LLMSelection(provider="lm_studio", lm_model="t", context_length={}),
            "draft": LLMSelection(provider="lm_studio", lm_model="d", context_length={}),
            "subagent": LLMSelection(provider="claude_cli", lm_model=None, context_length={}),
        }
    )
    switcher = _FakeRoleSwitcher(result)
    llm_router.set_role_switcher(switcher)  # type: ignore[arg-type]
    llm_router.set_settings_provider(lambda: _settings())
    try:
        client = TestClient(app)
        response = client.put(
            "/api/llm/roles/jarvis",
            json={"provider": "claude_cli", "lm_model": None, "context_length": {}},
        )
    finally:
        llm_router.set_role_switcher(None)
        llm_router.reset_settings_provider()

    assert response.status_code == 200
    assert len(switcher.calls) == 1
    role, sel = switcher.calls[0]
    assert role == "jarvis"
    assert sel.provider == "claude_cli"
    body = response.json()
    assert body["roles"]["jarvis"]["provider"] == "claude_cli"


def test_put_lm_studio_role_threads_model_and_base_url(tmp_path: Path) -> None:
    result = RoleSelection(
        roles={
            role: LLMSelection(provider="lm_studio", lm_model="x", context_length={})
            for role in ROLES
        }
    )
    switcher = _FakeRoleSwitcher(result)
    llm_router.set_role_switcher(switcher)  # type: ignore[arg-type]
    llm_router.set_settings_provider(lambda: _settings())
    try:
        client = TestClient(app)
        response = client.put(
            "/api/llm/roles/thinker",
            json={
                "provider": "lm_studio",
                "lm_model": "modelB",
                "base_url": "http://host-b:9999/v1",
                "context_length": {"modelB": 8192},
            },
        )
    finally:
        llm_router.set_role_switcher(None)
        llm_router.reset_settings_provider()

    assert response.status_code == 200
    _role, sel = switcher.calls[0]
    assert sel.lm_model == "modelB"
    assert sel.base_url == "http://host-b:9999/v1"
    assert sel.context_length == {"modelB": 8192}


def test_put_unknown_role_maps_to_404() -> None:
    switcher = _FakeRoleSwitcher(
        RoleSelection(roles={r: LLMSelection(provider="lm_studio", lm_model=None) for r in ROLES})
    )
    llm_router.set_role_switcher(switcher)  # type: ignore[arg-type]
    llm_router.set_settings_provider(lambda: _settings())
    try:
        client = TestClient(app)
        response = client.put("/api/llm/roles/speaker", json={"provider": "lm_studio"})
    finally:
        llm_router.set_role_switcher(None)
        llm_router.reset_settings_provider()

    assert response.status_code == 404
    assert response.json()["error"] == "unknown_role"
    assert switcher.calls == []  # never dispatched


def test_put_role_503_when_switcher_not_wired() -> None:
    llm_router.set_role_switcher(None)
    llm_router.set_settings_provider(lambda: _settings())
    try:
        client = TestClient(app)
        response = client.put("/api/llm/roles/jarvis", json={"provider": "lm_studio"})
    finally:
        llm_router.reset_settings_provider()

    assert response.status_code == 503
    assert response.json()["error"] == "swap_unavailable"


def test_put_invalid_provider_maps_to_422() -> None:
    """The switcher raises UnknownProviderError for a bad provider → 422."""

    from bob.llm_swap import UnknownProviderError

    class _RaisingSwitcher:
        def __init__(self) -> None:
            self.calls: list[tuple[str, LLMSelection]] = []

        async def swap_role(self, role: str, selection: LLMSelection) -> RoleSelection:
            self.calls.append((role, selection))
            raise UnknownProviderError("Unknown LLM provider: 'gpt5'")

    switcher = _RaisingSwitcher()
    llm_router.set_role_switcher(switcher)  # type: ignore[arg-type]
    llm_router.set_settings_provider(lambda: _settings())
    try:
        client = TestClient(app)
        response = client.put("/api/llm/roles/jarvis", json={"provider": "gpt5"})
    finally:
        llm_router.set_role_switcher(None)
        llm_router.reset_settings_provider()

    assert response.status_code == 422
    assert response.json()["error"] == "unknown_provider"
