// DevControls.tsx
// Dev-mode HUD overlay surfaced when the URL carries `?dev=1`. Composes:
//   - `.state-pills` (six pills, keys 1-6) — clicking forces the sphere state
//     via `devTweaksStore.forcedState`, overriding the `useSphereState`
//     derivation in `SphereUI`. Active pill = `.on` class with accent tone.
//   - tweaks panel — motion / glow sliders, state / variant / mood / theme
//     selects, autoCycle toggle. All read/write `useDevTweaksStore`.
//   - global keyboard handler — keys 1..6 force the matching state, skipping
//     when the user is focused inside an `<input>` / `<textarea>` so typing
//     "1234" into the chat input doesn't strobe the sphere. Keys 7..9 / a..f
//     are reserved for the future surface picker (out of scope V1).
//
// In production (no `?dev=1`) the component renders `null` and skips the
// keyboard listener entirely — no overhead, no chance of accidental state
// forcing in the wild.
//
// PRD: prd/0004-sphere-hud-ui.md — Issue: issues/0035-dev-controls-gated.md

import { useEffect } from "react";
import {
  type DevForcedSphereState,
  type DevTweaksMood,
  type DevTweaksTheme,
  useDevTweaksStore,
} from "../../state/devTweaksStore";

/** Order of the state pills — matches mockup `app.jsx` keyboard mapping
 * (`1=idle, 2=listen, 3=think, 4=speak, 5=alert, 6=error`). The same array
 * doubles as the autoCycle rotation order. */
const STATE_ORDER: readonly DevForcedSphereState[] = [
  "idle",
  "listen",
  "think",
  "speak",
  "alert",
  "error",
];

/** Variant name aliases — index → mockup label. Used by the variant
 * `<select>` so a developer reads "liquid" instead of "0". */
const VARIANT_NAMES: readonly string[] = ["liquid", "swarm", "wire", "plasma", "void", "glyph"];

/** Tone applied to the pill's `.on` accent — mockup mirrors `STATE_META`.
 * idle is `calm` (default accent), warn/err diverge so the active pill
 * matches the global theme it would trigger on the sphere. */
function pillTone(state: DevForcedSphereState): "calm" | "active" | "warn" | "err" {
  switch (state) {
    case "idle":
      return "calm";
    case "listen":
    case "think":
    case "speak":
      return "active";
    case "alert":
      return "warn";
    case "error":
      return "err";
  }
}

function isDevQuery(): boolean {
  if (typeof window === "undefined") return false;
  return new URLSearchParams(window.location.search).get("dev") === "1";
}

/**
 * Dev-mode UI overlay. Returns `null` outside `?dev=1` — the early bail keeps
 * the keyboard listener un-attached in production so user typing doesn't
 * race against `INPUT` / `TEXTAREA` focus checks.
 */
export function DevControls() {
  if (!isDevQuery()) {
    return null;
  }
  return <DevControlsActive />;
}

/** Internal component — only mounted when dev mode is on. Splitting it out
 * lets us keep the keyboard listener and autoCycle effect un-conditional
 * inside `DevControlsActive`, which keeps React's hook-order rules happy
 * (a single `useEffect` per render path, no conditional hooks). */
function DevControlsActive() {
  const forcedState = useDevTweaksStore((s) => s.forcedState);
  const motion = useDevTweaksStore((s) => s.motion);
  const glow = useDevTweaksStore((s) => s.glow);
  const variant = useDevTweaksStore((s) => s.variant);
  const mood = useDevTweaksStore((s) => s.mood);
  const theme = useDevTweaksStore((s) => s.theme);
  const autoCycle = useDevTweaksStore((s) => s.autoCycle);
  const setForcedState = useDevTweaksStore((s) => s.setForcedState);
  const setMotion = useDevTweaksStore((s) => s.setMotion);
  const setGlow = useDevTweaksStore((s) => s.setGlow);
  const setVariant = useDevTweaksStore((s) => s.setVariant);
  const setMood = useDevTweaksStore((s) => s.setMood);
  const setTheme = useDevTweaksStore((s) => s.setTheme);
  const setAutoCycle = useDevTweaksStore((s) => s.setAutoCycle);

  // Global keyboard shortcuts — keys 1..6 force the matching state. The
  // `tagName` guard mirrors the mockup so typing "1" into the chat input
  // doesn't switch the sphere to `idle`.
  useEffect(() => {
    const STATE_KEY_MAP: Record<string, DevForcedSphereState> = {
      "1": "idle",
      "2": "listen",
      "3": "think",
      "4": "speak",
      "5": "alert",
      "6": "error",
    };
    const onKey = (e: KeyboardEvent) => {
      const target = e.target;
      if (target instanceof HTMLInputElement || target instanceof HTMLTextAreaElement) return;
      const next = STATE_KEY_MAP[e.key];
      if (next) setForcedState(next);
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [setForcedState]);

  // AutoCycle — when toggled on, cycle `forcedState` through `STATE_ORDER`
  // every 4.5s starting from the current state (or `idle` if none forced).
  // Clearing the interval on toggle-off / unmount is critical, otherwise the
  // sphere keeps strobing after the dev panel is hidden.
  //
  // We read the entry forcedState via `getState()` instead of closing over
  // the `forcedState` from `useDevTweaksStore((s) => s.forcedState)` so the
  // effect doesn't restart on every state hop (which would otherwise be a
  // missing-dep warning).
  useEffect(() => {
    if (!autoCycle) return;
    const entry = useDevTweaksStore.getState().forcedState;
    let i = entry ? STATE_ORDER.indexOf(entry) : 0;
    if (i < 0) i = 0;
    const id = window.setInterval(() => {
      i = (i + 1) % STATE_ORDER.length;
      setForcedState(STATE_ORDER[i]);
    }, 4500);
    return () => window.clearInterval(id);
  }, [autoCycle, setForcedState]);

  return (
    <>
      <div className="state-pills">
        {STATE_ORDER.map((s, i) => (
          <button
            key={s}
            type="button"
            className={`pill ${forcedState === s ? "on" : ""} tone-${pillTone(s)}`}
            onClick={() => setForcedState(s)}
            aria-label={`Force state ${s}`}
            data-state={s}
          >
            <span className="pill-key">{i + 1}</span>
            <span className="pill-name">{s}</span>
          </button>
        ))}
      </div>

      <div className="twk-panel" data-noncommentable="">
        <div className="twk-hd">
          <b>Tweaks</b>
        </div>
        <div className="twk-body">
          <div className="twk-sect">AI State</div>
          <div className="twk-row">
            <div className="twk-lbl">
              <span>State</span>
            </div>
            <select
              className="twk-field"
              value={forcedState ?? "idle"}
              onChange={(e) => setForcedState(e.target.value as DevForcedSphereState)}
              aria-label="State"
            >
              {STATE_ORDER.map((s) => (
                <option key={s} value={s}>
                  {s}
                </option>
              ))}
            </select>
          </div>

          <div className="twk-row twk-row-h">
            <div className="twk-lbl">
              <span>Auto-cycle</span>
            </div>
            <button
              type="button"
              className="twk-toggle"
              data-on={autoCycle ? "1" : "0"}
              role="switch"
              aria-checked={autoCycle}
              aria-label="Auto-cycle"
              onClick={() => setAutoCycle(!autoCycle)}
            >
              <i />
            </button>
          </div>

          <div className="twk-sect">Sphere</div>
          <div className="twk-row">
            <div className="twk-lbl">
              <span>Motion</span>
              <span className="twk-val">{motion.toFixed(2)}</span>
            </div>
            <input
              type="range"
              className="twk-slider"
              min={0}
              max={1}
              step={0.01}
              value={motion}
              onChange={(e) => setMotion(Number(e.target.value))}
              aria-label="Motion"
            />
          </div>
          <div className="twk-row">
            <div className="twk-lbl">
              <span>Glow</span>
              <span className="twk-val">{glow.toFixed(2)}</span>
            </div>
            <input
              type="range"
              className="twk-slider"
              min={0}
              max={1}
              step={0.01}
              value={glow}
              onChange={(e) => setGlow(Number(e.target.value))}
              aria-label="Glow"
            />
          </div>
          <div className="twk-row">
            <div className="twk-lbl">
              <span>Variant</span>
            </div>
            <select
              className="twk-field"
              value={String(variant)}
              onChange={(e) => setVariant(Number(e.target.value))}
              aria-label="Variant"
            >
              {VARIANT_NAMES.map((name, idx) => (
                <option key={name} value={idx}>
                  {idx} · {name}
                </option>
              ))}
            </select>
          </div>

          <div className="twk-sect">Aesthetic</div>
          <div className="twk-row">
            <div className="twk-lbl">
              <span>Mood</span>
            </div>
            <select
              className="twk-field"
              value={mood}
              onChange={(e) => setMood(e.target.value as DevTweaksMood)}
              aria-label="Mood"
            >
              <option value="calm">calm</option>
              <option value="normal">normal</option>
            </select>
          </div>
          <div className="twk-row">
            <div className="twk-lbl">
              <span>Theme</span>
            </div>
            <select
              className="twk-field"
              value={theme}
              onChange={(e) => setTheme(e.target.value as DevTweaksTheme)}
              aria-label="Theme"
            >
              <option value="warm">warm</option>
              <option value="cold">cold</option>
            </select>
          </div>

          <div className="twk-hint">
            <div>
              <b>Keys:</b> 1-6 force state
            </div>
          </div>
        </div>
      </div>
    </>
  );
}
