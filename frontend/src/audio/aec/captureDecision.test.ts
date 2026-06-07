import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import { describe, expect, test } from "vitest";
import { buildSpikeVerdict, selectCapturePath } from "./aecSpikeSelector";
import {
  DEFAULT_CAPTURE_DECISION,
  FALLBACK_CAPTURE_PATH,
  HALF_DUPLEX_GATE_SPEC,
  SCAFFOLDED_SPIKE_RESULTS,
  VERDICT_ARTEFACT_FILENAME,
  getCaptureDecision,
} from "./captureDecision";

describe("captureDecision artefact (consumed by issue 0099)", () => {
  test("app default is the PRD `webview` capture path, hardware validation pending", () => {
    // PRD 0016 default capture source; the « Listen » pipeline (0099) is
    // functional pre-spike rather than dead-on-arrival on the rust stub.
    expect(DEFAULT_CAPTURE_DECISION.path).toBe("webview");
    expect(DEFAULT_CAPTURE_DECISION.hardwarePending).toBe(true);
  });

  test("the AFK fallback path is derived from the real selector → rust (spike-failure)", () => {
    // What an on-device spike FAILURE would select; flip the default to this.
    expect(FALLBACK_CAPTURE_PATH).toBe(selectCapturePath(SCAFFOLDED_SPIKE_RESULTS));
    expect(FALLBACK_CAPTURE_PATH).toBe("rust");
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

  test("the persisted verdict records the spike-fallback path (the AFK selection)", () => {
    // The on-disk verdict is the spike's own (headless → rust) selection; the
    // app default is the optimistic PRD webview path. They legitimately differ.
    expect(loadVerdict().chosen_path).toBe(FALLBACK_CAPTURE_PATH);
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
