import { createContext, useContext } from "react";
import type { ClientMessage } from "../../types/ws";

/**
 * Carries the `send` function produced by `useChatWsBridge` down to the
 * `?ui=new` subtree so leaf components (`InputField`, …) can dispatch frames
 * without each opening their own WS connection. Defaults to a no-op so
 * accidental consumption outside the provider is safe (the textarea simply
 * won't send anything) — production code always wraps in `<Provider>`.
 *
 * Issue: issues/0030-input-field-transcript-line.md (follow-up wiring).
 */
export const SphereWsContext = createContext<(msg: ClientMessage) => void>(() => undefined);

/**
 * Consumer helper — returns the current `send` function. Components should
 * use this rather than reading `useContext(SphereWsContext)` directly so the
 * dependency on the context surface is centralised here.
 */
export function useSphereSend(): (msg: ClientMessage) => void {
  return useContext(SphereWsContext);
}
