import { describe, expect, it } from "vitest";
import type { Task, TaskState } from "../types/ws";
import { type OrbChatSnapshot, type OrbState, deriveOrbState } from "./orbState";

// Mirrors the test style of `sphere/useSphereState.test.ts` (table-driven over
// the reachable input space) and `lib/agentPhase.test.ts` (pure fn, plain
// inputs → expected output). No store / renderHook: `deriveOrbState` is a pure
// function, so we call it directly.

// A quiet, connected chat — the neutral baseline each case perturbs.
const QUIET: OrbChatSnapshot = {
  connectionStatus: "open",
  isWaitingResponse: false,
  speakingMsgId: null,
  isStreamingResponse: false,
};

let taskSeq = 0;
/** Minimal `Task` in a given state — only `state` matters to the reducer, but
 * the type requires the rest, so we fill plausible values. */
function task(state: TaskState): Task {
  taskSeq += 1;
  return {
    id: `t-${taskSeq}`,
    title: `task ${taskSeq}`,
    goal: "",
    state,
    createdAt: new Date(0).toISOString(),
  };
}

/** Build a `Record<id, Task>` from a list of states (matches `chatStore.tasks`). */
function tasks(...states: TaskState[]): Record<string, Task> {
  const out: Record<string, Task> = {};
  for (const s of states) {
    const t = task(s);
    out[t.id] = t;
  }
  return out;
}

describe("deriveOrbState — mood ladder (pure)", () => {
  const cases: Array<{
    name: string;
    chat: OrbChatSnapshot;
    tasks: Record<string, Task>;
    expected: OrbState;
  }> = [
    // 7 — repos: connected, nothing happening → breathing idle.
    { name: "repos → idle", chat: QUIET, tasks: {}, expected: "idle" },

    // 6 — réflexion: Bob awaiting its own response, no tasks → think.
    {
      name: "réflexion (isWaitingResponse) → think",
      chat: { ...QUIET, isWaitingResponse: true },
      tasks: {},
      expected: "think",
    },

    // 5 — délégation: a sub-task is running in the background → listen.
    {
      name: "délégation (a running task) → listen",
      chat: QUIET,
      tasks: tasks("running"),
      expected: "listen",
    },
    {
      name: "délégation (a pending task) → listen",
      chat: QUIET,
      tasks: tasks("pending"),
      expected: "listen",
    },
    {
      name: "délégation with a done task alongside still → listen",
      chat: QUIET,
      tasks: tasks("done", "running"),
      expected: "listen",
    },

    // 4 — réponse en streaming: speaking, or reply text flowing → speak.
    {
      name: "réponse streaming (speakingMsgId) → speak",
      chat: { ...QUIET, speakingMsgId: "msg-1" },
      tasks: {},
      expected: "speak",
    },
    {
      name: "réponse streaming (isStreamingResponse) → speak",
      chat: { ...QUIET, isStreamingResponse: true },
      tasks: {},
      expected: "speak",
    },

    // 3 — alerte: a hand is blocked waiting for input.
    {
      name: "waiting_input task → alert",
      chat: QUIET,
      tasks: tasks("waiting_input"),
      expected: "alert",
    },

    // 2 — erreur: a delegated hand failed.
    {
      name: "failed task → error",
      chat: QUIET,
      tasks: tasks("failed"),
      expected: "error",
    },

    // 1 — erreur: socket down dominates everything.
    {
      name: "connectionStatus closed → error",
      chat: { ...QUIET, connectionStatus: "closed" },
      tasks: {},
      expected: "error",
    },
    {
      name: "connectionStatus connecting → error",
      chat: { ...QUIET, connectionStatus: "connecting" },
      tasks: {},
      expected: "error",
    },

    // ── priority ordering (each higher rule masks the lower one) ──
    {
      name: "socket down beats a running task (error > listen)",
      chat: { ...QUIET, connectionStatus: "closed" },
      tasks: tasks("running"),
      expected: "error",
    },
    {
      name: "failed task beats a running task (error > listen)",
      chat: QUIET,
      tasks: tasks("running", "failed"),
      expected: "error",
    },
    {
      name: "waiting_input beats a running task (alert > listen)",
      chat: QUIET,
      tasks: tasks("running", "waiting_input"),
      expected: "alert",
    },
    {
      name: "speaking beats a running task (speak > listen)",
      chat: { ...QUIET, speakingMsgId: "msg-2" },
      tasks: tasks("running"),
      expected: "speak",
    },
    {
      name: "running task beats a bare think → listen (delegation in flight)",
      chat: { ...QUIET, isWaitingResponse: true },
      tasks: tasks("running"),
      expected: "listen",
    },
    {
      name: "still-thinking masks a trailing TTS tail → think (not speak)",
      chat: { ...QUIET, isWaitingResponse: true, speakingMsgId: "msg-3" },
      tasks: {},
      expected: "think",
    },
  ];

  it.each(cases)("$name", ({ chat, tasks, expected }) => {
    expect(deriveOrbState(chat, tasks).state).toBe(expected);
  });
});

describe("deriveOrbState — energy", () => {
  // Energy is the active phase's intensity. We don't pin exact magic numbers in
  // the production table here (that would just restate the impl); instead we
  // assert the load-bearing INVARIANTS the orb relies on:
  //   - every energy is a normalized [0,1] number,
  //   - idle (rest) is the calmest,
  //   - think / speak (the vivid working phases) burn brighter than idle.
  const moods: Array<{ chat: OrbChatSnapshot; tasks: Record<string, Task>; state: OrbState }> = [
    { chat: QUIET, tasks: {}, state: "idle" },
    { chat: { ...QUIET, isWaitingResponse: true }, tasks: {}, state: "think" },
    { chat: QUIET, tasks: tasks("running"), state: "listen" },
    { chat: { ...QUIET, speakingMsgId: "m" }, tasks: {}, state: "speak" },
    { chat: QUIET, tasks: tasks("waiting_input"), state: "alert" },
    { chat: QUIET, tasks: tasks("failed"), state: "error" },
  ];

  it("returns a normalized [0,1] energy for every mood", () => {
    for (const m of moods) {
      const { state, energy } = deriveOrbState(m.chat, m.tasks);
      expect(state).toBe(m.state);
      expect(energy).toBeGreaterThanOrEqual(0);
      expect(energy).toBeLessThanOrEqual(1);
    }
  });

  it("idle is the calmest; think and speak burn brighter than idle", () => {
    const idle = deriveOrbState(QUIET, {}).energy;
    const think = deriveOrbState({ ...QUIET, isWaitingResponse: true }, {}).energy;
    const speak = deriveOrbState({ ...QUIET, speakingMsgId: "m" }, {}).energy;
    expect(think).toBeGreaterThan(idle);
    expect(speak).toBeGreaterThan(idle);
  });
});
