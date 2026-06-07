"""``bob`` CLI entrypoint — the attestation harness front door.

PRD 0016 / issue 0098. Greenfield CLI (there is no ``bob`` command today). The
only subcommand wired in this slice is ``attest``::

    bob attest scenarios/text-say.attest.yaml

It parses the YAML scenario, boots an isolated ephemeral backend with the
deterministic ``fake`` LLM, drives the real WS, runs the assertions and prints
the Annexe C **verdict JSON to stdout**. Exit code is ``0`` when ``ok: true``,
``1`` otherwise — so the command gates CI / ``verify`` directly. The PRD also
reserves ``say`` / ``scenario`` subcommands for later slices; the parser is
structured so adding them is a one-liner.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence

from bob.attest.runner import Scenario, ScenarioError, ScenarioRunner


def _print_verdict(verdict: dict[str, object]) -> None:
    """Emit the verdict as a single pretty JSON document on stdout."""

    json.dump(verdict, sys.stdout, ensure_ascii=False, indent=2)
    sys.stdout.write("\n")


def _cmd_attest(args: argparse.Namespace) -> int:
    """Run one scenario file → verdict JSON on stdout → exit code.

    A scenario that fails to parse / uses an unsupported feature is itself a
    failed attestation: we print a minimal verdict (``ok: false`` with the
    error) and return ``1`` rather than dumping a traceback, so the wrapping CI
    step sees a clean machine-readable failure.
    """

    try:
        scenario = Scenario.from_yaml_file(args.scenario)
        runner = ScenarioRunner(scenario, deep=bool(getattr(args, "deep", False)))
    except (ScenarioError, OSError) as exc:
        _print_verdict(
            {
                "scenario": str(args.scenario),
                "ok": False,
                "error": str(exc),
                "assertions": [],
            }
        )
        return 1

    verdict = runner.run()
    _print_verdict(verdict)
    return 0 if verdict.get("ok") else 1


def build_parser() -> argparse.ArgumentParser:
    """Build the top-level ``bob`` argument parser (subcommands plug in here)."""

    parser = argparse.ArgumentParser(
        prog="bob",
        description="Bob attestation harness CLI.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    attest = sub.add_parser(
        "attest",
        help="Run a declarative attestation scenario (YAML) and print a verdict.",
    )
    attest.add_argument("scenario", help="Path to the scenario YAML file.")
    attest.add_argument(
        "--deep",
        action="store_true",
        help=(
            "Enable the TTS->STT round-trip check (issue 0110): re-transcribe "
            "Bob's spoken reply and emit a 'roundtrip_transcript' observation so "
            "a 'transcript_roundtrip_similarity_gte' assertion can verify "
            "intelligibility. Off by default (deterministic + fast)."
        ),
    )
    attest.set_defaults(func=_cmd_attest)

    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Parse argv, dispatch the subcommand, return the process exit code."""

    parser = build_parser()
    args = parser.parse_args(argv)
    func = args.func
    result: int = func(args)
    return result


if __name__ == "__main__":  # pragma: no cover — module-run convenience.
    raise SystemExit(main())
