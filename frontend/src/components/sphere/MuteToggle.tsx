// MuteToggle.tsx
// Small glyph button in the bottom-right that toggles TTS playback. Clicking
// it flips `useVoiceMode().voiceEnabled`; the icon swaps between a regular
// speaker (voice on) and a barred speaker (voice off / muted) so the state is
// readable at a glance. A global `M` keydown listener mirrors the click for
// hands-on-keyboard muting.
//
// The keydown listener lives in this component (not in `SphereUI`) so the
// composition root stays slim. The mockup gates state-forcing shortcuts on
// `INPUT` / `TEXTAREA` focus so typing letters in the chat doesn't trigger
// stray actions — we re-use that pattern here via `document.activeElement`.
//
// PRD: prd/0004-sphere-hud-ui.md — Issue: issues/0034-mute-toggle.md

import { useEffect } from "react";
import { useVoiceMode } from "../../hooks/useVoiceMode";

export function MuteToggle() {
  const { voiceEnabled, toggle } = useVoiceMode();

  // Global `M` shortcut. The listener is attached to `window` so a user
  // pressing `M` anywhere in the app fires the toggle, except when an input
  // is focused (typing "m" mid-message must not mute the assistant). We read
  // the latest `toggle` via the dep array — `useVoiceMode` returns a stable
  // callback, so the effect re-binds at most when React swaps the hook
  // identity (e.g. fast-refresh in dev).
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key !== "m" && e.key !== "M") return;
      const active = document.activeElement;
      if (active instanceof HTMLInputElement || active instanceof HTMLTextAreaElement) return;
      toggle();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [toggle]);

  return (
    <button
      type="button"
      className="hud-mute"
      onClick={toggle}
      aria-pressed={voiceEnabled}
      aria-label={voiceEnabled ? "Désactiver la voix" : "Activer la voix"}
      title={voiceEnabled ? "Désactiver la voix" : "Activer la voix"}
    >
      {voiceEnabled ? <SpeakerIcon /> : <SpeakerMutedIcon />}
    </button>
  );
}

/** Regular speaker glyph — voice is on, TTS will play. */
function SpeakerIcon() {
  return (
    <svg
      xmlns="http://www.w3.org/2000/svg"
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth="1.5"
      strokeLinecap="round"
      strokeLinejoin="round"
      width="18"
      height="18"
      aria-hidden="true"
      data-testid="speaker-on-icon"
    >
      <path d="M11 5 6 9H2v6h4l5 4z" />
      <path d="M15.54 8.46a5 5 0 0 1 0 7.07" />
      <path d="M19.07 4.93a10 10 0 0 1 0 14.14" />
    </svg>
  );
}

/** Barred speaker glyph — voice is off / muted, TTS will be skipped. */
function SpeakerMutedIcon() {
  return (
    <svg
      xmlns="http://www.w3.org/2000/svg"
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth="1.5"
      strokeLinecap="round"
      strokeLinejoin="round"
      width="18"
      height="18"
      aria-hidden="true"
      data-testid="speaker-off-icon"
    >
      <path d="M11 5 6 9H2v6h4l5 4z" />
      <line x1="22" y1="9" x2="16" y2="15" />
      <line x1="16" y1="9" x2="22" y2="15" />
    </svg>
  );
}
