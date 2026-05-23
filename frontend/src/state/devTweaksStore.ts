// devTweaksStore.ts
// Zustand slice that holds the dev-mode tweak values exposed by `DevControls`
// (`?dev=1`). The store is always active — in production the controls render
// nothing but `SphereUI` still reads the defaults from here, so the canvas
// mount is unconditional and we avoid branching on `?dev=1` in the render
// path. The locked V1 aesthetic (`warm + calm + liquid`) lives in the
// defaults; `DevControls` lets us flip them at runtime to inspect each
// (state, variant, mood, theme) combo without touching backend events.
//
// Persistence: every setter except `forcedState` writes a snapshot of the
// non-transient fields to `localStorage.bob_dev_tweaks`. The forced sphere
// state is intentionally NOT persisted — it's a transient override that
// belongs to the active session, not a tweak you'd want to survive reload.
// On store create we hydrate from localStorage if present (try/catch so
// malformed JSON or unavailable storage degrades to defaults).
//
// PRD: prd/0004-sphere-hud-ui.md — Issue: issues/0035-dev-controls-gated.md

import { create } from "zustand";

/**
 * Full state union for dev override. `useSphereState` only returns four of
 * these in production (`idle | think | speak | error`) — the extra `listen`
 * and `alert` exist so the dev pills can exercise every shader path defined
 * in `SphereCanvas` (which already understands all six).
 */
export type DevForcedSphereState = "idle" | "listen" | "think" | "speak" | "alert" | "error";

export type DevTweaksMood = "calm" | "normal";
export type DevTweaksTheme = "warm" | "cold";

/** Persisted shape — everything written to / read from localStorage. The
 * transient `forcedState` is deliberately absent so a reload re-enters the
 * production derivation. */
export type PersistedDevTweaks = {
  motion: number;
  glow: number;
  variant: number;
  mood: DevTweaksMood;
  theme: DevTweaksTheme;
  autoCycle: boolean;
};

export const DEV_TWEAKS_STORAGE_KEY = "bob_dev_tweaks";

/** V1 locked defaults — match `Design Mockup/app.jsx` TWEAK_DEFAULTS plus
 * the PRD's "warm + calm + liquid" lock. Keep in sync with `SphereUI`'s
 * hard-coded props prior to issue #0035. */
export const DEV_TWEAKS_DEFAULTS: PersistedDevTweaks = {
  motion: 0.55,
  glow: 0.7,
  variant: 0,
  mood: "calm",
  theme: "warm",
  autoCycle: false,
};

type DevTweaksState = PersistedDevTweaks & {
  forcedState: DevForcedSphereState | null;
  setForcedState: (state: DevForcedSphereState | null) => void;
  setMotion: (motion: number) => void;
  setGlow: (glow: number) => void;
  setVariant: (variant: number) => void;
  setMood: (mood: DevTweaksMood) => void;
  setTheme: (theme: DevTweaksTheme) => void;
  setAutoCycle: (autoCycle: boolean) => void;
};

/**
 * Read the persisted tweaks from localStorage, falling back to defaults on
 * any failure (no storage, malformed JSON, unexpected shape). Defensive —
 * dev tweaks should never crash the app.
 */
function hydrate(): PersistedDevTweaks {
  if (typeof window === "undefined") return DEV_TWEAKS_DEFAULTS;
  try {
    const raw = window.localStorage.getItem(DEV_TWEAKS_STORAGE_KEY);
    if (raw === null) return DEV_TWEAKS_DEFAULTS;
    const parsed = JSON.parse(raw) as Partial<PersistedDevTweaks>;
    // Merge field-by-field so a partial / outdated payload still hydrates
    // the fields it does carry without overwriting unknown ones.
    return {
      motion: typeof parsed.motion === "number" ? parsed.motion : DEV_TWEAKS_DEFAULTS.motion,
      glow: typeof parsed.glow === "number" ? parsed.glow : DEV_TWEAKS_DEFAULTS.glow,
      variant: typeof parsed.variant === "number" ? parsed.variant : DEV_TWEAKS_DEFAULTS.variant,
      mood:
        parsed.mood === "calm" || parsed.mood === "normal" ? parsed.mood : DEV_TWEAKS_DEFAULTS.mood,
      theme:
        parsed.theme === "warm" || parsed.theme === "cold"
          ? parsed.theme
          : DEV_TWEAKS_DEFAULTS.theme,
      autoCycle:
        typeof parsed.autoCycle === "boolean" ? parsed.autoCycle : DEV_TWEAKS_DEFAULTS.autoCycle,
    };
  } catch {
    return DEV_TWEAKS_DEFAULTS;
  }
}

function persist(snapshot: PersistedDevTweaks): void {
  if (typeof window === "undefined") return;
  try {
    window.localStorage.setItem(DEV_TWEAKS_STORAGE_KEY, JSON.stringify(snapshot));
  } catch {
    // No-op: storage quota, private mode, etc. — dev tweaks aren't critical.
  }
}

/** Project the persisted slice out of the full state — `forcedState` is
 * never serialised. */
function snapshotOf(state: DevTweaksState): PersistedDevTweaks {
  return {
    motion: state.motion,
    glow: state.glow,
    variant: state.variant,
    mood: state.mood,
    theme: state.theme,
    autoCycle: state.autoCycle,
  };
}

export const useDevTweaksStore = create<DevTweaksState>((set, get) => ({
  ...hydrate(),
  forcedState: null,
  setForcedState: (forcedState) => set({ forcedState }),
  setMotion: (motion) => {
    set({ motion });
    persist({ ...snapshotOf(get()), motion });
  },
  setGlow: (glow) => {
    set({ glow });
    persist({ ...snapshotOf(get()), glow });
  },
  setVariant: (variant) => {
    set({ variant });
    persist({ ...snapshotOf(get()), variant });
  },
  setMood: (mood) => {
    set({ mood });
    persist({ ...snapshotOf(get()), mood });
  },
  setTheme: (theme) => {
    set({ theme });
    persist({ ...snapshotOf(get()), theme });
  },
  setAutoCycle: (autoCycle) => {
    set({ autoCycle });
    persist({ ...snapshotOf(get()), autoCycle });
  },
}));
