import { describe, expect, test } from "vitest";
import {
  shortTurnId,
  turnIdColor,
  turnIdHighlightBg,
  turnIdHighlightOutline,
  turnIdHue,
} from "./turnColor";

describe("turnIdHue", () => {
  test("same input yields the same hue (determinism)", () => {
    const id = "550e8400-e29b-41d4-a716-446655440000";
    expect(turnIdHue(id)).toBe(turnIdHue(id));
  });

  test("hue is always in [0, 360)", () => {
    const samples = [
      "",
      "a",
      "abc",
      "550e8400-e29b-41d4-a716-446655440000",
      "6ba7b810-9dad-11d1-80b4-00c04fd430c8",
      "00000000-0000-0000-0000-000000000000",
      "ffffffff-ffff-ffff-ffff-ffffffffffff",
    ];
    for (const s of samples) {
      const h = turnIdHue(s);
      expect(h).toBeGreaterThanOrEqual(0);
      expect(h).toBeLessThan(360);
      expect(Number.isInteger(h)).toBe(true);
    }
  });

  test("two distinct UUIDs land on distinct hues (high-probability spread)", () => {
    // Not a guarantee — but for any two real UUIDs we should not collide.
    // We assert spread across a batch: at least 16 distinct hues over 32 ids.
    const ids = Array.from(
      { length: 32 },
      (_, i) => `00000000-0000-0000-0000-0000000000${i.toString(16).padStart(2, "0")}`,
    );
    const hues = new Set(ids.map(turnIdHue));
    expect(hues.size).toBeGreaterThanOrEqual(16);
  });
});

describe("turnIdColor", () => {
  test("returns an hsl(...) css string with the right hue", () => {
    const id = "deadbeef-cafe-babe-feed-c0ffee123456";
    const expected = `hsl(${turnIdHue(id)}, 65%, 55%)`;
    expect(turnIdColor(id)).toBe(expected);
  });

  test("same input yields the same color", () => {
    const id = "abc123";
    expect(turnIdColor(id)).toBe(turnIdColor(id));
  });
});

describe("turnIdHighlightBg", () => {
  test("uses the same hue as the chip with low alpha", () => {
    const id = "abc123";
    expect(turnIdHighlightBg(id)).toBe(`hsla(${turnIdHue(id)}, 65%, 55%, 0.18)`);
  });
});

describe("turnIdHighlightOutline", () => {
  test("uses the same hue as the chip with brighter alpha", () => {
    const id = "abc123";
    expect(turnIdHighlightOutline(id)).toBe(`hsla(${turnIdHue(id)}, 65%, 55%, 0.65)`);
  });
});

describe("shortTurnId", () => {
  test("returns first 6 chars of a UUID-like input", () => {
    expect(shortTurnId("550e8400-e29b-41d4-a716-446655440000")).toBe("550e84");
  });

  test("returns full string when shorter than 6 chars", () => {
    expect(shortTurnId("abc")).toBe("abc");
    expect(shortTurnId("")).toBe("");
  });
});
