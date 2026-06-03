// ConscienceOrb.tsx — React wrapper around the WebGL conscience renderer + life
// engine, tinted to the Piste 3D · Nacre screen (rose / lavender).
//
// Ported from `Design Mockup/conscience.jsx` (the rAF wrapper) + the `CoreNebula`
// case of `Design Mockup/p3d-core.jsx` (which picks FORM 3 — Nébuleuse — and
// feeds it the Nacre presets / palettes / glass tint). DO NOT modify the mockup.
//
// The wrapper owns the rAF loop: each frame it updates the `LifeEngine` (breath,
// gaze, attention, blink, drift, wobble, mood melt), eases the Nébuleuse params
// toward the active mood's preset (so transitions read as one organic body
// settling), integrates the monotonic orbital phase, and renders.
//
// TWO deviations from the mockup, both mandated by PRD 0014 / issue 0084:
//   1. The mood is DRIVEN from the real app state (the `state` prop, derived by
//      `lib/orbState.ts`) instead of the mockup's scripted phase.
//   2. The LIVE TTS RMS (`audioLevelRef`, from `useAudioLevel`) overrides the
//      life engine's simulated `audio` envelope while the voice is playing, so
//      the orb's rim halo + brume swing with the actual speech.
//
// WebGL2 is required (same as `SphereCanvas`); if the context can't be created
// we render the shared `.hud-error` banner instead of crashing.
//
// PRD: prd/0014-hud-piste-3d-nacre.md — Issue: issues/0084-conscience-orb-orbstate-reducer.md

import { useEffect, useRef, useState } from "react";
import { LifeEngine, type LifePalettes, type LifeState } from "./conscienceLife";
import {
  type ConscienceRenderer,
  type NebParams,
  type Rgb,
  createConscienceRenderer,
} from "./conscienceShader";

// ── Nacre presets (verbatim from p3d-core.jsx `CoreNebula` / NEB_PRESETS) ──
// Per-mood Nébuleuse parameters: comet count / speed / length / width, orbital
// altitude + equator flattening, glow, core glow, fog, IOR, rim, sphere zoom.
const NEB_PRESETS: Record<LifeState, Omit<NebParams, "orbitPhase" | "latitude">> = {
  idle: {
    trailCount: 4,
    trailSpeed: 0.22,
    trailLen: 1.2,
    trailWidth: 0.02,
    trailAlt: 1.06,
    equator: 0.12,
    trailGlow: 0.5,
    coreGlow: 0.7,
    fogAmt: 0.85,
    ior: 1.24,
    rim: 0.65,
    sphereSize: 1.0,
  },
  listen: {
    trailCount: 6,
    trailSpeed: 0.38,
    trailLen: 1.5,
    trailWidth: 0.02,
    trailAlt: 1.4,
    equator: 0.72,
    trailGlow: 0.8,
    coreGlow: 1.7,
    fogAmt: 0.95,
    ior: 1.25,
    rim: 0.85,
    sphereSize: 1.0,
  },
  think: {
    trailCount: 22,
    trailSpeed: 1.5,
    trailLen: 2.3,
    trailWidth: 0.017,
    trailAlt: 1.02,
    equator: 0.18,
    trailGlow: 1.0,
    coreGlow: 1.1,
    fogAmt: 1.1,
    ior: 1.3,
    rim: 0.8,
    sphereSize: 0.92,
  },
  speak: {
    trailCount: 10,
    trailSpeed: 0.85,
    trailLen: 1.8,
    trailWidth: 0.022,
    trailAlt: 1.12,
    equator: 0.28,
    trailGlow: 1.0,
    coreGlow: 1.9,
    fogAmt: 1.5,
    ior: 1.3,
    rim: 1.7,
    sphereSize: 1.0,
  },
  alert: {
    trailCount: 11,
    trailSpeed: 1.9,
    trailLen: 1.5,
    trailWidth: 0.02,
    trailAlt: 1.12,
    equator: 0.45,
    trailGlow: 1.05,
    coreGlow: 1.3,
    fogAmt: 1.0,
    ior: 1.28,
    rim: 1.1,
    sphereSize: 1.0,
  },
  error: {
    trailCount: 12,
    trailSpeed: 2.4,
    trailLen: 1.35,
    trailWidth: 0.021,
    trailAlt: 1.1,
    equator: 0.38,
    trailGlow: 1.1,
    coreGlow: 1.25,
    fogAmt: 1.05,
    ior: 1.28,
    rim: 1.2,
    sphereSize: 1.0,
  },
};

function hex(h: string): Rgb {
  const s = h.replace("#", "");
  return [
    Number.parseInt(s.slice(0, 2), 16) / 255,
    Number.parseInt(s.slice(2, 4), 16) / 255,
    Number.parseInt(s.slice(4, 6), 16) / 255,
  ];
}

// Palettes kept in the screen's rose/lavender family, shifting subtly per mood
// (verbatim from p3d-core.jsx NEB_PALETTES; idle matches the p3d.css `.piste`
// tokens `--accent #E7B4CB` / `--accent2 #C6A2DB` / `--accent3 #F1E3EC`).
const NEB_PALETTES: LifePalettes = {
  idle: {
    accent: hex("#E7B4CB"),
    accent2: hex("#C6A2DB"),
    accent3: hex("#F1E3EC"),
    bg: hex("#160F18"),
  },
  listen: {
    accent: hex("#D9A8D6"),
    accent2: hex("#BBA6E0"),
    accent3: hex("#F1E6F2"),
    bg: hex("#160F18"),
  },
  think: {
    accent: hex("#C6A2DB"),
    accent2: hex("#A98FD8"),
    accent3: hex("#ECE0F4"),
    bg: hex("#18101C"),
  },
  speak: {
    accent: hex("#ECB0C8"),
    accent2: hex("#D7A8D8"),
    accent3: hex("#F6E6EE"),
    bg: hex("#170F18"),
  },
  alert: {
    accent: hex("#E59AC0"),
    accent2: hex("#D08FC8"),
    accent3: hex("#F2DCE8"),
    bg: hex("#170E18"),
  },
  error: {
    accent: hex("#D77A9E"),
    accent2: hex("#C77FB0"),
    accent3: hex("#EFCEDD"),
    bg: hex("#180C16"),
  },
};

// Cool, faintly-lavender glass tint (verbatim from p3d-core.jsx NEB_TINT).
const NEB_TINT: Rgb = [0.95, 0.92, 0.98];

// Form 3 = NEBULEUSE (the finalized WebGL consciousness orb).
const NEB_FORM = 3;

export type ConscienceOrbProps = {
  /** The orb mood, derived from the real app state by `lib/orbState.ts`. */
  state: LifeState;
  /** Dev-tunable motion (0..1), from `devTweaksStore`. */
  motion: number;
  /** Dev-tunable glow (0..1), from `devTweaksStore`. */
  glow: number;
  /** Live TTS RMS in [0,1], read once per rAF tick. When flowing it overrides
   * the life engine's simulated speak envelope so the orb pulses with the real
   * voice. Optional (tests / legacy callers pass nothing → simulated envelope
   * only). */
  audioLevelRef?: React.RefObject<number>;
};

/** A full neb-param target for the given mood (presets carry no orbitPhase /
 * latitude; we fold in defaults so the eased copy has every numeric field). */
function nebTargetFor(state: LifeState): NebParams {
  const preset = NEB_PRESETS[state] ?? NEB_PRESETS.idle;
  return { ...preset, latitude: 0, orbitPhase: 0 };
}

export function ConscienceOrb(props: ConscienceOrbProps): JSX.Element {
  const { state, motion, glow, audioLevelRef } = props;

  const canvasRef = useRef<HTMLCanvasElement | null>(null);
  const rendererRef = useRef<ConscienceRenderer | null>(null);
  const lifeRef = useRef<LifeEngine | null>(null);
  const rafRef = useRef<number>(0);
  const [webglFailed, setWebglFailed] = useState<boolean>(false);

  // Mutable refs so the rAF loop reads the latest props without re-installing.
  const motionRef = useRef<number>(motion);
  const glowRef = useRef<number>(glow);
  // Target neb preset for the active mood; eased toward each frame.
  const nebTargetRef = useRef<NebParams>(nebTargetFor(state));
  const nebCurRef = useRef<NebParams | null>(null); // eased toward the target preset
  const orbitPhaseRef = useRef<number>(0); // accumulated orbital phase (monotonic)
  // Capture the optional TTS ref so the loop reads live RMS without re-installing.
  const audioRef = useRef<React.RefObject<number> | undefined>(audioLevelRef);
  // Latest mood, mirrored into a ref so the mount-once effect can SEED the life
  // engine from it without listing `state` as a dependency (which would force a
  // full renderer re-install on every mood change). Live changes flow through
  // the `[state]` effect below, not through a remount.
  const stateRef = useRef<LifeState>(state);

  useEffect(() => {
    motionRef.current = motion;
  }, [motion]);
  useEffect(() => {
    glowRef.current = glow;
  }, [glow]);
  useEffect(() => {
    audioRef.current = audioLevelRef;
  }, [audioLevelRef]);
  // Mood changes: retarget the neb preset AND tell the (already-mounted) life
  // engine to melt toward the new mood. Also keep `stateRef` current so a
  // remount (StrictMode double-invoke / hot reload) seeds from the live mood.
  useEffect(() => {
    stateRef.current = state;
    nebTargetRef.current = nebTargetFor(state);
    lifeRef.current?.setState(state);
  }, [state]);

  // Mount the renderer + life engine once. The rAF loop integrates life,
  // eases the neb params, advances the monotonic orbit phase, and renders.
  useEffect(() => {
    const canvas = canvasRef.current;
    if (!canvas) return;

    const renderer = createConscienceRenderer(canvas);
    if (!renderer) {
      setWebglFailed(true);
      return;
    }
    rendererRef.current = renderer;

    const life = new LifeEngine({ palettes: NEB_PALETTES });
    // Seed from the live mood via the ref (not the closed-over `state`) so this
    // effect stays mount-once with an empty dep list and no lint suppression.
    life.setState(stateRef.current);
    lifeRef.current = life;

    let mousePx: [number, number] | null = null;
    const size = { w: canvas.clientWidth, h: canvas.clientHeight };
    const onResize = (): void => {
      const dpr = Math.min(window.devicePixelRatio || 1, 2);
      size.w = canvas.clientWidth;
      size.h = canvas.clientHeight;
      renderer.setSize(size.w, size.h, dpr);
    };
    onResize();
    window.addEventListener("resize", onResize);

    const onMove = (e: PointerEvent): void => {
      mousePx = [e.clientX, e.clientY];
    };
    const onLeave = (): void => {
      mousePx = null;
    };
    window.addEventListener("pointermove", onMove);
    window.addEventListener("pointerleave", onLeave);

    let lastT = performance.now();

    const loop = (now: number): void => {
      const dt = (now - lastT) / 1000;
      lastT = now;

      life.setMouse(mousePx, size);
      life.update(dt, {
        motion: motionRef.current,
        breathDepth: 1.0,
        gazeGain: 1.0,
      });
      // PRD 0014 — the real voice modulates the orb: take the live TTS RMS when
      // it is louder than the simulated envelope.
      const tap = audioRef.current?.current;
      if (typeof tap === "number" && tap > 0) life.applyLiveAudio(tap);

      const u = life.uniforms(NEB_FORM, motionRef.current, glowRef.current);

      // Ease every neb parameter toward the active mood's preset so transitions
      // between idle / listen / think / … read as one organic body settling.
      const target = nebTargetRef.current;
      if (!nebCurRef.current) nebCurRef.current = { ...target };
      const cur = nebCurRef.current;
      const k = 1 - Math.exp(-dt / 0.55); // ~0.55s time-constant
      for (const key of Object.keys(target) as (keyof NebParams)[]) {
        if (key === "orbitPhase") continue; // integrated, not eased
        cur[key] = cur[key] + (target[key] - cur[key]) * k;
      }
      // integrate orbital phase from the (eased) speed so the satellites turn at
      // a steady rate and never reverse, even while the speed eases.
      orbitPhaseRef.current += (cur.trailSpeed || 0) * dt;
      cur.orbitPhase = orbitPhaseRef.current;

      renderer.render({ ...u, neb: cur, tint: NEB_TINT });

      rafRef.current = requestAnimationFrame(loop);
    };
    rafRef.current = requestAnimationFrame(loop);

    return () => {
      cancelAnimationFrame(rafRef.current);
      window.removeEventListener("resize", onResize);
      window.removeEventListener("pointermove", onMove);
      window.removeEventListener("pointerleave", onLeave);
      rendererRef.current = null;
      lifeRef.current = null;
    };
    // Mount-once: every live value (motion / glow / mood / audio) is read
    // through a ref inside the loop, so this effect has no reactive deps and the
    // renderer is never torn down + rebuilt while the orb is on screen.
  }, []);

  if (webglFailed) {
    return (
      <div className="hud-error">
        WebGL2 required — open this app in a Chromium / WebKit recent build
      </div>
    );
  }

  return <canvas ref={canvasRef} className="cv-canvas" />;
}
