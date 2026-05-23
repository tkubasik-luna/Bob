import { act, renderHook } from "@testing-library/react";
import { beforeEach, describe, expect, test } from "vitest";
import { useVoiceMode } from "./useVoiceMode";

/**
 * Regression: `useVoiceMode` used to keep state inside a `useState` closure so
 * every consumer (ChatView, InputField, MuteToggle) ran with an isolated copy.
 * Toggling the MuteToggle never reached InputField, so `voice: true` never
 * landed on the WS frame and the backend never synthesised audio. Now the
 * hook reads from a shared Zustand store — these tests lock that in.
 */
describe("useVoiceMode — shared state across mounts", () => {
  beforeEach(() => {
    // Reset to default. Mount-and-toggle until back to `false` so other test
    // files that already imported the module don't bleed state across files.
    const { result, unmount } = renderHook(() => useVoiceMode());
    if (result.current.voiceEnabled) {
      act(() => {
        result.current.toggle();
      });
    }
    unmount();
  });

  test("two mounts see the same voiceEnabled value", () => {
    const a = renderHook(() => useVoiceMode());
    const b = renderHook(() => useVoiceMode());

    expect(a.result.current.voiceEnabled).toBe(false);
    expect(b.result.current.voiceEnabled).toBe(false);

    act(() => {
      a.result.current.toggle();
    });

    expect(a.result.current.voiceEnabled).toBe(true);
    expect(b.result.current.voiceEnabled).toBe(true);
  });

  test("toggle from one consumer flips the other", () => {
    const a = renderHook(() => useVoiceMode());
    const b = renderHook(() => useVoiceMode());

    act(() => {
      b.result.current.toggle();
    });
    expect(a.result.current.voiceEnabled).toBe(true);
    expect(b.result.current.voiceEnabled).toBe(true);

    act(() => {
      a.result.current.toggle();
    });
    expect(a.result.current.voiceEnabled).toBe(false);
    expect(b.result.current.voiceEnabled).toBe(false);
  });
});
