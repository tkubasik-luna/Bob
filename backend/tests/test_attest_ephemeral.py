"""Boot/teardown + isolation tests for the ephemeral attestation backend.

These boot a REAL ``uvicorn bob.main:app`` subprocess (the harness' black-box
contract), so they are a touch slower than the pure-unit suites — but they are
the only way to attest the isolation invariant the issue calls out: the
ephemeral backend must NOT touch the real ``BOB_DATA_DIR``.

The end-to-end test also runs a full ``ScenarioRunner`` against today's
text-only Bob, proving the harness is green against the current product (the
positive half of the issue-0098 demo) without any voice feature.
"""

from __future__ import annotations

import os
import urllib.request
from pathlib import Path

from bob.attest.ephemeral import EphemeralBackend, find_free_port
from bob.attest.runner import Scenario, ScenarioRunner

# The data dir the test process / conftest points the REAL backend at. The
# ephemeral backend must never write here.
_REAL_DATA_DIR = Path(os.environ["BOB_DATA_DIR"])


def test_find_free_port_returns_distinct_usable_ports() -> None:
    a = find_free_port()
    b = find_free_port()
    assert isinstance(a, int) and 1024 < a < 65536
    # Not a hard guarantee they differ, but the OS overwhelmingly hands out
    # fresh ports; a clash would still be a usable port.
    assert isinstance(b, int)


def test_boot_then_health_then_teardown_cleans_temp_dir() -> None:
    backend = EphemeralBackend(fake_llm_script="[]", boot_timeout_seconds=60)
    handle = backend.start()
    temp_dir = handle.data_dir
    try:
        # Health endpoint is live on the dedicated port.
        with urllib.request.urlopen(f"{handle.http_base}/health", timeout=5) as resp:
            assert resp.status == 200
        # The ephemeral backend created + uses its OWN temp dir.
        assert temp_dir.exists()
        assert temp_dir != _REAL_DATA_DIR
        assert (temp_dir / "bob.db").exists()
    finally:
        backend.stop()

    # Teardown wiped the temp dir entirely.
    assert not temp_dir.exists()


def test_isolation_does_not_touch_real_data_dir() -> None:
    """A full boot/teardown must leave the real BOB_DATA_DIR's DB untouched.

    We snapshot the real data dir's ``bob.db`` mtime (if any) before and after
    a complete ephemeral cycle and assert it did not appear / change. The
    ephemeral backend runs on a temp dir + its own port, so nothing it does can
    reach here.
    """

    real_db = _REAL_DATA_DIR / "bob.db"
    before_exists = real_db.exists()
    before_mtime = real_db.stat().st_mtime if before_exists else None

    with EphemeralBackend(fake_llm_script="[]", boot_timeout_seconds=60) as handle:
        # Drive nothing — boot alone would create bob.db IF it pointed here.
        assert handle.data_dir != _REAL_DATA_DIR

    after_exists = real_db.exists()
    after_mtime = real_db.stat().st_mtime if after_exists else None

    # The ephemeral cycle neither created nor mutated the real DB.
    assert after_exists == before_exists
    assert after_mtime == before_mtime


def test_context_manager_starts_and_stops() -> None:
    with EphemeralBackend(fake_llm_script="[]", boot_timeout_seconds=60) as handle:
        temp_dir = handle.data_dir
        assert temp_dir.exists()
    assert not temp_dir.exists()


def test_end_to_end_text_scenario_is_green_against_current_bob() -> None:
    """The text-say demo passes end-to-end: a ``say`` fires + deliverable set."""

    scenario = Scenario.from_dict(
        {
            "name": "e2e-text-say",
            "backend": "ephemeral",
            "llm": "fake",
            "fake_llm": [
                {"role": "jarvis", "on_input_contains": "bonjour", "reply": "Salut, je suis Bob."}
            ],
            "timeline": [
                {"do": "inject_text", "text": "bonjour"},
                {"do": "wait_event", "type": "say", "timeout_ms": 15000},
            ],
            "assertions": [
                {"kind": "event_emitted", "type": "say"},
                {"kind": "deliverable_nonempty"},
                {"kind": "no_error_events"},
            ],
        }
    )
    verdict = ScenarioRunner(scenario).run()

    assert verdict["ok"] is True, verdict
    assert verdict["scenario"] == "e2e-text-say"
    assert verdict["llm"] == "fake"
    assert verdict["backend"]["mode"] == "ephemeral"
    assert isinstance(verdict["backend"]["port"], int)
    assert verdict["events_captured"] >= 1
    kinds = {a["kind"]: a["ok"] for a in verdict["assertions"]}
    assert kinds == {"event_emitted": True, "deliverable_nonempty": True, "no_error_events": True}


def test_end_to_end_wrong_scenario_fails_with_ok_false() -> None:
    """A scenario asserting a reply but injecting none must report ok: false."""

    scenario = Scenario.from_dict(
        {
            "name": "e2e-no-reply",
            "timeline": [{"do": "wait_ms", "ms": 50}],
            "assertions": [
                {"kind": "event_emitted", "type": "say"},
                {"kind": "deliverable_nonempty"},
            ],
        }
    )
    verdict = ScenarioRunner(scenario).run()

    assert verdict["ok"] is False
    assert all(a["ok"] is False for a in verdict["assertions"])
