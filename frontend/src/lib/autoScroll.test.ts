import { describe, expect, it } from "vitest";
import {
  AT_BOTTOM_THRESHOLD_PX,
  type ScrollMetrics,
  distanceFromBottom,
  isAtBottom,
  shouldAutoScroll,
} from "./autoScroll";

const metrics = (over: Partial<ScrollMetrics> = {}): ScrollMetrics => ({
  scrollTop: 0,
  clientHeight: 100,
  scrollHeight: 100,
  ...over,
});

describe("distanceFromBottom", () => {
  it("is 0 when content exactly fits (nothing to scroll)", () => {
    expect(
      distanceFromBottom(metrics({ scrollHeight: 100, clientHeight: 100, scrollTop: 0 })),
    ).toBe(0);
  });

  it("is the gap when scrolled up", () => {
    // 500 tall, 100 viewport, scrolled to top → 400px from bottom.
    expect(distanceFromBottom(metrics({ scrollHeight: 500, scrollTop: 0 }))).toBe(400);
  });

  it("is 0 when pinned to the very bottom", () => {
    expect(distanceFromBottom(metrics({ scrollHeight: 500, scrollTop: 400 }))).toBe(0);
  });
});

describe("isAtBottom", () => {
  it("true when pinned to the bottom", () => {
    expect(isAtBottom(metrics({ scrollHeight: 500, scrollTop: 400 }))).toBe(true);
  });

  it("true within the slack threshold", () => {
    expect(
      isAtBottom(metrics({ scrollHeight: 500, scrollTop: 400 - AT_BOTTOM_THRESHOLD_PX })),
    ).toBe(true);
  });

  it("false just beyond the slack threshold", () => {
    expect(
      isAtBottom(metrics({ scrollHeight: 500, scrollTop: 400 - AT_BOTTOM_THRESHOLD_PX - 1 })),
    ).toBe(false);
  });

  it("false when scrolled up to read back", () => {
    expect(isAtBottom(metrics({ scrollHeight: 500, scrollTop: 0 }))).toBe(false);
  });

  it("respects a custom threshold", () => {
    const m = metrics({ scrollHeight: 500, scrollTop: 350 }); // 50px from bottom
    expect(isAtBottom(m, 40)).toBe(false);
    expect(isAtBottom(m, 60)).toBe(true);
  });
});

describe("shouldAutoScroll", () => {
  it("scrolls when stuck to bottom and windowed", () => {
    expect(shouldAutoScroll({ stuckToBottom: true, expanded: false })).toBe(true);
  });

  it("does not scroll when the user scrolled up", () => {
    expect(shouldAutoScroll({ stuckToBottom: false, expanded: false })).toBe(false);
  });

  it("does not fight the user when expanded (voir tout)", () => {
    expect(shouldAutoScroll({ stuckToBottom: true, expanded: true })).toBe(false);
  });
});
