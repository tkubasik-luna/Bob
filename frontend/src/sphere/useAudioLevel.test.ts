import { act, renderHook } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, test, vi } from "vitest";
import * as audioPlayer from "../audio/audioPlayer";
import { useChatStore } from "../store/chatStore";
import { useAudioLevel } from "./useAudioLevel";

// jsdom doesn't ship `AudioContext`. `useAudioLevel` itself never instantiates
// one (it only calls `getAnalyser()` and reads from the returned node), but we
// keep a stub at the ready in case any future change widens the import surface.
type FakeAnalyser = Pick<AnalyserNode, "fftSize" | "getByteTimeDomainData">;

function makeAnalyserWithSquareWave(): FakeAnalyser {
  // Alternating 0 / 255 is a hard square wave — RMS of (x/128 - 1) hits the
  // bound at 1.0 across every sample, well above the > 0.5 threshold the
  // issue asks for. Using extremes avoids any sample-count edge effect.
  return {
    fftSize: 1024,
    getByteTimeDomainData: (array: Uint8Array): void => {
      for (let i = 0; i < array.length; i++) {
        array[i] = i % 2 === 0 ? 0 : 255;
      }
    },
  };
}

function makeAnalyserSilent(): FakeAnalyser {
  // A silent signal is 128 across the board (Web Audio encoding of 0.0). The
  // RMS should round-trip to 0.
  return {
    fftSize: 1024,
    getByteTimeDomainData: (array: Uint8Array): void => {
      array.fill(128);
    },
  };
}

/** Drive a synchronous rAF: each call to `requestAnimationFrame` runs the
 * callback immediately. This makes the rAF-driven loop testable without
 * waiting on the real frame clock. We bound recursion by counting ticks and
 * stopping after `maxTicks` so a stable loop doesn't blow the call stack. */
function installSyncRaf(maxTicks: number): { ticks: () => number; cancelCalls: () => number } {
  let ticks = 0;
  let cancelCalls = 0;
  vi.spyOn(globalThis, "requestAnimationFrame").mockImplementation(
    (cb: FrameRequestCallback): number => {
      ticks += 1;
      if (ticks <= maxTicks) {
        cb(performance.now());
      }
      return ticks;
    },
  );
  vi.spyOn(globalThis, "cancelAnimationFrame").mockImplementation((): void => {
    cancelCalls += 1;
  });
  return { ticks: () => ticks, cancelCalls: () => cancelCalls };
}

// Snapshot the pristine store so each test starts from a clean slate (same
// idiom as useSphereState.test.ts).
const initialState = useChatStore.getState();

describe("useAudioLevel", () => {
  beforeEach(() => {
    useChatStore.setState(initialState, true);
  });

  afterEach(() => {
    vi.restoreAllMocks();
  });

  test("stays at 0 when `getAnalyser()` returns null", () => {
    vi.spyOn(audioPlayer, "getAnalyser").mockReturnValue(null);
    // Even with rAF allowed, no analyser means no loop is ever started.
    installSyncRaf(5);

    const { result } = renderHook(() => useAudioLevel());
    expect(result.current.current).toBe(0);

    // Flipping speakingMsgId is the documented retry trigger — should still
    // not break the silent fallback.
    act(() => {
      useChatStore.getState().setSpeakingMsgId("msg-1");
    });
    expect(result.current.current).toBe(0);
  });

  test("ref reflects the live RMS of a strong signal", () => {
    vi.spyOn(audioPlayer, "getAnalyser").mockReturnValue(
      makeAnalyserWithSquareWave() as unknown as AnalyserNode,
    );
    installSyncRaf(1);

    const { result } = renderHook(() => useAudioLevel());

    // One synchronous rAF tick fires inside the effect → the ref is written.
    expect(result.current.current).toBeGreaterThan(0.5);
  });

  test("ref reads ~0 for a silent (DC) signal", () => {
    vi.spyOn(audioPlayer, "getAnalyser").mockReturnValue(
      makeAnalyserSilent() as unknown as AnalyserNode,
    );
    installSyncRaf(1);

    const { result } = renderHook(() => useAudioLevel());
    expect(result.current.current).toBe(0);
  });

  test("cancelAnimationFrame is called on unmount", () => {
    vi.spyOn(audioPlayer, "getAnalyser").mockReturnValue(
      makeAnalyserWithSquareWave() as unknown as AnalyserNode,
    );
    const raf = installSyncRaf(1);

    const { unmount } = renderHook(() => useAudioLevel());
    unmount();

    expect(raf.cancelCalls()).toBeGreaterThan(0);
  });

  test("retries attach when speakingMsgId flips and analyser becomes available", () => {
    const getAnalyserSpy = vi.spyOn(audioPlayer, "getAnalyser");
    // First call (mount): no analyser yet (audio hasn't played).
    getAnalyserSpy.mockReturnValueOnce(null);
    // After speakingMsgId flips, the analyser is ready.
    getAnalyserSpy.mockReturnValue(makeAnalyserWithSquareWave() as unknown as AnalyserNode);
    installSyncRaf(1);

    const { result } = renderHook(() => useAudioLevel());
    // No analyser at mount → no rAF loop → ref stays 0.
    expect(result.current.current).toBe(0);

    // Flip speakingMsgId to trigger the retry. Now the analyser is there
    // and the loop fires synchronously.
    act(() => {
      useChatStore.getState().setSpeakingMsgId("msg-42");
    });
    expect(result.current.current).toBeGreaterThan(0.5);
    // We attempted getAnalyser at least twice (mount + retry).
    expect(getAnalyserSpy).toHaveBeenCalledTimes(2);
  });
});
