"""Tests for the per-role selection store (PRD 0016 / issue 0106, Annexe D).

Covers the v2 ``RoleSelectionStore`` contract: a four-role round-trip, the
1->2 migration (a flat v1 file seeds all four roles + stt/budget defaults),
first-boot seeding from ``.env``, and the defensive decode (corrupt / partial /
wrong-typed file collapses to defaults, ``ceiling_gib:null`` stays "detect
later").
"""

from __future__ import annotations

import json
from pathlib import Path

from bob.config import Settings
from bob.llm_selection_store import (
    DEFAULT_BUDGET_RESERVE_GIB,
    DEFAULT_STT_ENGINE,
    DEFAULT_STT_MODEL,
    LLM_SELECTION_FILENAME,
    ROLES,
    BudgetSelection,
    LLMSelection,
    RoleSelection,
    RoleSelectionStore,
    SttSelection,
)


def _settings(**overrides: object) -> Settings:
    base: dict[str, object] = {
        "LLM_PROVIDER": "lm_studio",
        "LLM_BASE_URL": "http://localhost:1234/v1",
        "LLM_MODEL": "qwen2.5-7b-instruct",
        "LLM_API_KEY": "lm-studio",
    }
    base.update(overrides)
    return Settings(**base)  # type: ignore[arg-type]


def _full_selection() -> RoleSelection:
    return RoleSelection(
        roles={
            "jarvis": LLMSelection(
                provider="lm_studio",
                lm_model="qwen2.5-7b-instruct",
                context_length={"qwen2.5-7b-instruct": 16384},
                base_url="http://localhost:1234/v1",
            ),
            "thinker": LLMSelection(
                provider="lm_studio",
                lm_model="qwen2.5-3b-instruct",
                context_length={},
                base_url="http://localhost:1234/v1",
            ),
            "draft": LLMSelection(
                provider="lm_studio",
                lm_model="qwen2.5-1.5b-instruct",
                context_length={},
                base_url="http://localhost:1234/v1",
            ),
            "subagent": LLMSelection(
                provider="claude_cli",
                lm_model=None,
                context_length={},
                base_url=None,
            ),
        },
        stt=SttSelection(engine="whisper_cpp", model="large-v3-turbo"),
        budget=BudgetSelection(ceiling_gib=None, reserve_gib=8.0, per_host_override={}),
    )


def test_v2_round_trips(tmp_path: Path) -> None:
    path = tmp_path / LLM_SELECTION_FILENAME
    store = RoleSelectionStore(path)
    store.write(_full_selection())

    reloaded = RoleSelectionStore(path).read()

    assert reloaded is not None
    assert reloaded.schema_version == 2
    assert set(reloaded.roles) == set(ROLES)
    assert reloaded.role("jarvis").provider == "lm_studio"
    assert reloaded.role("jarvis").lm_model == "qwen2.5-7b-instruct"
    assert reloaded.role("jarvis").context_length == {"qwen2.5-7b-instruct": 16384}
    assert reloaded.role("subagent").provider == "claude_cli"
    assert reloaded.role("subagent").base_url is None
    assert reloaded.stt == SttSelection(engine="whisper_cpp", model="large-v3-turbo")
    assert reloaded.budget.ceiling_gib is None
    assert reloaded.budget.reserve_gib == 8.0


def test_on_disk_shape_matches_annexe_d(tmp_path: Path) -> None:
    path = tmp_path / LLM_SELECTION_FILENAME
    RoleSelectionStore(path).write(_full_selection())

    on_disk = json.loads(path.read_text(encoding="utf-8"))

    assert on_disk["schema_version"] == 2
    assert set(on_disk["roles"]) == set(ROLES)
    assert on_disk["roles"]["subagent"] == {
        "provider": "claude_cli",
        "lm_model": None,
        "context_length": {},
        "base_url": None,
    }
    assert on_disk["stt"] == {"engine": "whisper_cpp", "model": "large-v3-turbo"}
    assert on_disk["budget"] == {
        "ceiling_gib": None,
        "reserve_gib": 8.0,
        "per_host_override": {},
    }


def test_migration_v1_flat_seeds_all_four_roles(tmp_path: Path) -> None:
    """An old flat v1 file seeds the four roles identically + stt/budget defaults."""

    path = tmp_path / LLM_SELECTION_FILENAME
    # The exact flat v1 shape written by the pre-0106 LLMSelectionStore.
    path.write_text(
        json.dumps(
            {
                "provider": "lm_studio",
                "lm_model": "legacy-model",
                "context_length": {"legacy-model": 8192},
                "base_url": "http://192.168.1.20:1234/v1",
            }
        ),
        encoding="utf-8",
    )

    migrated = RoleSelectionStore(path).read()

    assert migrated is not None
    assert migrated.schema_version == 2
    # All four roles seeded identically from the flat selection.
    for role in ROLES:
        sel = migrated.role(role)
        assert sel.provider == "lm_studio"
        assert sel.lm_model == "legacy-model"
        assert sel.context_length == {"legacy-model": 8192}
        assert sel.base_url == "http://192.168.1.20:1234/v1"
    # stt / budget take defaults.
    assert migrated.stt == SttSelection(engine=DEFAULT_STT_ENGINE, model=DEFAULT_STT_MODEL)
    assert migrated.budget.ceiling_gib is None
    assert migrated.budget.reserve_gib == DEFAULT_BUDGET_RESERVE_GIB
    assert migrated.budget.per_host_override == {}


def test_migration_rewrites_file_in_v2_shape(tmp_path: Path) -> None:
    """seed_from_settings on a flat v1 file persists the migrated v2 shape."""

    path = tmp_path / LLM_SELECTION_FILENAME
    path.write_text(
        json.dumps({"provider": "claude_cli", "lm_model": None, "context_length": {}}),
        encoding="utf-8",
    )
    store = RoleSelectionStore(path)

    seeded = store.seed_from_settings(_settings())

    # The flat v1 file wins over .env (mirrors v1 store semantics) and is fanned
    # out across the four roles.
    assert seeded.role("jarvis").provider == "claude_cli"
    # The file on disk is now canonical v2.
    on_disk = json.loads(path.read_text(encoding="utf-8"))
    assert on_disk["schema_version"] == 2
    assert set(on_disk["roles"]) == set(ROLES)


def test_first_boot_seeds_from_settings_and_persists(tmp_path: Path) -> None:
    path = tmp_path / LLM_SELECTION_FILENAME
    store = RoleSelectionStore(path)
    assert store.read() is None  # nothing persisted yet

    seeded = store.seed_from_settings(_settings())

    # .env selection fanned across all four roles.
    for role in ROLES:
        assert seeded.role(role).provider == "lm_studio"
        assert seeded.role(role).lm_model == "qwen2.5-7b-instruct"
        assert seeded.role(role).base_url == "http://localhost:1234/v1"
    assert path.exists()
    assert json.loads(path.read_text(encoding="utf-8"))["schema_version"] == 2


def test_read_decodes_corrupt_file_to_defaults(tmp_path: Path) -> None:
    path = tmp_path / LLM_SELECTION_FILENAME
    path.write_text("{ not valid json", encoding="utf-8")

    selection = RoleSelectionStore(path).read()

    assert selection is not None
    assert set(selection.roles) == set(ROLES)
    for role in ROLES:
        assert selection.role(role).provider == "lm_studio"
        assert selection.role(role).lm_model is None
        assert selection.role(role).context_length == {}
    assert selection.stt == SttSelection()
    assert selection.budget == BudgetSelection()


def test_read_decodes_partial_v2_file_to_defaults(tmp_path: Path) -> None:
    """A v2 file missing roles / mistyped blocks collapses key-by-key to defaults."""

    path = tmp_path / LLM_SELECTION_FILENAME
    path.write_text(
        json.dumps(
            {
                "schema_version": 2,
                "roles": {
                    "jarvis": {"provider": "lm_studio", "lm_model": "only-jarvis"},
                    # thinker / draft / subagent missing entirely
                    "draft": "not-a-dict",  # wrong type
                },
                "stt": {"engine": 123},  # wrong type -> default
                "budget": {"ceiling_gib": 24, "reserve_gib": "nope"},
            }
        ),
        encoding="utf-8",
    )

    selection = RoleSelectionStore(path).read()

    assert selection is not None
    # Present, well-typed role kept.
    assert selection.role("jarvis").lm_model == "only-jarvis"
    # Missing / mistyped roles default to lm_studio / unpinned.
    for role in ("thinker", "draft", "subagent"):
        assert selection.role(role).provider == "lm_studio"
        assert selection.role(role).lm_model is None
    # stt engine mistyped -> default; budget ceiling kept (numeric), reserve defaulted.
    assert selection.stt.engine == DEFAULT_STT_ENGINE
    assert selection.budget.ceiling_gib == 24.0
    assert selection.budget.reserve_gib == DEFAULT_BUDGET_RESERVE_GIB


def test_budget_ceiling_null_stays_none_detect_later(tmp_path: Path) -> None:
    path = tmp_path / LLM_SELECTION_FILENAME
    path.write_text(
        json.dumps(
            {
                "schema_version": 2,
                "roles": {role: {"provider": "lm_studio"} for role in ROLES},
                "budget": {"ceiling_gib": None, "reserve_gib": 6, "per_host_override": {"h": 12}},
            }
        ),
        encoding="utf-8",
    )

    selection = RoleSelectionStore(path).read()

    assert selection is not None
    assert selection.budget.ceiling_gib is None  # detect later
    assert selection.budget.reserve_gib == 6.0
    assert selection.budget.per_host_override == {"h": 12.0}


def test_with_role_replaces_one_role_only() -> None:
    base = _full_selection()
    replacement = LLMSelection(provider="claude_cli", lm_model=None, context_length={})

    updated = base.with_role("jarvis", replacement)

    assert updated.role("jarvis").provider == "claude_cli"
    # Other roles unchanged (same object identity from the source map).
    for role in ("thinker", "draft", "subagent"):
        assert updated.role(role) is base.role(role)
    # stt / budget preserved.
    assert updated.stt == base.stt
    assert updated.budget == base.budget
