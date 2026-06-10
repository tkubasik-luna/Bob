import { create } from "zustand";

/**
 * STT (mic listening) toggle — DISTINCT from the TTS voice toggle
 * (`useVoiceMode`). `useVoiceMode` decides whether Bob SPEAKS; this flag
 * decides whether Bob LISTENS (`useMicCapture` arming in `SphereUI`). They
 * used to be one flag, so muting Bob's voice also disarmed the mic — wrong
 * for a wake-word assistant where listening is ambient.
 *
 * PERSISTED in localStorage (unlike the session-only TTS toggle): the choice
 * is made on the SetupScreen gate, which shows on every launch, so it must
 * survive the reload into the HUD.
 */

export const STT_ENABLED_KEY = "bob.sttEnabled";

function readInitial(): boolean {
  try {
    return window.localStorage.getItem(STT_ENABLED_KEY) !== "0";
  } catch {
    return true;
  }
}

type SttModeStore = {
  sttEnabled: boolean;
  toggle: () => void;
};

const useSttModeStore = create<SttModeStore>((set) => ({
  // Default ON — the wake word (« Yo Bob ») is the product's front door.
  sttEnabled: readInitial(),
  toggle: () =>
    set((s) => {
      const next = !s.sttEnabled;
      try {
        window.localStorage.setItem(STT_ENABLED_KEY, next ? "1" : "0");
      } catch {
        // Persistence is best-effort; the in-memory flag still flips.
      }
      return { sttEnabled: next };
    }),
}));

export function useSttMode(): { sttEnabled: boolean; toggle: () => void } {
  const sttEnabled = useSttModeStore((s) => s.sttEnabled);
  const toggle = useSttModeStore((s) => s.toggle);
  return { sttEnabled, toggle };
}
