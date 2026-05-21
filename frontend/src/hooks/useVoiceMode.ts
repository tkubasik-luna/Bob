import { useCallback, useState } from "react";

/**
 * Session-only voice mode toggle. State lives in React memory (no localStorage),
 * so it resets on reload — that's by design for the MVP.
 */
export function useVoiceMode(): { voiceEnabled: boolean; toggle: () => void } {
  const [voiceEnabled, setVoiceEnabled] = useState(false);
  const toggle = useCallback(() => {
    setVoiceEnabled((v) => !v);
  }, []);
  return { voiceEnabled, toggle };
}
