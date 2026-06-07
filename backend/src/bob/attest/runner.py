"""Scenario parsing + timeline execution for the attestation harness.

PRD 0016 / issue 0098 + Annexe C. :class:`ScenarioRunner` turns a declarative
YAML scenario into a machine verdict:

1. Parse the YAML into a typed :class:`Scenario` (defensive — a malformed
   scenario raises :class:`ScenarioError` with a precise message, never a
   silent half-run).
2. Boot an isolated :class:`bob.attest.ephemeral.EphemeralBackend` carrying the
   scenario's ``fake_llm`` script (``backend: ephemeral`` + ``llm: fake`` — the
   only combo this skeleton supports; ``external`` / ``real`` raise loudly).
3. Capture ``/ws/debug`` and execute the ``timeline`` over the real WS
   (:mod:`bob.attest.drive`).
4. Run every ``assertions`` entry against the captured events
   (:mod:`bob.attest.assertions`) and emit the Annexe C verdict JSON.

The verdict shape (Annexe C):

    {
      "scenario": "...", "ok": true, "duration_ms": 1840,
      "assertions": [{"kind": "...", "ok": true, ...}],
      "events_captured": 37,
      "backend": {"mode": "ephemeral", "port": 53122},
      "llm": "fake"
    }
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from bob.attest.assertions import (
    LOGICAL_EVENT_MATCHERS,
    AssertionContext,
    CapturedEvent,
    project_deliverable,
    run_assertion,
)
from bob.attest.drive import (
    DebugCapture,
    inject_audio_ws,
    inject_bargein_ws,
    inject_text,
    roundtrip_transcribe,
    synth_mic_frames,
    synth_voiced_frames,
)
from bob.attest.ephemeral import EphemeralBackend
from bob.attest.fake_backend import FakeScript


class ScenarioError(ValueError):
    """Raised when a scenario file is malformed or uses an unsupported feature."""


@dataclass(frozen=True)
class Scenario:
    """A parsed, validated attestation scenario (Annexe C shape).

    Only ``name`` + ``timeline`` are strictly required; ``backend`` defaults to
    ``ephemeral`` and ``llm`` to ``fake`` (the harness default). ``fake_llm`` /
    ``assertions`` default to empty lists. ``timeline`` steps are kept as raw
    dicts and validated at execution time so an unknown op fails with a precise
    per-step message.
    """

    name: str
    description: str = ""
    backend: str = "ephemeral"
    llm: str = "fake"
    fake_llm: list[dict[str, Any]] = field(default_factory=list)
    timeline: list[dict[str, Any]] = field(default_factory=list)
    assertions: list[dict[str, Any]] = field(default_factory=list)
    #: Optional backend env overrides (PRD 0016 / issue 0109). A flat
    #: string→scalar map merged into the ephemeral backend's env BEFORE the
    #: forced isolation/provider keys (which always win — a scenario can't break
    #: isolation). Lets a scenario pin a harness-only dial it needs, e.g. a tiny
    #: ``VOICE_RETENTION_MAX_AUDIO_BYTES`` so the retention sweep evicts. Empty by
    #: default (zero behaviour change for every prior scenario).
    env: dict[str, str] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, raw: object) -> Scenario:
        if not isinstance(raw, dict):
            raise ScenarioError("scenario must be a YAML mapping at the top level")
        name = raw.get("name")
        if not isinstance(name, str) or not name:
            raise ScenarioError("scenario requires a non-empty 'name'")
        timeline = raw.get("timeline", [])
        if not isinstance(timeline, list):
            raise ScenarioError("'timeline' must be a list of steps")
        assertions = raw.get("assertions", [])
        if not isinstance(assertions, list):
            raise ScenarioError("'assertions' must be a list")
        fake_llm = raw.get("fake_llm", [])
        if not isinstance(fake_llm, list):
            raise ScenarioError("'fake_llm' must be a list of rules")
        raw_env = raw.get("env", {})
        if not isinstance(raw_env, dict):
            raise ScenarioError("'env' must be a mapping of name -> value")
        return cls(
            name=name,
            description=str(raw.get("description", "")),
            backend=str(raw.get("backend", "ephemeral")),
            llm=str(raw.get("llm", "fake")),
            fake_llm=[s for s in fake_llm if isinstance(s, dict)],
            timeline=[s for s in timeline if isinstance(s, dict)],
            assertions=[a for a in assertions if isinstance(a, dict)],
            env={str(k): str(v) for k, v in raw_env.items()},
        )

    @classmethod
    def from_yaml_file(cls, path: str | Path) -> Scenario:
        text = Path(path).read_text(encoding="utf-8")
        try:
            data = yaml.safe_load(text)
        except yaml.YAMLError as exc:
            raise ScenarioError(f"invalid YAML: {exc}") from exc
        return cls.from_dict(data)


def build_verdict(
    scenario: Scenario,
    *,
    ok: bool,
    duration_ms: int,
    assertion_results: list[dict[str, Any]],
    events_captured: int,
    backend_mode: str,
    port: int | None,
) -> dict[str, Any]:
    """Assemble the Annexe C verdict JSON dict."""

    return {
        "scenario": scenario.name,
        "ok": ok,
        "duration_ms": duration_ms,
        "assertions": assertion_results,
        "events_captured": events_captured,
        "backend": {"mode": backend_mode, "port": port},
        "llm": scenario.llm,
    }


class ScenarioRunner:
    """Execute a :class:`Scenario` end-to-end and return the verdict dict.

    Synchronous entry point :meth:`run` (the CLI calls it); the actual WS work
    runs on a private event loop via :meth:`_run_async`. Keeping the public
    surface sync means the CLI needs no asyncio ceremony and tests can call
    :meth:`run` directly.
    """

    def __init__(self, scenario: Scenario, *, deep: bool = False) -> None:
        self._scenario = scenario
        #: ``--deep`` (issue 0110): when set, a voiced audio turn captures Bob's
        #: outbound TTS PCM and the runner synthesises a ``roundtrip_transcript``
        #: observation (TTS→STT→compare) the ``transcript_roundtrip_similarity_gte``
        #: assertion reads. Off by default (the fast, deterministic path).
        self._deep = deep
        #: Bob's captured outbound PCM per deep audio turn (accumulated by
        #: ``_do_inject_audio`` when deep). Re-transcribed at settle.
        self._deep_reply_pcm = bytearray()
        self._validate_supported()

    def _validate_supported(self) -> None:
        """Reject combos the skeleton cannot honour — loudly, up front.

        ``backend: external`` and ``llm: real`` are reserved for later slices;
        attesting them now would silently mislead, so we fail before booting.
        """

        if self._scenario.backend != "ephemeral":
            raise ScenarioError(
                f"backend {self._scenario.backend!r} not supported yet "
                "(only 'ephemeral' in this slice)"
            )
        if self._scenario.llm != "fake":
            raise ScenarioError(
                f"llm {self._scenario.llm!r} not supported yet (only 'fake' in this slice)"
            )

    def run(self) -> dict[str, Any]:
        """Boot, drive, assert, tear down — return the Annexe C verdict dict."""

        import asyncio

        return asyncio.run(self._run_async())

    async def _run_async(self) -> dict[str, Any]:
        started = time.monotonic()
        script = FakeScript.from_rules(self._scenario.fake_llm)
        backend = EphemeralBackend(
            fake_llm_script=script.to_json(),
            fake_stt_transcript=self._audio_transcript(),
            extra_env=self._extra_env(),
        )
        handle = backend.start()
        capture = DebugCapture(handle.ws_base)
        timeline_errors: list[str] = []
        try:
            await capture.open()
            await self._execute_timeline(handle.ws_base, capture, timeline_errors)
            # Settle: a turn's debug frames are pushed to the ``/ws/debug``
            # subscriber concurrently with the chat reply. ``inject_text``
            # already blocks until ``thinking:end`` (after the ``output``
            # event), but yield briefly so the capture reader task drains any
            # frame still in flight before we snapshot for assertions.
            await self._settle(capture)
            events = capture.events
        finally:
            await capture.close()
            backend.stop()

        deliverable = project_deliverable(events)
        # ``--deep`` (issue 0110): re-transcribe Bob's captured spoken reply and
        # append the round-trip observation so a
        # ``transcript_roundtrip_similarity_gte`` assertion can read it. Appended
        # to the captured-event list (the harness owns the verdict, so a
        # harness-computed observation rides the same ``ctx.events`` the
        # assertions inspect). No-op when not deep / Bob never spoke.
        if self._deep:
            events.append(self._roundtrip_event(deliverable))
        ctx = AssertionContext(events=events, deliverable=deliverable)

        results: list[dict[str, Any]] = []
        all_ok = True
        # A timeline op that failed (e.g. a wait_event that timed out) is a
        # surfaced FAIL in the verdict so the run is never silently green.
        for message in timeline_errors:
            results.append({"kind": "timeline", "ok": False, "error": message})
            all_ok = False
        for spec in self._scenario.assertions:
            result = run_assertion(spec, ctx)
            results.append(result.to_dict())
            all_ok = all_ok and result.ok

        duration_ms = int((time.monotonic() - started) * 1000)
        return build_verdict(
            self._scenario,
            ok=all_ok,
            duration_ms=duration_ms,
            assertion_results=results,
            events_captured=len(events),
            backend_mode="ephemeral",
            port=handle.port,
        )

    @staticmethod
    async def _settle(capture: DebugCapture, *, quiet_ms: int = 150) -> None:
        """Wait until the capture stream goes quiet for ``quiet_ms``.

        Polls the captured-event count; once it stops growing for one quiet
        window we assume the turn's frames have all been drained. Bounded by a
        small absolute cap so a chatty backend can't stall the run.
        """

        import asyncio

        cap_deadline = asyncio.get_event_loop().time() + 2.0
        last_count = -1
        while asyncio.get_event_loop().time() < cap_deadline:
            count = len(capture.events)
            if count == last_count:
                return
            last_count = count
            await asyncio.sleep(quiet_ms / 1000.0)

    async def _execute_timeline(
        self, ws_base: str, capture: DebugCapture, errors: list[str]
    ) -> None:
        """Run each timeline step in order. Unknown ops are loud errors.

        Supported ops: ``inject_text``, ``inject_audio`` (issue 0099 — binary
        mic frames over the real WS), ``inject_bargein`` (issue 0101 — drive Bob
        into ``bob_speaking`` then interrupt him on the same socket),
        ``wait_event``, ``wait_state`` (FSM), ``wait_ms``.
        """

        import asyncio

        for index, step in enumerate(self._scenario.timeline):
            op = step.get("do")
            try:
                if op == "inject_text":
                    text = step.get("text", "")
                    if not isinstance(text, str):
                        raise ScenarioError("inject_text 'text' must be a string")
                    await inject_text(ws_base, text)
                elif op == "inject_audio":
                    await self._do_inject_audio(ws_base, step)
                elif op == "inject_bargein":
                    await self._do_inject_bargein(ws_base, step)
                elif op == "wait_event":
                    await self._do_wait_event(step, capture, errors)
                elif op == "wait_ms":
                    ms = step.get("ms", step.get("at_ms", 0))
                    await asyncio.sleep(max(int(ms), 0) / 1000.0)
                elif op == "wait_state":
                    await self._do_wait_state(step, capture, errors)
                else:
                    errors.append(f"timeline[{index}] unknown op {op!r}")
            except ScenarioError as exc:
                errors.append(f"timeline[{index}] {exc}")

    def _audio_transcript(self) -> str:
        """The transcript the fake STT engine should converge to for this run.

        Scans the timeline for the first ``inject_audio`` / ``inject_bargein``
        step's ``transcript`` (the deterministic fake engine is single-transcript
        per backend, so the first audio turn defines it). Empty when the scenario
        injects no audio — a text-only run leaves the fake engine idle.
        """

        for step in self._scenario.timeline:
            if step.get("do") in ("inject_audio", "inject_bargein"):
                transcript = step.get("transcript", "")
                return transcript if isinstance(transcript, str) else ""
        return ""

    def _roundtrip_event(self, deliverable: str) -> CapturedEvent:
        """Build the ``--deep`` ``roundtrip_transcript`` observation (issue 0110).

        ``said`` is Bob's reply (the projected deliverable); ``heard`` is the
        re-transcription of the PCM he actually played
        (:func:`bob.attest.drive.roundtrip_transcribe` — a deterministic fake
        stand-in keyed to ``said`` under fake TTS/STT, the real whisper under
        ``--real``). The similarity is left to the assertion to compute from
        ``said`` / ``heard`` (it owns the ratio), so the metric lives in exactly
        one place.

        The frame is shaped exactly like a captured ``/ws/debug`` voice event
        (``payload.ws_event.type == "roundtrip_transcript"``) so the assertion's
        matcher reads it identically to a wire event. A turn where Bob never
        spoke yields empty PCM → empty ``heard`` → similarity 0.0, so the deep
        assertion fails loudly rather than passing on silence.
        """

        heard = roundtrip_transcribe(
            bytes(self._deep_reply_pcm), said=deliverable, real=self._scenario.llm == "real"
        )
        return {
            "category": "voice",
            "severity": "debug",
            "source": "bob.attest.roundtrip",
            "summary": "roundtrip_transcript",
            "payload": {
                "ws_event": {
                    "type": "roundtrip_transcript",
                    "said": deliverable,
                    "heard": heard,
                }
            },
        }

    def _extra_env(self) -> dict[str, str]:
        """Harness-only env dials a scenario's ops need (issue 0101 + 0102).

        A scenario with an ``inject_bargein`` step needs Bob to hold the floor
        long enough to be interrupted, so the fake TTS is paced
        (``BOB_FAKE_TTS_CHUNK_MS``) and the barge-in confirmation window
        (``BARGEIN_CONFIRM_MS``) is pinned to a deterministic value.

        Issue 0102: an ``inject_audio`` / ``inject_bargein`` step carrying
        ``thinker: true`` pins ``THINKER_DEBOUNCE_MS=0`` so the background Thinker
        fires on the first ``stt_partial`` (no debounce coalescing to race the
        short synthetic turn) — making "≥1 snapshot before the endpoint"
        deterministic. Derived from the op's params so the YAML stays declarative;
        empty for scenarios that set none of these (zero behaviour change for the
        0099/0100/0101 scenarios).
        """

        env: dict[str, str] = {}
        for step in self._scenario.timeline:
            op = step.get("do")
            if op == "inject_bargein":
                confirm_ms = int(step.get("confirm_ms", 200))
                chunk_ms = int(step.get("tts_chunk_ms", 60))
                env["BARGEIN_CONFIRM_MS"] = str(max(1, confirm_ms))
                env["BOB_FAKE_TTS_CHUNK_MS"] = str(max(0, chunk_ms))
                # A handful of chunks at chunk_ms each = the speaking window the
                # barge-in lands in; the fake yields BOB_FAKE_TTS_CHUNKS per
                # sentence, so a multi-chunk reply spans well past the window.
                env["BOB_FAKE_TTS_CHUNKS"] = str(max(1, int(step.get("tts_chunks", 4))))
            if op in ("inject_audio", "inject_bargein") and step.get("thinker"):
                env["THINKER_DEBOUNCE_MS"] = "0"
        # Scenario-level backend env (issue 0109): merged AFTER the derived
        # per-step dials so an explicit scenario value wins, but still BEFORE the
        # forced isolation/provider keys in the ephemeral backend.
        env.update(self._scenario.env)
        return env

    async def _do_inject_audio(self, ws_base: str, step: dict[str, Any]) -> None:
        """Stream synthetic mic frames for one voice turn over the binary WS.

        The fake STT engine (booted with ``BOB_FAKE_STT_TRANSCRIPT`` =
        :meth:`_audio_transcript`) ignores PCM content and converges to that
        transcript, so silent frames sized to comfortably cover the transcript
        drive the REAL decode → ``VoiceTurn`` → ``stt_final`` path. Content
        fidelity is irrelevant — the assertion checks the contract, not audio.

        ``voiced: true`` (issue 0100) instead streams a loud burst + trailing
        silence so the full-duplex loop's energy VAD trips ``vad_speech_start``
        and its silence-floor Endpointer fires ``endpoint`` — driving the FSM
        through ``user_speaking`` → ``thinking`` → ``bob_speaking`` → ``idle``
        and the say-path. The fake STT still converges to the scripted
        transcript regardless of content.
        """

        transcript = step.get("transcript", "")
        words = max(1, len(str(transcript).split()))
        # Fake engine reveals one word per ~1600 samples; 480 samples/frame.
        voiced_count = max(8, (words * 1600) // 480 + 4)
        if step.get("voiced"):
            frames = synth_voiced_frames(voiced_count=voiced_count)
            # ``--deep`` (issue 0110): collect Bob's outbound TTS PCM off this
            # socket so the round-trip can re-transcribe exactly what he played.
            collect = self._deep_reply_pcm if self._deep else None
            # Keep the chat socket open until the say-path finishes so the
            # in-flight reply is not cancelled by the socket close (issue 0100).
            await inject_audio_ws(ws_base, frames, await_reply=True, collect_reply_pcm=collect)
        else:
            frames = synth_mic_frames(frame_count=voiced_count)
            await inject_audio_ws(ws_base, frames)

    async def _do_inject_bargein(self, ws_base: str, step: dict[str, Any]) -> None:
        """Drive a turn Bob starts speaking, then BARGE IN on it (issue 0101).

        Delegates to :func:`bob.attest.drive.inject_bargein_ws` (one socket
        spanning turn-1 → Bob speaks → confirmation-window burst). The pacing /
        confirmation env is set by :meth:`_extra_env` from this same step's
        params; here we just translate the timeline knobs to the drive call.
        """

        transcript = step.get("transcript", "")
        if not isinstance(transcript, str) or not transcript:
            raise ScenarioError("inject_bargein 'transcript' must be a non-empty string")
        await inject_bargein_ws(
            ws_base,
            transcript=transcript,
            bargein_after_ms=int(step.get("at_ms", 50)),
            bargein_span_ms=int(step.get("span_ms", 280)),
        )

    async def _do_wait_event(
        self, step: dict[str, Any], capture: DebugCapture, errors: list[str]
    ) -> None:
        """Block until a logical event of ``type`` is captured (or timeout)."""

        logical_type = step.get("type")
        timeout_ms = int(step.get("timeout_ms", 1500))
        matcher = (
            LOGICAL_EVENT_MATCHERS.get(logical_type) if isinstance(logical_type, str) else None
        )
        if matcher is None:
            errors.append(f"wait_event: unknown logical event type {logical_type!r}")
            return
        ok = await capture.wait_for(matcher, timeout_ms=timeout_ms)
        if not ok:
            errors.append(f"wait_event: '{logical_type}' not observed within {timeout_ms}ms")

    async def _do_wait_state(
        self, step: dict[str, Any], capture: DebugCapture, errors: list[str]
    ) -> None:
        """Block until a ``turn_state`` voice event reaches ``state`` (Annexe B).

        Op: ``{do: wait_state, state: bob_speaking, timeout_ms: 1500}``. The
        full-duplex FSM (issue 0100) emits a ``turn_state`` voice event on every
        transition; this synchronises on the one whose ``to`` field equals the
        requested state (optionally narrowed by ``turn_id``). A timeout records a
        loud timeline error so the run is never silently green.
        """

        state = step.get("state")
        if not isinstance(state, str) or not state:
            errors.append("wait_state: requires a 'state' string")
            return
        timeout_ms = int(step.get("timeout_ms", 1500))
        want_turn = step.get("turn_id")
        turn_state_matcher = LOGICAL_EVENT_MATCHERS["turn_state"]

        def _reached(event: CapturedEvent) -> bool:
            if not turn_state_matcher(event):
                return False
            ws_event = (event.get("payload") or {}).get("ws_event") or {}
            if ws_event.get("to") != state:
                return False
            if isinstance(want_turn, str) and want_turn:
                return bool(ws_event.get("turn_id") == want_turn)
            return True

        ok = await capture.wait_for(_reached, timeout_ms=timeout_ms)
        if not ok:
            errors.append(f"wait_state: '{state}' not reached within {timeout_ms}ms")
