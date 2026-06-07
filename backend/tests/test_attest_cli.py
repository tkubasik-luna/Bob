"""CLI surface tests for ``bob attest`` (issue 0098).

These exercise the argument parsing + exit-code contract WITHOUT booting a real
backend by monkeypatching :meth:`ScenarioRunner.run` (the boot/drive path is
covered by ``test_attest_ephemeral``). The bad-scenario path is checked for
real: a parse failure must print a machine-readable ``ok: false`` verdict and
exit 1 rather than dumping a traceback.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from bob.attest import cli
from bob.attest.runner import ScenarioRunner


def test_main_exits_0_on_ok_true_verdict(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    scenario = tmp_path / "s.yaml"
    scenario.write_text("name: ok-demo\ntimeline: []\nassertions: []\n", encoding="utf-8")

    def _fake_run(self: Any) -> dict[str, Any]:
        return {"scenario": "ok-demo", "ok": True, "assertions": []}

    monkeypatch.setattr(ScenarioRunner, "run", _fake_run)

    code = cli.main(["attest", str(scenario)])
    assert code == 0
    out = json.loads(capsys.readouterr().out)
    assert out == {"scenario": "ok-demo", "ok": True, "assertions": []}


def test_main_exits_1_on_ok_false_verdict(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    scenario = tmp_path / "s.yaml"
    scenario.write_text("name: bad-demo\ntimeline: []\n", encoding="utf-8")

    monkeypatch.setattr(ScenarioRunner, "run", lambda self: {"scenario": "bad-demo", "ok": False})

    code = cli.main(["attest", str(scenario)])
    assert code == 1
    assert json.loads(capsys.readouterr().out)["ok"] is False


def test_main_bad_scenario_prints_verdict_and_exits_1(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    scenario = tmp_path / "no-name.yaml"
    scenario.write_text("timeline: []\n", encoding="utf-8")  # missing 'name'

    code = cli.main(["attest", str(scenario)])
    assert code == 1
    verdict = json.loads(capsys.readouterr().out)
    assert verdict["ok"] is False
    assert "name" in verdict["error"]


def test_main_missing_file_exits_1(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    code = cli.main(["attest", str(tmp_path / "does-not-exist.yaml")])
    assert code == 1
    assert json.loads(capsys.readouterr().out)["ok"] is False


def test_parser_requires_a_subcommand() -> None:
    with pytest.raises(SystemExit):
        cli.main([])
