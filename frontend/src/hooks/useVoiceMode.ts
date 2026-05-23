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
  // Default ON — most user prompts expect Bob to speak. Mute is the explicit
  // gesture (button bottom-right or `M` key). Backend session default still
  // boots at `voice_mode=false` to be safe; the frontend re-syncs on every
  // WS `open` (`useChatWsBridge`) so the backend flips to true within ms.
  voiceEnabled: true,
  toggle: () => set((s) => ({ voiceEnabled: !s.voiceEnabled })),
}));

export function useVoiceMode(): { voiceEnabled: boolean; toggle: () => void } {
  const voiceEnabled = useVoiceModeStore((s) => s.voiceEnabled);
  const toggle = useVoiceModeStore((s) => s.toggle);
  return { voiceEnabled, toggle };
}
