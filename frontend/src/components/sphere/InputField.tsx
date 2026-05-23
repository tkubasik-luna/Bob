import { type KeyboardEvent, useState } from "react";
import { useVoiceMode } from "../../hooks/useVoiceMode";
import { useChatStore } from "../../store/chatStore";
import { useSphereSend } from "./sphereWsContext";

/**
 * Fixed-bottom HUD-style text input for `?ui=new`. Mirrors the submit path
 * already used by `ChatView`: append the user message to the store, then
 * `send({ type: "user_msg", content, voice? })` over the WS hook. The
 * textarea is single-line by default and grows up to 4 rows when the user
 * inserts newlines with `Shift+Enter`.
 *
 * The `send` function is supplied via `SphereWsContext` so the connection
 * lives at the top of the `?ui=new` tree (`SphereUI` mounts the single
 * `useChatWsBridge`). The connection-status guard reads from the store —
 * `useChatWsBridge` mirrors the WS status onto it.
 *
 * No props: this component consumes the same global hooks as `ChatView`
 * (store, voice mode) plus the sphere-scoped context for `send`. Tests
 * inject behaviour by mocking those hooks at the module border (see
 * `InputField.test.tsx`).
 *
 * PRD: prd/0004-sphere-hud-ui.md — Issue: issues/0030-input-field-transcript-line.md
 */
export function InputField() {
  const [value, setValue] = useState("");
  const addUserMessage = useChatStore((s) => s.addUserMessage);
  const connectionStatus = useChatStore((s) => s.connectionStatus);
  const { voiceEnabled } = useVoiceMode();
  const send = useSphereSend();

  const submit = () => {
    const trimmed = value.trim();
    if (trimmed.length === 0) return;
    if (connectionStatus !== "open") return;
    addUserMessage(trimmed);
    send({ type: "user_msg", content: trimmed, ...(voiceEnabled ? { voice: true } : {}) });
    setValue("");
  };

  const onKeyDown = (e: KeyboardEvent<HTMLTextAreaElement>) => {
    if (e.key === "Enter" && !e.shiftKey) {
      // Plain Enter submits. Shift+Enter falls through so the textarea's
      // default behaviour inserts a newline.
      e.preventDefault();
      submit();
    }
  };

  return (
    <div className="hud-input">
      <textarea
        className="hud-input-field"
        rows={1}
        placeholder="Tapez pour parler à Bob"
        value={value}
        onChange={(e) => setValue(e.target.value)}
        onKeyDown={onKeyDown}
        aria-label="Message pour Bob"
      />
    </div>
  );
}
