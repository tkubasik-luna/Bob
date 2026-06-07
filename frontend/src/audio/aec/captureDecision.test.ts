import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import { describe, expect, test } from "vitest";
import { buildSpikeVerdict, selectCapturePath } from "./aecSpikeSelector";
import {
  DEFAULT_CAPTURE_DECISION,
  HALF_DUPLEX_GATE_SPEC,
  SCAFFOLDED_SPIKE_RESULTS,
  VERDICT_ARTEFACT_FILENAME,
  getCaptureDecision,
} from "./captureDecision";

describe("captureDecision artefact (consumed by issue 0099)", () => {
  test("default decision is derived from the scaffolded results via the real selector", () => {
    // No hand-picked path: it must equal what the pure selector says.
    expect(DEFAULT_CAPTURE_DECISION.path).toBe(selectCapturePath(SCAFFOLDED_SPIKE_RESULTS));
  });

  test("headless (no-hardware) default selects the safe `rust` fallback", () => {
    expect(DEFAULT_CAPTURE_DECISION.path).toBe("rust");
    expect(DEFAULT_CAPTURE_DECISION.hardwarePending).toBe(true);
  });

  test("getCaptureDecision is the canonical read path and is stable", () => {
    expect(getCaptureDecision()).toEqual(DEFAULT_CAPTURE_DECISION);
    expect(getCaptureDecision().schemaVersion).toBe(1);
  });

  test("scaffolded results list all three criteria as pending (no fake pass)", () => {
    expect(SCAFFOLDED_SPIKE_RESULTS).toHaveLength(3);
    expect(SCAFFOLDED_SPIKE_RESULTS.every((r) => r.status === "pending")).toBe(true);
  });

  test("verdict artefact filename mirrors the BOB_DATA_DIR json convention", () => {
    expect(VERDICT_ARTEFACT_FILENAME).toBe("aec_spike_verdict.json");
  });
});

describe("committed spike verdict artefact (aec_spike_verdict.json)", () => {
  // The persisted JSON verdict (the spike's "verdict émis en JSON" criterion).
  // It must agree with the pure selector so it can never silently drift from
  // the AFK rule. We compare everything except the timestamp.
  interface RawVerdict {
    schema_version: number;
    ok: boolean;
    chosen_path: string;
    hardware_pending: boolean;
    criteria: { id: string; status: string }[];
  }

  function loadVerdict(): RawVerdict {
    const path = resolve(__dirname, "./aec_spike_verdict.json");
    return JSON.parse(readFileSync(path, "utf-8")) as RawVerdict;
  }

  test("matches buildSpikeVerdict(SCAFFOLDED_SPIKE_RESULTS) ignoring timestamp", () => {
    const expected = buildSpikeVerdict(SCAFFOLDED_SPIKE_RESULTS, new Date(0));
    const actual = loadVerdict();
    expect(actual.schema_version).toBe(expected.schema_version);
    expect(actual.ok).toBe(expected.ok);
    expect(actual.chosen_path).toBe(expected.chosen_path);
    expect(actual.hardware_pending).toBe(expected.hardware_pending);
    expect(actual.criteria.map((c) => `${c.id}:${c.status}`)).toEqual(
      expected.criteria.map((c) => `${c.id}:${c.status}`),
    );
  });

  test("the persisted verdict agrees with the in-app decision path", () => {
    expect(loadVerdict().chosen_path).toBe(getCaptureDecision().path);
  });
});

describe("HALF_DUPLEX_GATE_SPEC (handoff to issue 0101)", () => {
  test("mutes the mic during bob_speaking", () => {
    expect(HALF_DUPLEX_GATE_SPEC.muteDuringState).toBe("bob_speaking");
  });

  test("engaging the gate is flagged as a degradation, emitted as a voice warn event", () => {
    expect(HALF_DUPLEX_GATE_SPEC.isDegradation).toBe(true);
    expect(HALF_DUPLEX_GATE_SPEC.event).toEqual({
      category: "voice",
      severity: "warn",
      type: "aec_degraded_half_duplex",
    });
  });
});
