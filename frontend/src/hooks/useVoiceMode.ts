import { create } from "zustand";

/**
 * Session-only voice mode toggle. State lives in a tiny Zustand store so all
 * call sites (`ChatView`, `InputField`, `MuteToggle`) share the SAME flag —
 * with `useState` each consumer kept its own isolated copy, so toggling the
 * mute button in the sphere window never changed what `InputField` sent over
 * WS and the backend never received `voice: true`. No persistence on purpose:
 * voice resets on reload, matching the MVP behaviour.
 */
type VoiceModeStore = {
  voiceEnabled: boolean;
  toggle: () => void;
};

const useVoiceModeStore = create<VoiceModeStore>((set) => ({
  voiceEnabled: false,
  toggle: () => set((s) => ({ voiceEnabled: !s.voiceEnabled })),
}));

export function useVoiceMode(): { voiceEnabled: boolean; toggle: () => void } {
  const voiceEnabled = useVoiceModeStore((s) => s.voiceEnabled);
  const toggle = useVoiceModeStore((s) => s.toggle);
  return { voiceEnabled, toggle };
}
