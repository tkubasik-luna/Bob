"""Unit tests for the pure model-memory budget core (PRD 0016 / issue 0107).

:mod:`bob.model_budget` is a PURE module — footprint estimation, per-host
ceiling resolution and the fit-check take their RAM / file-size inputs as
arguments (or through injected probes), so every case here is exact,
deterministic and offline. The real ``sysctl`` / ``os.stat`` probes are smoke
-tested only for "never raises", since their values are machine-dependent.
"""

from __future__ import annotations

import pytest

from bob.model_budget import (
    DEFAULT_KV_MARGIN_GIB,
    DEFAULT_RESERVE_GIB,
    KV_GIB_PER_1K_TOKENS,
    BudgetDecision,
    HostBudget,
    ResidentModel,
    build_host_budget,
    build_model_footprint_probe,
    detect_local_ram_gib,
    file_size_gib,
    fits,
    footprint_gib,
    host_ceiling_gib,
    kv_cache_margin_gib,
    local_ceiling_gib,
    remote_ceiling_gib,
)

# --- footprint: disk weights + KV margin proportional to context -------------


def test_kv_cache_margin_scales_with_context_length() -> None:
    # Proportional to the window: KV_GIB_PER_1K_TOKENS per 1024 tokens.
    assert kv_cache_margin_gib(1024) == pytest.approx(KV_GIB_PER_1K_TOKENS)
    assert kv_cache_margin_gib(8192) == pytest.approx(KV_GIB_PER_1K_TOKENS * 8)
    # Bigger window → strictly bigger margin.
    assert kv_cache_margin_gib(32768) > kv_cache_margin_gib(8192)


def test_kv_cache_margin_unknown_context_uses_flat_fallback() -> None:
    # None / non-positive context → flat non-zero fallback (never weights-only).
    assert kv_cache_margin_gib(None) == DEFAULT_KV_MARGIN_GIB
    assert kv_cache_margin_gib(0) == DEFAULT_KV_MARGIN_GIB
    assert kv_cache_margin_gib(-5) == DEFAULT_KV_MARGIN_GIB


def test_footprint_is_weights_plus_kv_margin() -> None:
    # 4 GiB GGUF + 8K-token KV margin.
    expected = 4.0 + KV_GIB_PER_1K_TOKENS * 8
    assert footprint_gib(4.0, 8192) == pytest.approx(expected)
    # Same weights, bigger window → bigger footprint.
    assert footprint_gib(4.0, 32768) > footprint_gib(4.0, 8192)
    # Unknown context still books the flat margin on top of the weights.
    assert footprint_gib(4.0, None) == pytest.approx(4.0 + DEFAULT_KV_MARGIN_GIB)


def test_footprint_custom_kv_rate_is_injectable() -> None:
    # The KV rate is a tunable; a test pins it exactly.
    assert footprint_gib(2.0, 2048, gib_per_1k_tokens=0.5) == pytest.approx(2.0 + 1.0)


# --- local / remote ceiling --------------------------------------------------


def test_local_ceiling_is_detected_ram_minus_reserve() -> None:
    assert local_ceiling_gib(32.0, reserve_gib=8.0) == pytest.approx(24.0)
    # Default reserve.
    assert local_ceiling_gib(64.0) == pytest.approx(64.0 - DEFAULT_RESERVE_GIB)


def test_local_ceiling_clamps_at_zero_when_reserve_exceeds_ram() -> None:
    # A reserve larger than RAM → 0 ceiling (everything refuses), never negative.
    assert local_ceiling_gib(4.0, reserve_gib=8.0) == 0.0


def test_remote_ceiling_uses_override_when_present() -> None:
    assert remote_ceiling_gib("studio.lan:1234", {"studio.lan:1234": 48.0}) == 48.0


def test_remote_ceiling_none_when_no_override_skip_check() -> None:
    # No override for a remote host → None ("no readable ceiling → skip").
    assert remote_ceiling_gib("studio.lan:1234", {}) is None
    assert remote_ceiling_gib("studio.lan:1234", {"other:1234": 16.0}) is None


def test_remote_ceiling_tolerates_base_url_shaped_override_key() -> None:
    # A hand-edited override keyed by a full base URL still matches the host.
    override = {"http://studio.lan:1234/v1": 40.0}
    assert remote_ceiling_gib("studio.lan:1234", override) == 40.0


def test_host_ceiling_dispatch_local_remote_override() -> None:
    # Local with detected RAM → detected - reserve.
    assert host_ceiling_gib(
        "localhost:1234",
        is_local=True,
        detected_ram_gib=32.0,
        reserve_gib=8.0,
        per_host_override={},
    ) == pytest.approx(24.0)
    # An override wins even for a local host (operator pins it).
    assert (
        host_ceiling_gib(
            "localhost:1234",
            is_local=True,
            detected_ram_gib=32.0,
            reserve_gib=8.0,
            per_host_override={"localhost:1234": 12.0},
        )
        == 12.0
    )
    # Remote with no override → None (skip).
    assert (
        host_ceiling_gib(
            "studio.lan:1234",
            is_local=False,
            detected_ram_gib=None,
            reserve_gib=8.0,
            per_host_override={},
        )
        is None
    )
    # Local but RAM detection failed (None) + no override → None.
    assert (
        host_ceiling_gib(
            "localhost:1234",
            is_local=True,
            detected_ram_gib=None,
            reserve_gib=8.0,
            per_host_override={},
        )
        is None
    )


# --- fit-check ---------------------------------------------------------------


def test_fits_sum_under_ceiling() -> None:
    assert fits([4.0, 4.0], 10.0) is True
    assert fits([4.0, 4.0, 4.0], 10.0) is False  # 12 > 10
    # Exactly at the ceiling fits (<=).
    assert fits([5.0, 5.0], 10.0) is True


def test_fits_none_ceiling_always_fits() -> None:
    # Remote host with no override → no readable limit → always fits.
    assert fits([100.0, 100.0], None) is True
    assert fits([], None) is True


# --- HostBudget: ref-counted resident tracker --------------------------------


def test_host_budget_check_add_under_and_over_ceiling() -> None:
    budget = HostBudget(ceiling_gib=10.0)
    budget.add("modelA", 4.0)

    fit = budget.check_add("modelB", 4.0)  # 4 + 4 = 8 <= 10
    assert fit.ok is True
    assert fit.required_gib == pytest.approx(8.0)

    over = budget.check_add("modelC", 7.0)  # 4 + 7 = 11 > 10
    assert over.ok is False
    assert over.required_gib == pytest.approx(11.0)
    assert "plafond" in over.message()


def test_host_budget_reselecting_resident_model_always_fits() -> None:
    # A model already counted resident re-fits at candidate 0 (ref-count case).
    budget = HostBudget(ceiling_gib=8.0)
    budget.add("modelA", 8.0)  # ceiling fully consumed
    decision = budget.check_add("modelA", 8.0)
    assert decision.ok is True
    assert decision.candidate_gib == 0.0


def test_host_budget_none_ceiling_check_always_ok() -> None:
    budget = HostBudget(ceiling_gib=None)
    budget.add("modelA", 100.0)
    assert budget.check_add("modelB", 100.0).ok is True


def test_host_budget_add_remove_tracks_resident_total() -> None:
    budget = HostBudget(ceiling_gib=20.0)
    budget.add("modelA", 4.0)
    budget.add("modelB", 6.0)
    assert budget.resident_gib() == pytest.approx(10.0)
    assert budget.resident_ids() == frozenset({"modelA", "modelB"})

    budget.remove("modelA")
    assert budget.resident_gib() == pytest.approx(6.0)
    assert budget.is_resident("modelA") is False
    # Removing an absent model is a no-op.
    budget.remove("ghost")
    assert budget.resident_gib() == pytest.approx(6.0)


def test_host_budget_add_is_idempotent_by_id() -> None:
    # The same model added twice counts once (de-dup by id — ref-count semantics).
    budget = HostBudget(ceiling_gib=20.0)
    budget.add("modelA", 4.0)
    budget.add("modelA", 4.0)
    assert budget.resident_gib() == pytest.approx(4.0)


def test_budget_decision_message_shapes() -> None:
    ok = BudgetDecision(
        ok=True, ceiling_gib=10.0, resident_gib=4.0, candidate_gib=2.0, candidate_model="m"
    )
    assert "fits" in ok.message()
    refused = BudgetDecision(
        ok=False, ceiling_gib=10.0, resident_gib=8.0, candidate_gib=5.0, candidate_model="m"
    )
    msg = refused.message()
    assert "plafond" in msg and "libère un rôle" in msg
    # None ceiling renders as the infinity marker, never a crash.
    skip = BudgetDecision(
        ok=True, ceiling_gib=None, resident_gib=0.0, candidate_gib=1.0, candidate_model="m"
    )
    assert "∞" in skip.message()


def test_resident_model_equality_by_id() -> None:
    # ResidentModel de-duplicates by id in a set regardless of footprint.
    a1 = ResidentModel("modelA", 4.0)
    a2 = ResidentModel("modelA", 9.0)
    b = ResidentModel("modelB", 4.0)
    assert a1 == a2
    assert {a1, a2, b} == {a1, b}


# --- composition: build_host_budget + footprint probe ------------------------


def test_build_host_budget_local_uses_injected_ram() -> None:
    budget = build_host_budget(
        "localhost:1234",
        ceiling_gib=None,
        reserve_gib=8.0,
        per_host_override={},
        is_local=True,
        ram_probe=lambda: 32.0,
    )
    assert budget.ceiling_gib == pytest.approx(24.0)


def test_build_host_budget_pinned_ceiling_wins_over_ram() -> None:
    budget = build_host_budget(
        "localhost:1234",
        ceiling_gib=16.0,
        reserve_gib=8.0,
        per_host_override={},
        is_local=True,
        ram_probe=lambda: 64.0,  # ignored — pinned ceiling wins
    )
    assert budget.ceiling_gib == 16.0


def test_build_host_budget_override_wins_over_everything() -> None:
    budget = build_host_budget(
        "studio.lan:1234",
        ceiling_gib=16.0,
        reserve_gib=8.0,
        per_host_override={"studio.lan:1234": 48.0},
        is_local=False,
        ram_probe=lambda: 9999.0,
    )
    assert budget.ceiling_gib == 48.0


def test_build_host_budget_remote_no_override_is_none_skip() -> None:
    budget = build_host_budget(
        "studio.lan:1234",
        ceiling_gib=None,
        reserve_gib=8.0,
        per_host_override={},
        is_local=False,
        ram_probe=lambda: 9999.0,  # not consulted for remote
    )
    assert budget.ceiling_gib is None


def test_build_model_footprint_probe_uses_resolved_path_size() -> None:
    probe = build_model_footprint_probe(
        resolve_path=lambda mid: f"/models/{mid}.gguf",
        file_size_probe=lambda _path: 4.0,
        gib_per_1k_tokens=0.5,
    )
    # 4 GiB weights + 2K-token margin at 0.5/1K = 4 + 1 = 5.
    assert probe("modelA", 2048) == pytest.approx(5.0)


def test_build_model_footprint_probe_falls_back_when_unsized() -> None:
    probe = build_model_footprint_probe(
        resolve_path=lambda _mid: None,  # path unknown
        default_footprint_gib=6.0,
        gib_per_1k_tokens=0.5,
    )
    # Coarse 6 GiB default + 2K margin (0.5/1K) = 6 + 1 = 7.
    assert probe("modelA", 2048) == pytest.approx(7.0)
    # A resolvable path whose file cannot be sized also falls back.
    probe2 = build_model_footprint_probe(
        resolve_path=lambda mid: f"/missing/{mid}",
        file_size_probe=lambda _path: None,
        default_footprint_gib=6.0,
    )
    assert probe2("modelB", None) == pytest.approx(6.0 + DEFAULT_KV_MARGIN_GIB)


# --- real probes: smoke only (machine-dependent values) ----------------------


def test_detect_local_ram_never_raises_and_is_positive_or_none() -> None:
    value = detect_local_ram_gib()
    assert value is None or value > 0.0


def test_file_size_gib_missing_path_is_none_not_raise() -> None:
    assert file_size_gib("/no/such/file/at/all.gguf") is None


def test_file_size_gib_reads_a_real_file(tmp_path) -> None:  # type: ignore[no-untyped-def]
    f = tmp_path / "weights.gguf"
    f.write_bytes(b"\x00" * (1024 * 1024))  # 1 MiB
    size = file_size_gib(str(f))
    assert size is not None
    assert size == pytest.approx(1.0 / 1024, rel=1e-3)  # 1 MiB in GiB
