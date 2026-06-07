import { act, renderHook, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, test, vi } from "vitest";
import type { ClientMessage } from "../types/ws";
import { setCaptureDecisionOverride } from "./aec/captureDecision";
import { MIC_FRAME_TAG } from "./micDownsample";
import { useMicCapture } from "./useMicCapture";

// --- Web Audio / getUserMedia mocks (jsdom has none of these) ---------------

type PortMessageHandler = (event: { data: Float32Array }) => void;

class FakeAudioWorkletNode {
  port = {
    onmessage: null as PortMessageHandler | null,
    postMessage: vi.fn(),
  };
  connect = vi.fn();
  disconnect = vi.fn();
}

class FakeAudioContext {
  sampleRate = 48_000;
  audioWorklet = { addModule: vi.fn().mockResolvedValue(undefined) };
  createMediaStreamSource = vi.fn(() => ({ connect: vi.fn(), disconnect: vi.fn() }));
  close = vi.fn().mockResolvedValue(undefined);
}

let lastNode: FakeAudioWorkletNode | null = null;
let stoppedTracks = 0;

function installMocks(): void {
  lastNode = null;
  stoppedTracks = 0;
  const track = {
    stop: () => {
      stoppedTracks += 1;
    },
  };
  const stream = { getTracks: () => [track] } as unknown as MediaStream;

  vi.stubGlobal("navigator", {
    mediaDevices: { getUserMedia: vi.fn().mockResolvedValue(stream) },
  });
  vi.stubGlobal("performance", { now: () => 1234 });
  vi.stubGlobal("AudioContext", FakeAudioContext as unknown as typeof AudioContext);
  vi.stubGlobal("AudioWorkletNode", function (this: unknown, ..._args: unknown[]) {
    const node = new FakeAudioWorkletNode();
    lastNode = node;
    return node;
  } as unknown as typeof AudioWorkletNode);
}

describe("useMicCapture", () => {
  beforeEach(() => {
    installMocks();
    setCaptureDecisionOverride(null);
  });
  afterEach(() => {
    vi.unstubAllGlobals();
    setCaptureDecisionOverride(null);
  });

  test("arms on enable: getUserMedia + voice_start", async () => {
    const sent: ClientMessage[] = [];
    const send = (m: ClientMessage) => sent.push(m);
    const sendBinary = vi.fn();

    renderHook(() => useMicCapture({ enabled: true, send, sendBinary }));

    await waitFor(() => {
      expect(sent.some((m) => m.type === "voice_start")).toBe(true);
    });
    const start = sent.find((m) => m.type === "voice_start");
    expect(start).toMatchObject({ type: "voice_start", window: "new" });
    expect(navigator.mediaDevices.getUserMedia).toHaveBeenCalledOnce();
  });

  test("worklet frame → tagged binary frame on sendBinary", async () => {
    const send = vi.fn();
    const sendBinary = vi.fn();
    renderHook(() => useMicCapture({ enabled: true, send, sendBinary }));

    await waitFor(() => expect(lastNode).not.toBeNull());
    // Simulate the worklet posting a 30 ms @ 48 kHz block.
    const block = new Float32Array(1440).fill(0.25);
    act(() => {
      lastNode?.port.onmessage?.({ data: block });
    });

    expect(sendBinary).toHaveBeenCalledOnce();
    const frame = sendBinary.mock.calls[0][0] as ArrayBuffer;
    const bytes = new Uint8Array(frame);
    expect(bytes[0]).toBe(MIC_FRAME_TAG);
    // 30 ms @ 16 kHz = 480 samples * 2 bytes + 1 tag byte.
    expect(bytes.byteLength).toBe(480 * 2 + 1);
  });

  test("disarms on disable: voice_stop + tracks stopped", async () => {
    const sent: ClientMessage[] = [];
    const send = (m: ClientMessage) => sent.push(m);
    const sendBinary = vi.fn();

    const { rerender } = renderHook(
      ({ enabled }: { enabled: boolean }) => useMicCapture({ enabled, send, sendBinary }),
      { initialProps: { enabled: true } },
    );
    await waitFor(() => expect(sent.some((m) => m.type === "voice_start")).toBe(true));

    rerender({ enabled: false });
    await waitFor(() => expect(sent.some((m) => m.type === "voice_stop")).toBe(true));
    expect(stoppedTracks).toBeGreaterThan(0);
  });

  test("rust capture decision leaves the seam (no getUserMedia, no voice_start)", async () => {
    setCaptureDecisionOverride("rust");
    const sent: ClientMessage[] = [];
    renderHook(() =>
      useMicCapture({ enabled: true, send: (m) => sent.push(m), sendBinary: vi.fn() }),
    );
    // Give any async start a tick; it must NOT run for the rust path.
    await new Promise((r) => setTimeout(r, 10));
    expect(navigator.mediaDevices.getUserMedia).not.toHaveBeenCalled();
    expect(sent.some((m) => m.type === "voice_start")).toBe(false);
  });

  test("does nothing while disabled", () => {
    const send = vi.fn();
    renderHook(() => useMicCapture({ enabled: false, send, sendBinary: vi.fn() }));
    expect(navigator.mediaDevices.getUserMedia).not.toHaveBeenCalled();
    expect(send).not.toHaveBeenCalled();
  });
});
