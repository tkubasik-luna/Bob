// useSphereState.ts
// Pure hook that derives the sphere's high-level visual state from the chat
// store. No side-effects, no refs, no effects: it only reads three fields and
// applies a fixed priority ladder so the same store snapshot always maps to the
// same state. The result feeds the `state` prop of `SphereCanvas` (which then
// crossfades internally).
//
// Priority (highest → lowest):
//   1. `connectionStatus !== "open"`  → "error"
//   2. `isWaitingResponse === true`   → "think"
//   3. `speakingMsgId !== null`       → "speak"
//   4. otherwise                       → "idle"
//
// Note: the wider `SphereCanvas` state union also includes `listen` and
// `alert`, but neither is reachable in V1 (no speech-to-text yet, no proactive
// alerts). This hook intentionally narrows to the four states V1 actually uses.
//
// PRD: prd/0004-sphere-hud-ui.md — Issue: issues/0029-use-sphere-state-derive.md

import { useChatStore } from "../store/chatStore";

export type SphereDerivedState = "idle" | "think" | "speak" | "error";

/**
 * Read-only selector applied to a `ChatState`-shaped snapshot. Extracted so we
 * can unit-test the priority ladder without going through `renderHook`, and
 * so the hook itself stays a one-liner over `useChatStore`.
 */
export function deriveSphereState(snapshot: {
  connectionStatus: string;
  isWaitingResponse: boolean;
  speakingMsgId: string | null;
}): SphereDerivedState {
  if (snapshot.connectionStatus !== "open") return "error";
  if (snapshot.isWaitingResponse) return "think";
  if (snapshot.speakingMsgId !== null) return "speak";
  return "idle";
}

/**
 * Subscribe to the three store fields that drive the sphere's visual state
 * and return the derived state. The selector is structurally narrow so the
 * hook only re-renders when one of those three fields changes.
 */
export function useSphereState(): SphereDerivedState {
  return useChatStore((s) =>
    deriveSphereState({
      connectionStatus: s.connectionStatus,
      isWaitingResponse: s.isWaitingResponse,
      speakingMsgId: s.speakingMsgId,
    }),
  );
}
