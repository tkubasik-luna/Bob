import { act, renderHook } from "@testing-library/react";
import { beforeEach, describe, expect, test } from "vitest";
import { useChatStore } from "../store/chatStore";
import type { ConnectionStatus } from "../types/ws";
import { type SphereDerivedState, deriveSphereState, useSphereState } from "./useSphereState";

// Snapshot the pristine store so each test starts from a clean slate. Same
// pattern as the HudTasks test suite — the store is a shared singleton so
// neighbouring tests would otherwise bleed state.
const initialState = useChatStore.getState();

function setStore(patch: {
  connectionStatus?: ConnectionStatus;
  isWaitingResponse?: boolean;
  speakingMsgId?: string | null;
}): void {
  useChatStore.setState(patch);
}

describe("deriveSphereState (pure)", () => {
  // Table-driven over every reachable triple of (connectionStatus,
  // isWaitingResponse, speakingMsgId) → expected state. The matrix is
  // intentionally exhaustive on the boolean / null dimensions and samples
  // each `ConnectionStatus` variant at least once so the rule "non-open ⇒
  // error" is locked.
  const cases: Array<{
    connectionStatus: ConnectionStatus;
    isWaitingResponse: boolean;
    speakingMsgId: string | null;
    expected: SphereDerivedState;
  }> = [
    // Rule 4 — idle fallback (everything quiet, WS open).
    { connectionStatus: "open", isWaitingResponse: false, speakingMsgId: null, expected: "idle" },

    // Rule 3 — speak takes precedence over idle.
    {
      connectionStatus: "open",
      isWaitingResponse: false,
      speakingMsgId: "msg-1",
      expected: "speak",
    },

    // Rule 2 — think takes precedence over speak and idle.
    { connectionStatus: "open", isWaitingResponse: true, speakingMsgId: null, expected: "think" },
    {
      connectionStatus: "open",
      isWaitingResponse: true,
      speakingMsgId: "msg-2",
      expected: "think",
    },

    // Rule 1 — error overrides everything when WS is not "open".
    {
      connectionStatus: "closed",
      isWaitingResponse: false,
      speakingMsgId: null,
      expected: "error",
    },
    {
      connectionStatus: "closed",
      isWaitingResponse: true,
      speakingMsgId: "msg-3",
      expected: "error",
    },
    {
      connectionStatus: "connecting",
      isWaitingResponse: false,
      speakingMsgId: null,
      expected: "error",
    },
    {
      connectionStatus: "connecting",
      isWaitingResponse: true,
      speakingMsgId: "msg-4",
      expected: "error",
    },
  ];

  test.each(cases)(
    "($connectionStatus, waiting=$isWaitingResponse, speakingMsgId=$speakingMsgId) → $expected",
    ({ connectionStatus, isWaitingResponse, speakingMsgId, expected }) => {
      expect(deriveSphereState({ connectionStatus, isWaitingResponse, speakingMsgId })).toBe(
        expected,
      );
    },
  );
});

describe("useSphereState (hook)", () => {
  beforeEach(() => {
    // Restore the whole store (not just the three fields) so toasts, tasks,
    // and any other neighbouring state can't leak across tests.
    useChatStore.setState(initialState, true);
  });

  test("returns 'error' on mount because the default store status is 'connecting'", () => {
    // Sanity check: the pristine store ships with connectionStatus="connecting"
    // (see chatStore.ts), which falls through rule 1 to "error". Locking this
    // catches accidental flips of the default.
    const { result } = renderHook(() => useSphereState());
    expect(result.current).toBe("error");
  });

  test("returns 'idle' when WS is open and nothing else is happening", () => {
    act(() => {
      setStore({ connectionStatus: "open", isWaitingResponse: false, speakingMsgId: null });
    });
    const { result } = renderHook(() => useSphereState());
    expect(result.current).toBe("idle");
  });

  test("transitions idle → think when setWaiting(true) is called", () => {
    act(() => {
      setStore({ connectionStatus: "open", isWaitingResponse: false, speakingMsgId: null });
    });
    const { result } = renderHook(() => useSphereState());
    expect(result.current).toBe("idle");

    act(() => {
      useChatStore.getState().setWaiting(true);
    });
    expect(result.current).toBe("think");
  });

  test("transitions think → speak when waiting clears and speakingMsgId is set", () => {
    act(() => {
      setStore({ connectionStatus: "open", isWaitingResponse: true, speakingMsgId: null });
    });
    const { result } = renderHook(() => useSphereState());
    expect(result.current).toBe("think");

    act(() => {
      useChatStore.getState().setWaiting(false);
      useChatStore.getState().setSpeakingMsgId("msg-42");
    });
    expect(result.current).toBe("speak");
  });

  test("transitions speak → idle when speakingMsgId clears", () => {
    act(() => {
      setStore({ connectionStatus: "open", isWaitingResponse: false, speakingMsgId: "msg-42" });
    });
    const { result } = renderHook(() => useSphereState());
    expect(result.current).toBe("speak");

    act(() => {
      useChatStore.getState().setSpeakingMsgId(null);
    });
    expect(result.current).toBe("idle");
  });

  test("error overrides think: connectionStatus='closed' + isWaitingResponse=true → 'error'", () => {
    act(() => {
      setStore({ connectionStatus: "closed", isWaitingResponse: true, speakingMsgId: null });
    });
    const { result } = renderHook(() => useSphereState());
    expect(result.current).toBe("error");
  });

  test("error overrides speak: connectionStatus='closed' + speakingMsgId set → 'error'", () => {
    act(() => {
      setStore({ connectionStatus: "closed", isWaitingResponse: false, speakingMsgId: "msg-9" });
    });
    const { result } = renderHook(() => useSphereState());
    expect(result.current).toBe("error");
  });

  test("recovers from error → idle when connectionStatus flips back to 'open'", () => {
    act(() => {
      setStore({ connectionStatus: "closed", isWaitingResponse: false, speakingMsgId: null });
    });
    const { result } = renderHook(() => useSphereState());
    expect(result.current).toBe("error");

    act(() => {
      useChatStore.getState().setStatus("open");
    });
    expect(result.current).toBe("idle");
  });
});
