# 0004 Sphere HUD UI

Shipped on 2026-05-23 from PRD `prd/0004-sphere-hud-ui.md`.

## What it does

Bob gains a second "Sphere" Tauri window (`Bob · Sphere`, 1280×800, borderless) that runs alongside the existing chat window (`Bob · Legacy`, 900×700). The Sphere window swaps the chat scroll for a full-screen WebGL2 sphere that breathes when idle, swirls when Jarvis is thinking, pulses with the live TTS RMS when Bob is speaking, and glitches when the WebSocket drops. Around that sphere sits a minimal HUD: a tasks panel top-right bound to the existing Jarvis task store, a single transcript line above a permanent text input field, an auto-opening markdown overlay for structured or multi-line replies, and a mute toggle (also bound to the `M` shortcut). The legacy `ChatView` chain stays intact in the Legacy window and remains the default at `?ui=legacy` — the new UI is opt-in via the `Sphere` window (`?ui=new`), with `?ui=new&dev=1` revealing state pills + a tweaks panel for variant/mood/theme/motion/glow.

## Technical surface

- **New Tauri window**: `tauri.conf.json` already exposes two windows (`legacy` + `new`). The `new` window adds `"decorations": false` plus a 28px CSS drag region declared in `hud.css`. OS shortcuts (Cmd+W/M/Q) keep working via Tauri v2 defaults.
- **Sphere modules** (`frontend/src/sphere/`):
  - `sphereShader.ts` — full WebGL2 fragment shader ported verbatim from `Design Mockup/`: 6 variants × 6 states compiled in, V1 reads only `warm + calm + liquid` at runtime.
  - `SphereCanvas.tsx` — React wrapper around the renderer, owns the `requestAnimationFrame` loop and the cross-fade between sphere states (~250ms via internal weight ref).
  - `useSphereState.ts` — pure hook deriving `'idle' | 'think' | 'speak' | 'error'` from the chat store. Priority: `connectionStatus !== 'open'` → `error`; `isWaitingResponse` → `think`; `speakingMsgId` set OR assistant stream in progress → `speak`; otherwise `idle`.
  - `useAudioLevel.ts` — hook that grabs the `AnalyserNode` exposed by `audioPlayer.ts`, samples RMS in a `requestAnimationFrame` loop, returns a `useRef<number>` so the parent never re-renders at 60 Hz. Fallback 0 when no analyser is available.
- **HUD components** (`frontend/src/components/sphere/`):
  - `HudTasks.tsx` — top-right task panel; maps the existing `chatStore.tasks` map onto the mockup format and shows the 4 most recent.
  - `TranscriptLine.tsx` — single-line fade between the last user prompt (during `think`), the last assistant snippet (during/after `speak`), and the idle hint.
  - `InputField.tsx` — text input sitting under the transcript line; reuses the same WS `send` function as `ChatView` via a small React context.
  - `MarkdownOverlay.tsx` — overlay card that renders the last assistant message through `react-markdown` + `remark-gfm`; closes on `Esc`, backdrop click, or the `×` button.
  - `MuteToggle.tsx` — bottom-right speaker glyph bound to `useVoiceMode`; toggled by the `M` keyboard shortcut (skipped while focus is inside an `INPUT` / `TEXTAREA`).
  - `DevControls.tsx` — only mounted when `?dev=1`; renders state pills (1–6 + Auto) plus the tweaks panel (motion / glow / variant / mood / theme / autoCycle) and the 1-6 keyboard shortcut to force state.
  - `sphereWsContext.tsx` — local React context publishing the `send` function from the WS bridge so leaves don't open extra sockets.
- **Glue**:
  - `frontend/src/components/SphereUI.tsx` is the composition root for `?ui=new`. It replaces the prior placeholder, mounts the canvas + HUD, and renders `MarkdownOverlay` only when `shouldOverlayResponse` returns true on the last assistant message.
  - `frontend/src/hooks/useChatWsBridge.ts` factors the WS connection out of `ChatView` so the Sphere window owns its own socket (mirrors the existing `useWebSocket` lifecycle).
  - `frontend/src/lib/overlayHeuristic.ts` — pure `shouldOverlayResponse(content): boolean`: opens when content contains markdown structure (heading, list, code fence, blockquote, table, link, hr) **or** has more than 3 lines.
  - `frontend/src/state/devTweaksStore.ts` — Zustand store holding the locked V1 defaults plus the dev-mode overrides; persists tweaks to `localStorage`.
  - `frontend/src/styles/hud.css` — global stylesheet imported from `main.tsx`, ported from the mockup `<style>` block (CSS vars, theme/mood/state selectors, all `.hud-*` / `.md-*` / `.sphere-stage` / `.overlay-card` classes, plus the 28px drag region). The 5 Google Fonts (Space Grotesk, JetBrains Mono, Geist, Geist Mono, Newsreader) are imported in the same file. `frontend/src/index.css` exposes the design tokens through a Tailwind v4 `@theme` block so utilities like `bg-accent` line up with the CSS vars.
- **Audio tap** (`frontend/src/audio/audioPlayer.ts`): the Web Audio graph is now `source → analyser → destination`. The added node is FFT-sized for RMS reads, audibly identical to the previous path, and exposed via `getAnalyser()`.
- **No backend changes**: zero migration, zero new endpoint, zero WS protocol change. The sphere derives every state from existing chat-store fields. The Jarvis tool-calling layer and the TTS pipeline shipped in 0002/0003 are untouched.
- **Test stack landed in this feature**: Vitest + `@testing-library/react` + `@testing-library/jest-dom` + `jsdom`, wired in `frontend/vitest.config.ts` and `frontend/src/test/setup.ts`. Scripts `pnpm test` / `pnpm test:watch` added. The current suite is 13 files / 117 tests, covering every new deep module (`shouldOverlayResponse`, `useSphereState`, `useAudioLevel`, `SphereCanvas`, `MarkdownOverlay`, `HudTasks`, `TranscriptLine`, `InputField`, `MuteToggle`, `DevControls`, `useChatWsBridge`).

## Notable decisions

- **V1 lock**: `theme=warm`, `mood=calm`, `variant=liquid`. The other 5 variants (`swarm` / `wire` / `plasma` / `void` / `glyph`) are compiled into the shader so we keep parity with the mockup, but only the dev tweaks panel (`?dev=1`) can switch them. Production code never reads anything else.
- **Sphere state is fully client-derived** — no backend or protocol change. Priority order is `connectionStatus → isWaitingResponse → speakingMsgId → idle`, implemented as a pure hook so every transition is unit-tested in isolation.
- **Overlay heuristic = structure OR length** — `shouldOverlayResponse` opens the markdown card only when the assistant reply contains heading / list / code / blockquote / table / link / hr OR has more than 3 lines. Short plain replies (e.g. "il est 14:32") stay in the transcript line so the overlay does not pollute every turn.
- **WS bridge factored, not deduped** — `useChatWsBridge` lives at the top of the `?ui=new` tree and owns its own socket; the legacy `ChatView` keeps the connection it had before. The two windows therefore run two WS connections to the same backend session, which is acceptable for the opt-in dev experience and will be deduped in a follow-up if we ever flip the default.
- **Audio reactivity uses a real analyser** — `audioPlayer.ts` now inserts an `AnalyserNode` between the TTS source and the destination. The graph is audibly identical to before; `getAnalyser()` is the only new public surface.
- **Legacy chain kept intact** — `ChatView`, `ChatMessageBlock`, `TaskCard`, `TaskSidebar`, `TaskDrawer`, `Dispatcher`, `registry`, `MarkdownView`, `Toast` all stay in the repo and still serve the Legacy window. The PRD's optional "delete on flip" step is **not** taken: this feature ships the new UI as opt-in only, with no default flip. `?ui=legacy` (and the default) still renders the pre-0004 experience unchanged.
- **Tauri borderless without losing UX** — `decorations: false` on the `new` window plus a 28px `-webkit-app-region: drag` strip at the top of `hud.css` keeps the cinematic frame promise. OS-level shortcuts (Cmd+W / Cmd+M / Cmd+Q) are still wired via Tauri v2's default menu.
- **Dev mode is URL-gated** — `?ui=new&dev=1` mounts `DevControls`, reveals state pills + tweaks panel, and persists tweaks to `localStorage`. Without the query the dev tree never mounts, so prod stays clean.

## Issues

- `issues/0026-vitest-setup-overlay-heuristic.md` — Vitest setup + `shouldOverlayResponse` heuristic — commit `6a2f4f2`.
- `issues/0027-hud-css-port-tailwind-theme.md` — Port `hud.css` + Tailwind v4 `@theme` + Google Fonts — commit `a962daf`.
- `issues/0028-sphere-canvas-shader-port.md` — Port WebGL2 renderer + `SphereCanvas` component — commit `77555c7`.
- `issues/0029-use-sphere-state-derive.md` — `useSphereState` pure hook + `SphereCanvas` binding — commit `adeec89`.
- `issues/0030-input-field-transcript-line.md` — `InputField` + `TranscriptLine` + WS bridge for `?ui=new` — commit `64b6c7e`.
- `issues/0031-markdown-overlay-auto-trigger.md` — `MarkdownOverlay` auto-trigger + dismiss paths — commit `cc66ac3`.
- `issues/0032-hud-tasks-panel.md` — `HudTasks` panel bound to `chatStore.tasks` — commit `5175ddb`.
- `issues/0033-audio-level-sphere-reactivity.md` — Real audio reactivity via `AnalyserNode` tap — commit `8848d5a`.
- `issues/0034-mute-toggle.md` — `MuteToggle` bottom-right + `M` keyboard shortcut — commit `38ded60`.
- `issues/0035-dev-controls-gated.md` — `DevControls` gated behind `?dev=1` + tweaks store — commit `cf7140f`.
- `issues/0036-tauri-borderless-drag-region.md` — Tauri borderless `new` window + drag region — commit `87f9534`.
- Wrap-up: `issues/0037-feature-wrap-up.md` (this doc).
