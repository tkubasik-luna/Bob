// useAudioLevel.ts
// React hook that exposes a ref tracking the live RMS (0..1) of the audio
// currently going through `audioPlayer`'s shared AnalyserNode.
//
// Shape:
//   - On mount we try to grab the analyser. If `audioPlayer` hasn't created an
//     AudioContext yet (no TTS has ever played), `getAnalyser()` returns null
//     and we wait. The retry trigger is the store's `speakingMsgId`: it flips
//     from null to a value exactly when the first chunk enqueues, which is
//     when the analyser becomes available. This is cheap and event-driven —
//     no polling loops on the React tree.
//   - Once we have an analyser we open a `requestAnimationFrame` loop that
//     calls `getByteTimeDomainData` each frame, computes the normalized RMS,
//     and writes it into a `useRef<number>`. Returning a ref (not state) keeps
//     the parent from re-rendering at 60fps — the consumer is `SphereCanvas`'s
//     own rAF loop, which reads `ref.current` once per frame.
//   - Hard deadline: if 30s after the first mount we still don't have an
//     analyser, we stop retrying silently. The ref stays at 0 → the sphere
//     just doesn't pulse.
//
// PRD: prd/0004-sphere-hud-ui.md — Issue: issues/0033-audio-level-sphere-reactivity.md

import { useEffect, useRef } from "react";
import { getAnalyser } from "../audio/audioPlayer";
import { useChatStore } from "../store/chatStore";

const FALLBACK_TIMEOUT_MS = 30_000;

/**
 * Subscribe to `audioPlayer`'s analyser and expose the live RMS as a mutable
 * ref. The ref is normalized to [0, 1] and updates on every animation frame
 * while audio is flowing. Returns 0 when no audio is playing or when the
 * analyser hasn't been initialised yet.
 */
export function useAudioLevel(): React.RefObject<number> {
  const levelRef = useRef<number>(0);
  // Used as a retry trigger for the analyser attach below. We don't *consume*
  // speakingMsgId beyond watching it flip; the actual analyser lookup is the
  // imperative `getAnalyser()` call inside the effect.
  const speakingMsgId = useChatStore((s) => s.speakingMsgId);

  // Survives across renders so the effect's retry attempts can share the same
  // deadline and the same animation handle.
  const mountedAtRef = useRef<number>(0);
  const rafIdRef = useRef<number>(0);
  const attachedRef = useRef<boolean>(false);

  useEffect(() => {
    // Reading the trigger value inside the effect body marks the intentional
    // dependency (biome's useExhaustiveDependencies otherwise considers
    // `speakingMsgId` unused). The value itself is irrelevant — we only care
    // that this effect re-runs every time it changes (null -> id at the start
    // of a TTS reply, when the analyser is guaranteed to exist).
    void speakingMsgId;

    if (mountedAtRef.current === 0) {
      mountedAtRef.current = performance.now();
    }

    // If we already have a running rAF loop, the dep change is a no-op.
    if (attachedRef.current) return;

    // Hard deadline: silently give up after 30s. The ref stays at 0.
    if (performance.now() - mountedAtRef.current >= FALLBACK_TIMEOUT_MS) {
      return;
    }

    const node = getAnalyser();
    if (!node) return;

    attachedRef.current = true;
    const buf = new Uint8Array(node.fftSize);

    const tick = (): void => {
      node.getByteTimeDomainData(buf);
      // RMS of the centered, normalized [-1, 1] signal.
      let sum = 0;
      for (let i = 0; i < buf.length; i++) {
        const v = buf[i] / 128 - 1;
        sum += v * v;
      }
      const rms = Math.sqrt(sum / buf.length);
      // Clamp defensively — Web Audio guarantees byte values 0..255 but a
      // misbehaving stub in tests could violate that.
      levelRef.current = rms > 1 ? 1 : rms < 0 ? 0 : rms;
      rafIdRef.current = requestAnimationFrame(tick);
    };
    rafIdRef.current = requestAnimationFrame(tick);

    return () => {
      // Effect cleanup runs on every speakingMsgId change. Only the unmount
      // path (or a teardown when attachedRef flips) needs to cancel.
      // We don't reset attachedRef here because we want the loop to stay
      // alive across re-renders — the loop reads from the analyser whether
      // or not audio is flowing (silent input → ~0 RMS naturally).
    };
  }, [speakingMsgId]);

  // Component-unmount cleanup: cancel the rAF loop exactly once.
  useEffect(() => {
    return () => {
      if (rafIdRef.current !== 0) {
        cancelAnimationFrame(rafIdRef.current);
        rafIdRef.current = 0;
      }
      attachedRef.current = false;
    };
  }, []);

  return levelRef;
}
