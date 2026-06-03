// conscienceLife.ts
// The "life engine". Each frame it integrates the involuntary signals that make
// the consciousness read as ALIVE rather than a status light:
//   • breath   — layered, period-wandering, asymmetric (fast in / slow out), with sighs
//   • gaze     — spring-tracked toward the cursor; wanders on its own when ignored
//   • attention— how engaged it is (rises on cursor movement & in listen/think/speak)
//   • blink    — involuntary, scheduled at irregular intervals (more when anxious)
//   • drift    — slow float so it is never perfectly centred
//   • wobble   — asymmetry amount, climbs with think/error
//   • state + colour melt — weights and palette ease so moods never snap
//
// Nothing here is perfectly periodic. Irregularity is the whole point.
//
// Ported VERBATIM (re-typed for TS) from `Design Mockup/conscience-life.js`
// (DO NOT modify the mockup). The mockup also simulated a `fake` voice envelope
// for speak; we keep that intact as a fallback, but the React wrapper overrides
// `audio` with the LIVE TTS RMS whenever it is flowing (PRD 0014: the voice
// modulates the orb during playback).
//
// PRD: prd/0014-hud-piste-3d-nacre.md — Issue: issues/0084-conscience-orb-orbstate-reducer.md

import type { ConscienceRenderParams, Rgb, StateWeights } from "./conscienceShader";

export type LifeState = "idle" | "listen" | "think" | "speak" | "alert" | "error";

export type LifePalette = {
  accent: Rgb;
  accent2: Rgb;
  accent3: Rgb;
  bg: Rgb;
};
export type LifePalettes = Record<LifeState, LifePalette>;

export const STATES: readonly LifeState[] = ["idle", "listen", "think", "speak", "alert", "error"];

function hex(h: string): Rgb {
  const s = h.replace("#", "");
  return [
    Number.parseInt(s.slice(0, 2), 16) / 255,
    Number.parseInt(s.slice(2, 4), 16) / 255,
    Number.parseInt(s.slice(4, 6), 16) / 255,
  ];
}

// Warm / organic DEFAULT palettes per state (Bob's warm family). The Nacre core
// overrides these via the `palettes` constructor option (rose/lavender). Kept
// verbatim from the mockup so the engine is usable on its own.
const PALETTES: LifePalettes = {
  idle: {
    accent: hex("#FF7A45"),
    accent2: hex("#FFB6A0"),
    accent3: hex("#FFE4D7"),
    bg: hex("#0A0605"),
  },
  listen: {
    accent: hex("#FF9A4E"),
    accent2: hex("#FFCDA0"),
    accent3: hex("#FFF0E2"),
    bg: hex("#0B0706"),
  },
  think: {
    accent: hex("#E86A8E"),
    accent2: hex("#FFB0C6"),
    accent3: hex("#FFE0EC"),
    bg: hex("#0B0608"),
  },
  speak: {
    accent: hex("#FFA255"),
    accent2: hex("#FFD8B0"),
    accent3: hex("#FFF2E2"),
    bg: hex("#0B0706"),
  },
  alert: {
    accent: hex("#FF6A24"),
    accent2: hex("#FFC24D"),
    accent3: hex("#FFE6B0"),
    bg: hex("#0C0604"),
  },
  error: {
    accent: hex("#E5443A"),
    accent2: hex("#FF8A6A"),
    accent3: hex("#FFD0C0"),
    bg: hex("#0C0504"),
  },
};

// Per-state breathing character: rate (Hz-ish), depth, jitter.
type BreathChar = { rate: number; depth: number; jitter: number };
const BREATH: Record<LifeState, BreathChar> = {
  idle: { rate: 0.17, depth: 1.0, jitter: 0.1 },
  listen: { rate: 0.13, depth: 0.45, jitter: 0.05 }, // shallow, almost held — attentive
  think: { rate: 0.26, depth: 0.7, jitter: 0.35 }, // irregular
  speak: { rate: 0.4, depth: 0.85, jitter: 0.2 },
  alert: { rate: 0.62, depth: 0.55, jitter: 0.3 }, // fast, shallow
  error: { rate: 0.5, depth: 0.65, jitter: 0.6 }, // erratic
};

function lerp(a: number, b: number, t: number): number {
  return a + (b - a) * t;
}
function lerp3(a: Rgb, b: Rgb, t: number): Rgb {
  return [lerp(a[0], b[0], t), lerp(a[1], b[1], t), lerp(a[2], b[2], t)];
}
function clamp(x: number, a: number, b: number): number {
  return Math.min(b, Math.max(a, x));
}
// tiny smooth noise from summed sines (deterministic, no allocs)
function snoise(x: number): number {
  return (
    0.5 +
    0.34 * Math.sin(x * 1.0) +
    0.12 * Math.sin(x * 2.37 + 1.3) +
    0.04 * Math.sin(x * 5.1 + 2.6)
  );
}

export type LifeVitals = {
  breath: number;
  attention: number;
  gaze: [number, number];
  bpm: number;
  mood: LifeState;
};

export type LifeUpdateOpts = {
  motion?: number;
  breathDepth?: number;
  gazeGain?: number;
};

/** Uniforms (minus the neb / tint block, which the wrapper eases + injects)
 * produced by `LifeEngine.uniforms`. */
export type LifeUniforms = Omit<ConscienceRenderParams, "neb" | "tint">;

/**
 * The frame-by-frame integrator of involuntary life signals. A direct TS port
 * of the mockup's `LifeEngine` (`Design Mockup/conscience-life.js`). All maths
 * is deterministic per `dt` so behaviour matches the mockup exactly.
 */
export class LifeEngine {
  private PAL: LifePalettes;
  private t = 0;
  private targetState: LifeState = "idle";
  weights: StateWeights = { idle: 1, listen: 0, think: 0, speak: 0, alert: 0, error: 0 };

  // colour (melts)
  private accent: Rgb;
  private accent2: Rgb;
  private accent3: Rgb;
  private bg: Rgb;

  // breath
  private breathPhase = 0; // 0..1 cycle
  private breath = 0; // 0..1 output
  private sighTimer = 4 + Math.random() * 5;
  private sigh = 0; // extra depth from an occasional deep breath

  // gaze (spring) — in -1..1 look space
  private gaze: [number, number] = [0, 0];
  private gazeVel: [number, number] = [0, 0];
  private gazeTarget: [number, number] = [0, 0];
  private mouse: [number, number] = [0, 0]; // normalised look target from cursor
  private mouseActive = 0; // decays; nonzero = cursor recently moved
  private saccadeTimer = 1 + Math.random() * 2; // when ignored, look around on its own
  private wanderTarget: [number, number] = [0, 0];

  // attention (spring scalar)
  private attention = 0;
  private attVel = 0;

  // blink (involuntary)
  private blink = 0;
  private blinkTimer = 1.5 + Math.random() * 3;
  private blinkPhase = -1; // -1 idle; else 0..1 progress

  // drift / floating
  private driftSeedX = Math.random() * 100;
  private driftSeedY = Math.random() * 100;
  private drift: [number, number] = [0, 0];

  private wobble = 0;

  // simulated audio for speak (fallback when no live TTS RMS is available)
  private audio = 0;

  // exposed vitals for the HUD
  vitals: LifeVitals = { breath: 0, attention: 0, gaze: [0, 0], bpm: 0, mood: "idle" };

  constructor(opts: { palettes?: LifePalettes } = {}) {
    this.PAL = opts.palettes || PALETTES;
    this.accent = this.PAL.idle.accent.slice() as unknown as Rgb;
    this.accent2 = this.PAL.idle.accent2.slice() as unknown as Rgb;
    this.accent3 = this.PAL.idle.accent3.slice() as unknown as Rgb;
    this.bg = this.PAL.idle.bg.slice() as unknown as Rgb;
  }

  setState(s: LifeState): void {
    if (STATES.includes(s)) this.targetState = s;
  }

  // mousePx: [x,y] in CSS px relative to canvas; size: {w,h}; null = no cursor
  setMouse(mousePx: [number, number] | null, size: { w: number; h: number }): void {
    if (!mousePx) {
      this.mouseActive = Math.max(0, this.mouseActive - 0.02);
      return;
    }
    const m = Math.min(size.w, size.h);
    const nx = (mousePx[0] - size.w / 2) / m;
    const ny = (mousePx[1] - size.h / 2) / m;
    const tx = clamp(nx * 2.2, -1, 1);
    const ty = clamp(ny * 2.2, -1, 1);
    const moved = Math.abs(tx - this.mouse[0]) + Math.abs(ty - this.mouse[1]);
    this.mouse = [tx, ty];
    if (moved > 0.002) this.mouseActive = 1;
  }

  update(rawDt: number, opts?: LifeUpdateOpts): void {
    // Clamp the frame delta (a backgrounded tab can hand us a huge `dt`); use a
    // local so we don't reassign the parameter (biome `noParameterAssign`).
    const dt = Math.min(rawDt, 0.05);
    this.t += dt;
    const motion = opts && opts.motion != null ? opts.motion : 0.6;
    const breathScale = opts && opts.breathDepth != null ? opts.breathDepth : 1;
    const gazeGain = opts && opts.gazeGain != null ? opts.gazeGain : 1;
    const target = this.targetState;

    // ---- state weight melt (slow, organic settle) ----
    const wr = 1 - Math.exp(-dt * 3.0);
    const blended = { rate: 0, depth: 0, jitter: 0 };
    for (const k of STATES) {
      this.weights[k] = lerp(this.weights[k], k === target ? 1 : 0, wr);
      const b = BREATH[k];
      blended.rate += this.weights[k] * b.rate;
      blended.depth += this.weights[k] * b.depth;
      blended.jitter += this.weights[k] * b.jitter;
    }

    // ---- colour melt ----
    const pal = this.PAL[target];
    const cr = 1 - Math.exp(-dt * 2.4);
    this.accent = lerp3(this.accent, pal.accent, cr);
    this.accent2 = lerp3(this.accent2, pal.accent2, cr);
    this.accent3 = lerp3(this.accent3, pal.accent3, cr);
    this.bg = lerp3(this.bg, pal.bg, cr);

    // ---- breathing (period wanders; inhale fast, exhale slow; occasional sigh) ----
    const periodNoise = 0.8 + 0.4 * snoise(this.t * 0.21); // wandering period
    const jitterN = (snoise(this.t * 1.7 + 10) - 0.5) * blended.jitter;
    const rate = blended.rate * periodNoise * (1 + jitterN);
    this.breathPhase = (this.breathPhase + dt * rate) % 1;
    // asymmetric breath curve: quick rise to 1, slow fall
    const ph = this.breathPhase;
    let shaped: number;
    if (ph < 0.4)
      shaped = Math.sin((ph / 0.4) * Math.PI * 0.5); // inhale
    else shaped = Math.cos(((ph - 0.4) / 0.6) * Math.PI * 0.5); // exhale
    // sighs
    this.sighTimer -= dt;
    if (this.sighTimer <= 0) {
      this.sigh = 1;
      this.sighTimer = 7 + Math.random() * 8;
    }
    this.sigh = Math.max(0, this.sigh - dt * 0.5);
    const depth = blended.depth * breathScale * (1 + this.sigh * 0.5);
    this.breath = clamp(shaped * depth, 0, 1.2);

    // ---- attention ----
    this.mouseActive = Math.max(0, this.mouseActive - dt * 0.6);
    let attTarget = 0.12; // baseline curiosity
    attTarget += this.weights.listen * 0.85;
    attTarget += this.weights.think * 0.45;
    attTarget += this.weights.speak * 0.55;
    attTarget += this.weights.alert * 0.7;
    attTarget += this.mouseActive * 0.55;
    attTarget = clamp(attTarget, 0, 1);
    // spring (muscular settle)
    const aStiff = 26;
    const aDamp = 8;
    this.attVel += (attTarget - this.attention) * aStiff * dt;
    this.attVel *= Math.exp(-aDamp * dt);
    this.attention = clamp(this.attention + this.attVel * dt, 0, 1);

    // ---- gaze target: follow cursor when engaged, else wander (saccades) ----
    const engaged = this.mouseActive > 0.15 || target === "listen" || target === "speak";
    if (engaged) {
      this.gazeTarget = [this.mouse[0] * gazeGain, this.mouse[1] * gazeGain];
    } else {
      this.saccadeTimer -= dt;
      if (this.saccadeTimer <= 0) {
        // involuntary glance somewhere new — quick, then dwell
        const a = Math.random() * Math.PI * 2;
        const rad = 0.25 + Math.random() * 0.5;
        this.wanderTarget = [Math.cos(a) * rad, Math.sin(a) * rad * 0.7];
        this.saccadeTimer = 1.4 + Math.random() * 3.2;
      }
      this.gazeTarget = this.wanderTarget;
    }
    // spring gaze — overshoot & settle, saccades snap faster than smooth pursuit
    const gStiff = engaged ? 60 : 110;
    const gDamp = engaged ? 12 : 16;
    for (let i = 0; i < 2; i++) {
      this.gazeVel[i] += (this.gazeTarget[i] - this.gaze[i]) * gStiff * dt;
      this.gazeVel[i] *= Math.exp(-gDamp * dt);
      this.gaze[i] = clamp(this.gaze[i] + this.gazeVel[i] * dt, -1.2, 1.2);
    }

    // ---- blink (involuntary; faster when alert/error) ----
    const blinkUrge = 1 + this.weights.alert * 2 + this.weights.error * 2.5;
    if (this.blinkPhase < 0) {
      this.blinkTimer -= dt * blinkUrge;
      if (this.blinkTimer <= 0) {
        this.blinkPhase = 0;
      }
    } else {
      this.blinkPhase += dt / 0.16; // ~160ms blink
      // close fast, open slightly slower
      const bp = this.blinkPhase;
      this.blink = bp < 0.45 ? bp / 0.45 : Math.max(0, 1 - (bp - 0.45) / 0.55);
      if (this.blinkPhase >= 1) {
        this.blinkPhase = -1;
        this.blink = 0;
        this.blinkTimer = 1.6 + Math.random() * 4.0; // irregular spacing
        if (Math.random() < 0.18) this.blinkTimer *= 0.3; // occasional double-blink
      }
    }

    // ---- drift / float ----
    this.drift = [
      (snoise(this.t * 0.13 + this.driftSeedX) - 0.5) * 0.018 * (0.5 + motion),
      (snoise(this.t * 0.11 + this.driftSeedY) - 0.5) * 0.014 * (0.5 + motion),
    ];

    // ---- wobble (asymmetry climbs with think/error) ----
    const wobTarget = 0.2 + this.weights.think * 0.5 + this.weights.error * 0.6;
    this.wobble = lerp(this.wobble, wobTarget, 1 - Math.exp(-dt * 2.0));

    // ---- voice-like amplitude (speak) — fallback when no live RMS is fed ----
    // Fast syllabic bursts + flutter so the body reads as if it's talking.
    const speaking = this.weights.speak;
    const tt = this.t;
    const syllable = (0.5 + 0.5 * Math.sin(tt * 9.0 + Math.sin(tt * 2.3))) ** 2.0; // ~syllable rate
    const flutter = 0.5 + 0.5 * Math.sin(tt * 31.0 + Math.sin(tt * 7.0) * 3.0); // fast vibrato
    const burst = Math.max(0, Math.sin(tt * 3.3) * Math.sin(tt * 8.1 + 1.0)); // word-like gating
    const env = syllable * (0.5 + 0.5 * flutter) * (0.35 + 0.85 * burst);
    const fake = speaking * Math.min(1, env * 1.4);
    this.audio = lerp(this.audio, fake, 0.6); // light smoothing → quick variation

    // ---- vitals readout ----
    this.vitals.breath = this.breath;
    this.vitals.attention = this.attention;
    this.vitals.gaze = [this.gaze[0], this.gaze[1]];
    this.vitals.bpm = Math.round(rate * 60); // "breaths per minute"-ish
    this.vitals.mood = target;
  }

  /**
   * Override the simulated speak envelope with a live audio level (TTS RMS in
   * [0,1]). PRD 0014: the voice modulates the orb during playback. When the
   * level is ~0 (silence / no audio) the engine keeps its own `fake` envelope
   * so the body still reads as breathing — we only take the live value when it
   * is genuinely louder than the simulation.
   */
  applyLiveAudio(level: number): void {
    if (level > this.audio) this.audio = level;
  }

  uniforms(form: number, motion: number, glow: number): LifeUniforms {
    return {
      time: this.t,
      form,
      motion,
      glow,
      accent: this.accent,
      accent2: this.accent2,
      accent3: this.accent3,
      bg: this.bg,
      audio: this.audio,
      breath: this.breath,
      gaze: this.gaze,
      attention: this.attention,
      blink: this.blink,
      drift: this.drift,
      wobble: this.wobble,
      states: this.weights,
    };
  }
}
