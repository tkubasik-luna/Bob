# Refonte HUD « Piste 3D · Nacre »

Shipped on 2026-06-04 from PRD `prd/0014-hud-piste-3d-nacre.md`.

## What it does

The `new` HUD window (`?ui=new`) is reskinned in-place to the "Piste 3D · Nacre"
mockup, wired to the real backend/WS data. The centre is a living conscience orb
(nacre nebula) that breathes at rest and shifts mood with what Bob is doing; the
left is a 3D stacked **deck** showing the thread of consciousness — a BOB card
(prompt → reflection → background tasks → response → perf) in front, sub-task
cards behind, the live card gliding forward (click a back card to promote it); the
right is a **DONNÉES GÉNÉRÉES** dock accumulating the artifacts Bob produces, which
persist for the session and open full-screen on click (typed Mail / Document
surfaces, read-aloud). Top-left is the `● BOB · {état}` identity + tagline; top-right
a **RÉGLAGES** gear opens the LLM settings modal (Claude CLI ↔ LM Studio, model list,
context length); bottom-centre a minimal always-on input + voice caption, mute
bottom-right. Cold start centres orb + identity + input and fades the deck/dock in on
the first real data. The `legacy` (ChatView) and `debug` windows are unchanged.

## Technical surface

- **Window**: only the `new` HUD is touched. `App.tsx` still routes `?ui=new` →
  `SphereUI`; `SphereUI.tsx` is rewritten into the `.piste` shell.
- **Shell + scene** (`frontend/src/components/piste/`): `BackgroundGrain`, `Identity`,
  a 3D `.stage` with `slot-core` / `slot-task` / `slot-data`, plus `SettingsControl`
  (top-right), `BottomBar` (bottom), the section overlay and `DevControls`. Scoped
  foundation stylesheet `frontend/src/styles/p3d.css` (tokens/base/layout/camera/stage/
  identity/bg) + co-located per-component CSS.
- **Conscience orb**: `piste/orb/ConscienceOrb.tsx` + `conscienceShader.ts` +
  `conscienceLife.ts` (WebGL nebula port), mounted in `CoreSlot`.
- **Thread deck (left)**: `TaskSlot` renders the 3D deck; `BobCard` + `SubCard`.
  Replaces the old right-rail `AgentActivityPanel` (dropped from the shell).
- **Data dock (right)**: `DataSlot` + `DataDock` + `DataCard`, fed by the new
  `frontend/src/store/deliverableStore.ts` (session-scoped, `fresh`/`seen`, no TTL)
  via `useDeliverableIngest`.
- **Overlay**: `components/sphere/SectionsOverlay.tsx` reskinned to the mockup chrome
  (corners, beam, mono header `BOB · GÉNÉRÉ` + `RÉF`, footer actions); typed surfaces
  `MailCard` (MailSurface) and new `DocSurface` (reuses the existing markdown renderer);
  composite deliverables still render as a section stack. Click-only (auto-open removed).
- **Settings**: `piste/SettingsControl.tsx` modal replaces the old top-left
  `ProviderPicker` (deleted), wired to the existing `/api/llm/*` endpoints.
- **Pure, unit-tested modules** (`frontend/src/lib/`): `orbState` (chat+tasks → orb
  state/energy), `reflectionNarrator` (narrated reflection fallback), `threadDeck`
  (deck order / rank→transform / front / promote), `deliverableCard` (deliverable →
  dock card), plus `components/sphere/overlayArtifact` (header/REF + read-aloud text).
- **Events consumed** (all via existing stores, no new data source): `reasoning_delta`,
  `agent_perf`, `agent_answer`, `agent_activity`, `speech_delta`, `task_created/updated/result`
  (`result_payload`), `ui_payload`, `assistant_msg` — through `chatStore` +
  `activityFeedStore` + the new `deliverableStore`.
- **Backend**: no changes. The main Bob thread already emits `reasoning_delta` / `perf`
  / `answer` / `activity` under the stable `agent_ref = "jarvis"`.
- **Migrations / tables**: none (frontend feature).

## Notable decisions

- **Stub-slot architecture**: the foundation issue (0083) pre-wires empty slot
  components in `components/piste/`, so the eight downstream slices each own a disjoint
  file set and never edit the shell. This is what made the parallel build safe — respect
  it when adding new zones (own a slot file; don't fan logic back into `SphereUI.tsx`).
- **CSS reconciliation**: `p3d.css` carries the foundation layers only, scoped under
  `.piste`; every component owns a co-located CSS file (never extend `p3d.css`). The
  overlay's mockup `ov-*` rules supersede the legacy `hud.css` ones via a `.p3d-ov`
  specificity bump — `hud.css` is left untouched.
- **`agent_ref = "jarvis"`** binds the BOB card to the orchestrator's reasoning/perf/
  answer lane exactly as sub-task cards bind to their `task_id`. Don't break this ref.
- **Overlay is click-only** (legacy auto-open effects removed); dock cards persist for
  the session (no TTL); **1 card per deliverable**, composite → section stack in overlay.
- **Pure modules stay UI-free and tested** (`orbState`, `reflectionNarrator`, `threadDeck`,
  `deliverableCard`, `overlayArtifact`); orb/scene/deck/dock/overlay/settings CSS + WebGL
  are validated by eye / in run, not unit tests (per the PRD testing decision).
- **Verbatim mockup port** where possible (conscience shader/life, `DeckCard`/`ThreadStack`
  transform math, `ov-*` chrome). Nacre palette only; **Mail + Document** surfaces only
  (video/contact/action deferred).
- **Read-aloud** ("LIRE À VOIX HAUTE") uses the in-component Web Speech API — there is no
  backend "speak arbitrary text" endpoint; playback is otherwise backend-driven.
- **Assumed product deltas vs pure fidelity** (explicit at grilling): session persistence
  instead of the mockup's ~11 s TTL; 2 real surface types instead of 5; 1 card per
  deliverable + overlay stack instead of 1 card per artifact; 100 % live data, no demo/attract mode.

## Issues

- `issues/0083-piste3d-foundation-shell-css.md` — piste foundation shell + scoped CSS — commit `0beb557`
- `issues/0084-conscience-orb-orbstate-reducer.md` — conscience nebula orb + orbState reducer — commit `bcd767f`
- `issues/0085-bob-card-reflection-perf.md` — BOB card (reflection / tasks / response / perf) — commit `6a88c63`
- `issues/0087-data-dock-deliverable-store.md` — DONNÉES GÉNÉRÉES dock + deliverable store — commit `9e4408a`
- `issues/0089-settings-modal-llm-picker.md` — RÉGLAGES settings modal (replaces ProviderPicker) — commit `4b8a0fb`
- `issues/0090-input-transcript-mute-nacre.md` — nacre input / transcript / mute bottom bar — commit `d8d3b19`
- `issues/0086-subtask-cards-thread-deck.md` — sub-task cards + 3D thread-deck — commit `72ae1e3`
- `issues/0088-overlay-reskin-typed-surfaces.md` — overlay reskin + typed Mail/Document surfaces — commit `2dc0047`
- `issues/0091-idle-state-panels-fade-in.md` — cold-start idle + panels fade-in — commit `3b31000`
