// SphereCanvas.tsx
// React wrapper around the WebGL2 sphere renderer.
// Owns: state crossfade (~250ms), color interpolation, audio sim while speaking,
// and the canvas2D glyph overlay (variant 5).
//
// Ported from Design Mockup/sphere.jsx (DO NOT modify the mockup).

import { useEffect, useRef, useState } from "react";
import { type SphereRenderer, type StateWeights, createSphereRenderer } from "./sphereShader";

export type SphereState = "idle" | "listen" | "think" | "speak" | "alert" | "error";
export type SphereTheme = "warm" | "cold";
export type SphereMood = "calm" | "normal";

export type SphereCanvasProps = {
  state: SphereState;
  variant: number;
  motion: number;
  glow: number;
  theme: SphereTheme;
  mood: SphereMood;
  audioLevel?: number;
  /** Live RMS of the outgoing TTS signal in [0, 1], read once per rAF tick.
   * Provided by `useAudioLevel` (issue #0033). When omitted (legacy callers
   * and tests), the canvas treats the level as a constant 0 — no oscillation,
   * no sinusoidal simulation. */
  audioLevelRef?: React.RefObject<number>;
};

type Rgb = [number, number, number];

const STATE_KEYS: readonly SphereState[] = ["idle", "listen", "think", "speak", "alert", "error"];

const THEMES: Record<SphereTheme, { bg: Rgb; accent: Rgb; accent2: Rgb }> = {
  cold: {
    bg: [0.008, 0.024, 0.055],
    accent: [0.0, 0.9, 1.0],
    accent2: [0.42, 0.71, 1.0],
  },
  warm: {
    bg: [0.039, 0.024, 0.024],
    accent: [1.0, 0.48, 0.27],
    accent2: [1.0, 0.71, 0.62],
  },
};

// Per-state calm-mood color tints (matches mockup's CSS --state-tint).
const CALM_STATE_TINTS: Record<SphereState, Rgb> = {
  idle: [0.61, 0.66, 0.71],
  listen: [0.56, 0.7, 0.78],
  think: [0.66, 0.61, 0.75],
  speak: [0.56, 0.73, 0.62],
  alert: [0.79, 0.65, 0.42],
  error: [0.75, 0.55, 0.55],
};

// Glyph alphabet for variant 5 overlay -- runes / math / katakana-ish chars.
const GLYPH_ALPHABET =
  "∆∇∮∯∰⊕⊗⊘⊙⊚⊛⊜⌬⌭⌮¶§†‡¦◊◇◈ハミヒーシナモニサワツオリアホテマケメエカキムユラゾネスタヌヘ0123456789אבגדהוCONSCIENCE";

function lerp(a: number, b: number, t: number): number {
  return a + (b - a) * t;
}

function lerp3(a: Rgb, b: Rgb, t: number): Rgb {
  return [lerp(a[0], b[0], t), lerp(a[1], b[1], t), lerp(a[2], b[2], t)];
}

function desaturate(rgb: Rgb, satKeep: number, tint: Rgb, tintMix: number): Rgb {
  const luminance = 0.299 * rgb[0] + 0.587 * rgb[1] + 0.114 * rgb[2];
  const mono: Rgb = [luminance, luminance, luminance];
  let out: Rgb = lerp3(rgb, mono, 1 - satKeep);
  if (tintMix > 0) out = lerp3(out, tint, tintMix);
  return out;
}

function drawGlyphOverlay(
  canvas: HTMLCanvasElement | null,
  variant: number,
  t: number,
  accent: Rgb,
): void {
  if (!canvas) return;
  const ctx = canvas.getContext("2d");
  if (!ctx) return;
  const dpr = Math.min(window.devicePixelRatio || 1, 2);
  const W = canvas.clientWidth * dpr;
  const H = canvas.clientHeight * dpr;
  if (canvas.width !== W) canvas.width = W;
  if (canvas.height !== H) canvas.height = H;

  ctx.clearRect(0, 0, W, H);
  if (variant !== 5) return;

  ctx.save();
  ctx.translate(W / 2, H / 2);
  const size = Math.min(W, H);
  const R = size * 0.22;

  const a0 = Math.round(accent[0] * 255);
  const a1 = Math.round(accent[1] * 255);
  const a2 = Math.round(accent[2] * 255);

  const N = 180;
  const phi = Math.PI * (3 - Math.sqrt(5));
  for (let i = 0; i < N; i++) {
    const y = 1 - (i / (N - 1)) * 2;
    const rad = Math.sqrt(1 - y * y);
    const th = phi * i + t * 0.18;
    const x = Math.cos(th) * rad;
    const z = Math.sin(th) * rad;
    const cx = Math.cos(t * 0.07);
    const sx = Math.sin(t * 0.07);
    const y2 = y * cx - z * sx;
    const z2 = y * sx + z * cx;
    const px = x * R;
    const py = y2 * R;
    const depth = (z2 + 1) / 2;
    if (z2 < -0.2) continue;

    const seed = (i * 13 + Math.floor(t * 2 + i * 0.5)) % GLYPH_ALPHABET.length;
    const ch = GLYPH_ALPHABET[seed];

    const fontSize = (10 + depth * 12) * dpr;
    ctx.font = `${fontSize}px "JetBrains Mono", monospace`;
    ctx.textAlign = "center";
    ctx.textBaseline = "middle";
    const alpha = 0.15 + depth * 0.65;
    const isHot = (i + Math.floor(t * 1.2)) % 17 === 0;
    if (isHot) {
      ctx.fillStyle = `rgba(255,255,255,${alpha})`;
      ctx.shadowColor = `rgba(${a0},${a1},${a2},1)`;
      ctx.shadowBlur = 14 * dpr;
    } else {
      ctx.fillStyle = `rgba(${a0},${a1},${a2},${alpha * 0.85})`;
      ctx.shadowBlur = 0;
    }
    ctx.fillText(ch, px, py);
  }
  ctx.restore();
}

export function SphereCanvas(props: SphereCanvasProps): JSX.Element {
  const { state, variant, motion, glow, theme, mood, audioLevel, audioLevelRef } = props;

  const canvasRef = useRef<HTMLCanvasElement | null>(null);
  const glyphRef = useRef<HTMLCanvasElement | null>(null);
  const rendererRef = useRef<SphereRenderer | null>(null);
  const rafRef = useRef<number>(0);
  const [webglFailed, setWebglFailed] = useState<boolean>(false);

  // Mutable refs so the rAF loop reads the latest props without re-installing.
  const targetStateRef = useRef<SphereState>(state);
  const variantRef = useRef<number>(variant);
  const motionRef = useRef<number>(motion);
  const glowRef = useRef<number>(glow);
  const moodRef = useRef<SphereMood>(mood);
  const audioLevelPropRef = useRef<number>(audioLevel ?? 0);
  // Capture the (optionally-passed) ref from props so the rAF loop can read
  // the live RMS without re-installing every render. Stored in a ref so swaps
  // are picked up immediately without restarting the loop.
  const externalAudioLevelRef = useRef<React.RefObject<number> | undefined>(audioLevelRef);
  useEffect(() => {
    externalAudioLevelRef.current = audioLevelRef;
  }, [audioLevelRef]);

  const stateWeightsRef = useRef<StateWeights>({
    idle: 1,
    listen: 0,
    think: 0,
    speak: 0,
    alert: 0,
    error: 0,
  });
  const audioRef = useRef<number>(0);
  const colorRef = useRef<{ bg: Rgb; accent: Rgb; accent2: Rgb }>({
    bg: [...THEMES.warm.bg] as Rgb,
    accent: [...THEMES.warm.accent] as Rgb,
    accent2: [...THEMES.warm.accent2] as Rgb,
  });

  useEffect(() => {
    targetStateRef.current = state;
  }, [state]);
  useEffect(() => {
    variantRef.current = variant;
  }, [variant]);
  useEffect(() => {
    motionRef.current = motion;
  }, [motion]);
  useEffect(() => {
    glowRef.current = glow;
  }, [glow]);
  useEffect(() => {
    moodRef.current = mood;
  }, [mood]);
  useEffect(() => {
    audioLevelPropRef.current = audioLevel ?? 0;
  }, [audioLevel]);

  // Mount the renderer once per theme (the renderer itself does not depend on
  // theme, but seeding colorRef to the new theme palette here keeps the
  // crossfade clean on theme switches).
  useEffect(() => {
    const canvas = canvasRef.current;
    if (!canvas) return;

    const renderer = createSphereRenderer(canvas);
    if (!renderer) {
      setWebglFailed(true);
      return;
    }
    rendererRef.current = renderer;

    // Seed colors from the current theme so we don't crossfade from black.
    const baseTheme = THEMES[theme];
    colorRef.current = {
      bg: [...baseTheme.bg] as Rgb,
      accent: [...baseTheme.accent] as Rgb,
      accent2: [...baseTheme.accent2] as Rgb,
    };

    const onResize = (): void => {
      const dpr = Math.min(window.devicePixelRatio || 1, 2);
      renderer.setSize(canvas.clientWidth, canvas.clientHeight, dpr);
    };
    onResize();
    window.addEventListener("resize", onResize);

    const start = performance.now();
    let lastT = start;

    const loop = (now: number): void => {
      const dt = Math.min((now - lastT) / 1000, 0.05);
      lastT = now;
      const t = (now - start) / 1000;

      const target = targetStateRef.current;

      // Audio: real tap from `useAudioLevel` when provided (issue #0033). The
      // legacy `audioLevel` number prop is honoured as a fallback for callers
      // that compute their own level (none in V1, but kept for compatibility).
      // No more sinusoidal simulation — silent input naturally produces ~0.
      const tap = externalAudioLevelRef.current?.current;
      const audioTarget = tap ?? audioLevelPropRef.current ?? 0;
      audioRef.current = lerp(audioRef.current, audioTarget, 0.25);

      // Crossfade state weights -- ~250ms to settle.
      const weights = stateWeightsRef.current;
      const rate = 1 - Math.exp(-dt * 4.5);
      for (const key of STATE_KEYS) {
        const targetVal = key === target ? 1 : 0;
        weights[key] = lerp(weights[key], targetVal, rate);
      }

      // Color targets (alert/error override the theme).
      const base = THEMES[theme];
      const targetBg: Rgb = base.bg;
      let targetAccent: Rgb = base.accent;
      let targetAccent2: Rgb = base.accent2;
      if (target === "alert") {
        targetAccent = [1.0, 0.7, 0.0];
        targetAccent2 = [1.0, 0.4, 0.0];
      } else if (target === "error") {
        targetAccent = [1.0, 0.24, 0.24];
        targetAccent2 = [1.0, 0.55, 0.4];
      }
      if (moodRef.current === "calm") {
        const tint = CALM_STATE_TINTS[target];
        targetAccent = desaturate(targetAccent, 0.55, tint, 0.35);
        targetAccent2 = desaturate(targetAccent2, 0.55, tint, 0.3);
      }
      const cRate = 1 - Math.exp(-dt * 3.0);
      colorRef.current.bg = lerp3(colorRef.current.bg, targetBg, cRate);
      colorRef.current.accent = lerp3(colorRef.current.accent, targetAccent, cRate);
      colorRef.current.accent2 = lerp3(colorRef.current.accent2, targetAccent2, cRate);

      renderer.render({
        time: t,
        variant: variantRef.current,
        motion: motionRef.current,
        glow: glowRef.current,
        accent: colorRef.current.accent,
        accent2: colorRef.current.accent2,
        bg: colorRef.current.bg,
        audio: audioRef.current,
        states: weights,
      });

      drawGlyphOverlay(glyphRef.current, variantRef.current, t, colorRef.current.accent);

      rafRef.current = requestAnimationFrame(loop);
    };
    rafRef.current = requestAnimationFrame(loop);

    return () => {
      cancelAnimationFrame(rafRef.current);
      window.removeEventListener("resize", onResize);
      rendererRef.current = null;
    };
  }, [theme]);

  if (webglFailed) {
    return (
      <div className="hud-error">
        WebGL2 required — open this app in a Chromium / WebKit recent build
      </div>
    );
  }

  return (
    <div className="sphere-stage">
      <canvas ref={canvasRef} className="sphere-canvas" />
      <canvas ref={glyphRef} className="glyph-overlay" />
    </div>
  );
}
