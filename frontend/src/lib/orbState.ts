// orbState.ts
// PURE reducer that derives the conscience orb's high-level mood + energy from
// the REAL app state (chat + sub-tasks). No UI imports, no refs, no effects,
// no store access: it takes two plain snapshots and applies a fixed priority
// ladder so the same inputs always map to the same `{ state, energy }`. The
// result feeds `ConscienceOrb` (which crossfades the mood internally) and the
// `--energy` CSS var (which drives the breathing halo intensity).
//
// This EXTENDS the existing four-state sphere derivation (`deriveSphereState`
// in `sphere/useSphereState.ts`) by folding in the sub-task map so Bob's
// delegation reads on the orb: tasks in flight ⇒ "listen" (the mockup's
// `delegate → listen` mood, see `Design Mockup/p3d-core.jsx` NEB_PHASE_TO_STATE),
// a streaming answer ⇒ "speak", a failed task / dropped socket ⇒ "error",
// a task awaiting input ⇒ "alert", reasoning in flight ⇒ "think", else "idle"
// (breathing at rest).
//
// Priority (highest → lowest):
//   1. `connectionStatus !== "open"`            → error   (socket down)
//   2. any task in state "failed"               → error   (a delegated hand failed)
//   3. any task in state "waiting_input"        → alert   (needs attention)
//   4. response streaming (speaking OR a live   → speak
//      `streamingAssistant`) and not mid-thought
//   5. any task in state "running" | "pending"  → listen  (delegation in flight)
//   6. `isWaitingResponse === true`             → think   (Bob is reasoning)
//   7. otherwise                                 → idle    (breathing)
//
// PRD: prd/0014-hud-piste-3d-nacre.md — Issue: issues/0084-conscience-orb-orbstate-reducer.md

import type { Task, TaskState } from "../types/ws";

/** The orb's mood. Same six-state union the nebula shader understands
 * (`Design Mockup/conscience-shader.js`); every state is reachable here
 * (unlike `deriveSphereState`, which narrowed to four). */
export type OrbState = "idle" | "listen" | "think" | "speak" | "alert" | "error";

/** Derived orb drive: the mood + a normalized [0,1] energy. `energy` is the
 * intensity of the active phase (idle is calm, think/speak are vivid), mirroring
 * the mockup's phase→energy mapping. It drives the halo / breathe amplitude. */
export type OrbDrive = {
  state: OrbState;
  energy: number;
};

/** The slice of chat state the orb cares about. A plain snapshot (not the
 * zustand store) so the reducer is trivially unit-testable — same shape
 * contract as `deriveSphereState`'s argument, plus the streaming flag so a
 * live answer reads as "speak" even before TTS audio starts. */
export type OrbChatSnapshot = {
  connectionStatus: string;
  isWaitingResponse: boolean;
  speakingMsgId: string | null;
  /** True while an assistant turn is streaming its answer (`streamingAssistant`
   * is non-null in `chatStore`). Lets the orb shift to "speak" the moment the
   * reply text starts flowing, not only once TTS audio begins. */
  isStreamingResponse: boolean;
};

/** Per-state energy — the active phase's intensity, fed to the orb's halo and
 * the `--energy` CSS var. Idle breathes calmly; think/speak burn brightest;
 * alert/error sit elevated. Values are deliberately in [0,1]. */
const STATE_ENERGY: Record<OrbState, number> = {
  idle: 0.25,
  listen: 0.55,
  think: 0.85,
  speak: 0.9,
  alert: 0.7,
  error: 0.65,
};

function hasTaskInState(tasks: Record<string, Task>, want: TaskState): boolean {
  for (const id in tasks) {
    if (tasks[id]?.state === want) return true;
  }
  return false;
}

/**
 * Pure derivation of the orb's `{ state, energy }` from a chat snapshot and the
 * sub-task map. Deterministic and side-effect free — see the priority ladder in
 * the file header. Extracted from any store / UI so it can be exercised directly
 * in tests, exactly like `deriveSphereState`.
 */
export function deriveOrbState(chat: OrbChatSnapshot, tasks: Record<string, Task>): OrbDrive {
  const state = deriveOrbMood(chat, tasks);
  return { state, energy: STATE_ENERGY[state] };
}

/** The mood half of the ladder, split out so the energy mapping stays a pure
 * table lookup and the priority order is easy to read in one place. */
function deriveOrbMood(chat: OrbChatSnapshot, tasks: Record<string, Task>): OrbState {
  // 1 — socket down dominates everything (matches `deriveSphereState`).
  if (chat.connectionStatus !== "open") return "error";
  // 2 — a delegated hand failed: surface the failure on the orb.
  if (hasTaskInState(tasks, "failed")) return "error";
  // 3 — a hand is blocked waiting for input: alert, not a hard error.
  if (hasTaskInState(tasks, "waiting_input")) return "alert";
  // 4 — the answer is streaming (TTS playing, or reply text flowing) and Bob is
  //     no longer mid-thought → speak. The `!isWaitingResponse` guard keeps a
  //     fresh "think" (Bob reasoning before the next answer chunk) from being
  //     masked by a still-playing tail of the previous utterance.
  if ((chat.speakingMsgId !== null || chat.isStreamingResponse) && !chat.isWaitingResponse) {
    return "speak";
  }
  // 5 — work is delegated and running in the background → listen / delegate mood.
  if (hasTaskInState(tasks, "running") || hasTaskInState(tasks, "pending")) {
    return "listen";
  }
  // 6 — Bob is reasoning (awaiting its own response) → think.
  if (chat.isWaitingResponse) return "think";
  // 7 — nothing happening: breathe at rest.
  return "idle";
}
