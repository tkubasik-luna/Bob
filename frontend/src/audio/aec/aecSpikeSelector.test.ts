import { describe, expect, test } from "vitest";
import {
  type CriterionResult,
  type CriterionStatus,
  SPIKE_CRITERION_IDS,
  type SpikeCriterionId,
  allCriteriaPass,
  buildSpikeVerdict,
  hasPendingCriteria,
  selectCapturePath,
} from "./aecSpikeSelector";

/** Build a full criteria-results triple from a {id: status} shorthand. */
function results(map: Partial<Record<SpikeCriterionId, CriterionStatus>>): CriterionResult[] {
  return SPIKE_CRITERION_IDS.map((id) => ({ id, status: map[id] ?? "pending" }));
}

const ALL_PASS = results({
  getusermedia_stream: "pass",
  aec_attenuation_25db: "pass",
  overlap_word_transcribed: "pass",
});

describe("selectCapturePath (AFK auto-fallback rule)", () => {
  test("all three pass → webview", () => {
    expect(selectCapturePath(ALL_PASS)).toBe("webview");
  });

  test.each(SPIKE_CRITERION_IDS)("a single failing criterion (%s) → rust", (failing) => {
    const r = results({
      getusermedia_stream: "pass",
      aec_attenuation_25db: "pass",
      overlap_word_transcribed: "pass",
    });
    const target = r.find((c) => c.id === failing);
    if (target) target.status = "fail";
    expect(selectCapturePath(r)).toBe("rust");
  });

  test.each(SPIKE_CRITERION_IDS)("a single pending criterion (%s) → rust", (pending) => {
    const r = results({
      getusermedia_stream: "pass",
      aec_attenuation_25db: "pass",
      overlap_word_transcribed: "pass",
    });
    const target = r.find((c) => c.id === pending);
    if (target) target.status = "pending";
    expect(selectCapturePath(r)).toBe("rust");
  });

  test("all pending → rust (the headless default)", () => {
    expect(selectCapturePath(results({}))).toBe("rust");
  });

  test("a missing criterion → rust (not silently webview)", () => {
    const partial: CriterionResult[] = [
      { id: "getusermedia_stream", status: "pass" },
      { id: "aec_attenuation_25db", status: "pass" },
      // overlap_word_transcribed absent
    ];
    expect(selectCapturePath(partial)).toBe("rust");
  });

  test("order-independent", () => {
    const reversed = [...ALL_PASS].reverse();
    expect(selectCapturePath(reversed)).toBe("webview");
  });
});

describe("allCriteriaPass / hasPendingCriteria", () => {
  test("allCriteriaPass mirrors webview selection", () => {
    expect(allCriteriaPass(ALL_PASS)).toBe(true);
    expect(allCriteriaPass(results({ getusermedia_stream: "pass" }))).toBe(false);
  });

  test("hasPendingCriteria detects any pending", () => {
    expect(hasPendingCriteria(ALL_PASS)).toBe(false);
    expect(hasPendingCriteria(results({ getusermedia_stream: "pass" }))).toBe(true);
  });
});

describe("buildSpikeVerdict (shape + decision)", () => {
  const FIXED = new Date("2026-06-07T12:00:00.000Z");

  test("all-pass verdict: ok=true, chosen_path=webview, not hardware_pending", () => {
    const v = buildSpikeVerdict(ALL_PASS, FIXED);
    expect(v).toMatchObject({
      schema_version: 1,
      produced_at: "2026-06-07T12:00:00.000Z",
      ok: true,
      chosen_path: "webview",
      hardware_pending: false,
    });
    expect(v.criteria).toHaveLength(3);
  });

  test("a fail flips ok=false and chosen_path=rust", () => {
    const v = buildSpikeVerdict(
      results({
        getusermedia_stream: "pass",
        aec_attenuation_25db: "fail",
        overlap_word_transcribed: "pass",
      }),
      FIXED,
    );
    expect(v.ok).toBe(false);
    expect(v.chosen_path).toBe("rust");
    expect(v.hardware_pending).toBe(false); // measured, just failed
  });

  test("pending criteria mark hardware_pending and select rust", () => {
    const v = buildSpikeVerdict(results({}), FIXED);
    expect(v.ok).toBe(false);
    expect(v.chosen_path).toBe("rust");
    expect(v.hardware_pending).toBe(true);
  });

  test("missing criteria are materialised as pending in order", () => {
    const v = buildSpikeVerdict([{ id: "getusermedia_stream", status: "pass" }], FIXED);
    expect(v.criteria.map((c) => c.id)).toEqual(SPIKE_CRITERION_IDS);
    expect(v.criteria[0].status).toBe("pass");
    expect(v.criteria[1].status).toBe("pending");
    expect(v.criteria[2].status).toBe("pending");
    expect(v.criteria[1].detail).toBe("not evaluated");
  });

  test("verdict is JSON-serialisable round-trip", () => {
    const v = buildSpikeVerdict(ALL_PASS, FIXED);
    const round = JSON.parse(JSON.stringify(v));
    expect(round).toEqual(v);
  });
});
