// SettingsControl.tsx — Piste 3D · Nacre top-right « RÉGLAGES » modal
// (PRD 0014 / issue 0089). Replaces the provisional top-left ProviderPicker.
//
// A gear button in the top-right zone opens a frosted modal, ported verbatim
// from `Design Mockup/p3d-panels.jsx` (the `SettingsControl` component) +
// `Design Mockup/p3d.css` (the `.settings-*` / `.set-*` block) and matched to
// the screenshot `Design Mockup/screenshots/p3d-settings.png`. The panel:
//   - a segmented control Claude CLI ↔ LM Studio,
//   - on Claude: a read-only "connected" status + the fixed CLI model label,
//   - on LM Studio: a server URL field (http:// prefix) + presets +
//     reachability state, a live local-model list (name / spec / status) with
//     selection, and the context-length slider + Apply (feature 0013).
//
// The whole backend wiring is ported from the old `components/sphere/
// ProviderPicker.tsx` (now deleted): the same `lib/llmApi` calls and the same
// blocking PUT lifecycles (provider switch, model swap, ctx apply), so the
// engine/model selection still persists across launches (the backend persists
// the selection JSON on every PUT). The mockup's hardcoded LM_MODELS catalogue
// is replaced by the live `GET /api/llm/models` fetch; the URL field + presets
// keep the mockup's local-edit behaviour (there is no URL REST endpoint — the
// server endpoint is configured server-side — so the field is informational and
// drives the reachability heuristic only).
//
// Takes NO props — the shell renders `<SettingsControl/>`. Open/close + all LLM
// state is owned here. Co-located styles live in `SettingsControl.css`.

import { useCallback, useEffect, useRef, useState } from "react";
import {
  type LlmModel,
  LlmModelSwapError,
  LlmModelsUnavailableError,
  type LlmSelection,
  fetchLlmModels,
  fetchLlmSelection,
  putLlmModel,
  putLlmProvider,
} from "../../lib/llmApi";
import "./SettingsControl.css";

// Backend provider ids (mirror of the server enum). The mockup used
// "claude"/"lmstudio"; the REST API speaks these.
const PROVIDER_CLAUDE = "claude_cli";
const PROVIDER_LM = "lm_studio";

// Fallback Claude label shown before the selection loads (the backend's
// `claude_model` field is authoritative once `GET /selection` resolves).
const CLAUDE_MODEL_FALLBACK = "claude-sonnet-4.5";

// Server-URL presets (ported from the mockup). The active preset highlights
// when it matches the current URL; clicking one swaps the field. Local-only —
// there is no URL REST endpoint at this stage.
const LM_PRESETS = [
  { label: "localhost", url: "http://localhost:1234" },
  { label: "studio.local", url: "http://studio.local:1234" },
  { label: "192.168.1.20", url: "http://192.168.1.20:1234" },
];
const LM_URL_DEFAULT = LM_PRESETS[0].url;

/** Compact "architecture · quant" spec line — both come straight off the
 * backend `LLMModel`; falls back gracefully when a field is null. (The mockup's
 * "params · quant" can't be honoured: the live API exposes architecture, not a
 * parameter count.) */
function modelSpec(m: LlmModel): string {
  const parts = [m.architecture, m.quantisation].filter((p): p is string => !!p);
  return parts.join(" · ");
}

/** Cheap reachability heuristic for the URL field, ported from the mockup: the
 * URL is "joignable" iff it has a plausible host[:port][/path] shape. This is a
 * display affordance only (no probe request) — the authoritative reachability
 * check happens server-side when switching to LM Studio. */
function lmUrlReachable(raw: string): boolean {
  const s = raw.trim().replace(/^https?:\/\//i, "");
  return /^[\w.-]+(:\d{2,5})?(\/.*)?$/.test(s) && s.length > 3;
}

type ListState =
  | { status: "idle" }
  | { status: "loading" }
  | { status: "ready"; models: LlmModel[] }
  | { status: "error"; detail: string };

// Swap (PUT) lifecycle. `target` is the model id the user clicked so the row
// can show its own spinner; `detail` carries the failure message. We stay on
// the PREVIOUS selection while loading and on failure.
type SwapState =
  | { status: "idle" }
  | { status: "loading"; target: string }
  | { status: "error"; target: string; detail: string };

// Provider-switch (PUT { provider }) lifecycle. `target` is the provider the
// user toggled to so the failing message can be attributed; on failure the
// toggle reverts to the previous provider and shows `detail`.
type ProviderSwapState =
  | { status: "idle" }
  | { status: "loading"; target: string }
  | { status: "error"; target: string; detail: string };

// Ctx-length Apply (PUT { lm_model, context_length }) lifecycle (feature 0013).
// Distinct from the model SWAP state so an Apply (reload-with-ctx of the SAME
// model) shows its own loading/error without colliding with a model switch.
type CtxApplyState =
  | { status: "idle" }
  | { status: "loading" }
  | { status: "error"; detail: string };

// Lower bound for the ctx slider — a sane floor so the thumb never lands on a
// degenerate window. The upper bound is the selected model's max_context_length.
const CTX_SLIDER_MIN = 1024;
// Step the slider in 1k-token increments — smooth enough to feel continuous,
// coarse enough that the persisted/applied value is a round number.
const CTX_SLIDER_STEP = 1024;

// The ctx value to seed the slider for `model`: the persisted per-model value
// from the selection JSON if present, else the model's max (its default window).
function defaultCtxFor(model: LlmModel | null, selection: LlmSelection | null): number | null {
  if (model === null) return null;
  const persisted = selection?.context_length?.[model.id];
  if (typeof persisted === "number" && persisted > 0) return persisted;
  return model.max_context_length;
}

export function SettingsControl() {
  const [open, setOpen] = useState(false);
  // Provider toggle. Seeded from the current selection once it loads; the
  // explicit toggle fires the blocking provider PUT.
  const [provider, setProvider] = useState<string>(PROVIDER_LM);
  const [selection, setSelection] = useState<LlmSelection | null>(null);
  const [list, setList] = useState<ListState>({ status: "idle" });
  const [swap, setSwap] = useState<SwapState>({ status: "idle" });
  const [providerSwap, setProviderSwap] = useState<ProviderSwapState>({ status: "idle" });
  // LM Studio server URL — local-edit only (the mockup behaviour); drives the
  // reachability chip + preset highlight. No REST endpoint backs it.
  const [lmUrl, setLmUrl] = useState<string>(LM_URL_DEFAULT);
  // Ctx slider is LOCAL state (feature 0013): dragging mutates only this; the
  // explicit Apply button fires the blocking reload-with-ctx PUT. `null` until
  // a model + its bound are known.
  const [ctxValue, setCtxValue] = useState<number | null>(null);
  const [ctxApply, setCtxApply] = useState<CtxApplyState>({ status: "idle" });
  // Whether the live model list has been fetched in this open session. We fetch
  // once the panel is open under LM Studio (and refetch on a fresh open).
  const fetchedRef = useRef(false);

  const isLM = provider === PROVIDER_LM;

  // Load the current selection once on mount to highlight it and seed the
  // provider toggle. Failure is non-fatal: the modal still renders.
  useEffect(() => {
    const ctrl = new AbortController();
    fetchLlmSelection(ctrl.signal)
      .then((sel) => {
        setSelection(sel);
        if (sel.provider) setProvider(sel.provider);
      })
      .catch(() => {
        // selection unavailable — leave defaults; do not crash the HUD
      });
    return () => ctrl.abort();
  }, []);

  // Fetch the model list (once per fresh open under LM Studio).
  const loadModels = useCallback(() => {
    setList({ status: "loading" });
    fetchLlmModels()
      .then((models) => setList({ status: "ready", models }))
      .catch((err: unknown) => {
        const detail =
          err instanceof LlmModelsUnavailableError
            ? err.message
            : "Erreur de chargement des modèles";
        setList({ status: "error", detail });
      });
  }, []);

  // When the panel opens under LM Studio, fetch the live list once. Reseting
  // `fetchedRef` on close means a reopen refetches (the server's loaded set may
  // have changed). Toggling to Claude closes nothing here — the list section is
  // simply not rendered.
  useEffect(() => {
    if (open && isLM && !fetchedRef.current) {
      fetchedRef.current = true;
      loadModels();
    }
    if (!open) {
      fetchedRef.current = false;
    }
  }, [open, isLM, loadModels]);

  // Fire the BLOCKING swap PUT for the clicked model. No-op when it is already
  // the current selection or a swap is already in flight. On success we update
  // the local selection so the status label reflects the new model; on failure
  // we stay put and surface the detail on the row.
  const selectModel = useCallback(
    (modelId: string) => {
      if (swap.status === "loading") return;
      if (modelId === (selection?.lm_model ?? null)) return;
      setSwap({ status: "loading", target: modelId });
      putLlmModel(modelId)
        .then((next) => {
          setSelection(next);
          setSwap({ status: "idle" });
        })
        .catch((err: unknown) => {
          const detail =
            err instanceof LlmModelSwapError ? err.message : "Échec du changement de modèle";
          setSwap({ status: "error", target: modelId, detail });
        });
    },
    [swap.status, selection?.lm_model],
  );

  // Fire the BLOCKING provider-switch PUT. Optimistically flips the local toggle
  // so the UI responds immediately, then reverts to the previous provider on
  // failure. No-op when it is already the active provider or a switch is already
  // in flight. On success the local selection is updated so the status tracks
  // the new provider.
  const switchProvider = useCallback(
    (p: string) => {
      if (p === provider) return;
      if (providerSwap.status === "loading") return;
      const previous = provider;
      setProvider(p);
      setProviderSwap({ status: "loading", target: p });
      putLlmProvider(p)
        .then((next) => {
          setSelection(next);
          setProvider(next.provider);
          setProviderSwap({ status: "idle" });
        })
        .catch((err: unknown) => {
          const detail =
            err instanceof LlmModelSwapError ? err.message : "Échec du changement de moteur";
          setProvider(previous); // revert — the backend kept the previous provider
          setProviderSwap({ status: "error", target: p, detail });
        });
    },
    [provider, providerSwap.status],
  );

  const currentModelId = selection?.lm_model ?? null;
  const models = list.status === "ready" ? list.models : [];
  const activeModel = models.find((m) => m.id === currentModelId) ?? null;
  const claudeModel = selection?.claude_model ?? CLAUDE_MODEL_FALLBACK;
  const providerSwapping = providerSwap.status === "loading";

  // Seed / reseed the ctx slider whenever the active model (or its persisted
  // ctx) changes: the persisted per-model value if present, else the model
  // default (its max). Switching back to a model restores its remembered ctx.
  const ctxMax = activeModel?.max_context_length ?? null;
  const seededCtx = defaultCtxFor(activeModel, selection);
  // biome-ignore lint/correctness/useExhaustiveDependencies: reseed only when the model id / its persisted ctx / its max changes — not on every local drag.
  useEffect(() => {
    setCtxValue(seededCtx);
    setCtxApply({ status: "idle" });
  }, [currentModelId, ctxMax, selection?.context_length?.[currentModelId ?? ""]]);

  // Fire the BLOCKING ctx Apply (feature 0013): reload the CURRENT model at the
  // slider's ctx via the same validate-then-swap path. Dragging the slider does
  // NOT call this — only the explicit Apply button does. No-op without a model /
  // a value, or while another mutation is in flight.
  const applyCtx = useCallback(() => {
    if (currentModelId === null || ctxValue === null) return;
    if (ctxApply.status === "loading" || swap.status === "loading") return;
    setCtxApply({ status: "loading" });
    putLlmModel(currentModelId, ctxValue)
      .then((next) => {
        setSelection(next);
        setCtxApply({ status: "idle" });
      })
      .catch((err: unknown) => {
        const detail =
          err instanceof LlmModelSwapError ? err.message : "Échec de l'application du contexte";
        setCtxApply({ status: "error", detail });
      });
  }, [currentModelId, ctxValue, ctxApply.status, swap.status]);
  const ctxApplying = ctxApply.status === "loading";

  // URL field — the protocol is fixed to http:// by the `.set-field-proto`
  // prefix; the input edits the host[:port][/path] body only.
  const reachable = lmUrlReachable(lmUrl);
  const urlBody = lmUrl.replace(/^https?:\/\//i, "");
  const onUrlChange = (v: string) => setLmUrl(`http://${v.replace(/^https?:\/\//i, "")}`);

  return (
    <div className="settings-zone">
      <button
        type="button"
        className={`settings-btn ${open ? "is-open" : ""}`}
        onClick={() => setOpen((v) => !v)}
        aria-label="Réglages"
        aria-expanded={open}
      >
        <svg viewBox="0 0 24 24" className="settings-gear" aria-hidden="true">
          <circle cx="12" cy="12" r="3.2" />
          <path d="M12 2.5v3M12 18.5v3M21.5 12h-3M5.5 12h-3M18.7 5.3l-2.1 2.1M7.4 16.6l-2.1 2.1M18.7 18.7l-2.1-2.1M7.4 7.4L5.3 5.3" />
        </svg>
        <span className="settings-cap">Réglages</span>
      </button>

      {open && (
        <>
          {/* biome-ignore lint/a11y/useKeyWithClickEvents: the scrim is a redundant pointer affordance for closing; keyboard users dismiss via the visible Fermer button — no keyboard equivalent is needed on the backdrop. */}
          <div className="settings-scrim" onClick={() => setOpen(false)} />
          <div
            className="settings-panel"
            // biome-ignore lint/a11y/useSemanticElements: native <dialog> brings its own positioning + backdrop semantics that collide with the mockup chrome (`.settings-scrim` is our backdrop, this component owns open/closed).
            role="dialog"
            aria-label="Réglages"
            aria-modal="false"
          >
            <div className="settings-head">
              <span className="settings-title">RÉGLAGES</span>
              <button
                type="button"
                className="settings-close"
                onClick={() => setOpen(false)}
                aria-label="Fermer"
              >
                ✕
              </button>
            </div>

            <div className="settings-section">
              <div className="settings-label">MOTEUR LLM</div>

              {/* segmented provider toggle — Claude CLI ↔ LM Studio. Fires the
                blocking PUT { provider }; disabled while a switch is in flight. */}
              <div className="set-seg" role="radiogroup" aria-label="Moteur LLM">
                <button
                  type="button"
                  // biome-ignore lint/a11y/useSemanticElements: ARIA radiogroup of buttons per the mockup — <button> is the correct focusable element; the role only adds radio semantics.
                  role="radio"
                  aria-checked={!isLM}
                  disabled={providerSwapping}
                  className={`set-seg-btn ${!isLM ? "on" : ""}`}
                  onClick={() => switchProvider(PROVIDER_CLAUDE)}
                >
                  <span className="set-seg-glyph">&gt;_</span>Claude CLI
                </button>
                <button
                  type="button"
                  // biome-ignore lint/a11y/useSemanticElements: ARIA radiogroup of buttons per the mockup — <button> is the correct focusable element; the role only adds radio semantics.
                  role="radio"
                  aria-checked={isLM}
                  disabled={providerSwapping}
                  className={`set-seg-btn ${isLM ? "on" : ""}`}
                  onClick={() => switchProvider(PROVIDER_LM)}
                >
                  <span className="set-seg-glyph">▦</span>LM Studio
                </button>
              </div>

              {/* Provider-switch error — the toggle reverted to the previous
                provider; surface why so the user can retry / start LM Studio. */}
              {providerSwap.status === "error" && (
                <div className="set-status is-off" role="alert">
                  <span className="set-dot is-off" />
                  <span className="eng-name">{providerSwap.detail}</span>
                  <span className="set-status-state">échec</span>
                </div>
              )}

              {!isLM ? (
                <div className="set-detail" key="claude">
                  {/* Claude side: READ-ONLY model label from the backend
                    (`claude_model`) — no dropdown, no URL, no ctx control. */}
                  <div className="set-status is-ok">
                    <span className="set-dot is-ok" />
                    <span className="eng-name">{claudeModel}</span>
                    <span className="set-status-state">CLI · connecté</span>
                  </div>
                  <div className="set-field-hint">
                    <b>Pont CLI local — modèle fixe, aucune URL requise.</b>
                  </div>
                </div>
              ) : (
                <div className="set-detail" key="lm">
                  {/* LM Studio server URL — http:// prefix + editable body +
                    presets. Local-edit only (no URL endpoint); drives the
                    reachability chip below. */}
                  <div className="set-field">
                    <div className="settings-label">SERVEUR LM STUDIO</div>
                    <div className="set-field-row">
                      <span className="set-field-proto">http://</span>
                      <input
                        className="set-input"
                        type="text"
                        inputMode="url"
                        spellCheck="false"
                        value={urlBody}
                        placeholder="localhost:1234"
                        onChange={(e) => onUrlChange(e.target.value)}
                        aria-label="URL du serveur LM Studio"
                      />
                    </div>
                    <div className="set-presets">
                      {LM_PRESETS.map((p) => (
                        <button
                          key={p.url}
                          type="button"
                          className={`set-preset ${lmUrl === p.url ? "on" : ""}`}
                          onClick={() => setLmUrl(p.url)}
                        >
                          {p.label}
                        </button>
                      ))}
                    </div>
                  </div>

                  <div className={`set-status ${reachable ? "is-ok" : "is-off"}`}>
                    <span className={`set-dot ${reachable ? "is-ok" : "is-off"}`} />
                    <span className="eng-name">
                      {reachable ? "serveur joignable" : "serveur introuvable"}
                    </span>
                    <span className="set-status-state">
                      {reachable ? "connecté" : "hors ligne"}
                    </span>
                  </div>

                  {/* live local-model list — from GET /api/llm/models, fetched on
                    open. Clicking a non-current row fires the blocking swap PUT. */}
                  <div className="set-field">
                    <div className="set-models-head">
                      <span className="settings-label">MODÈLE LOCAL</span>
                      <span className="set-models-count">
                        {list.status === "ready"
                          ? `${list.models.length} disponibles`
                          : list.status === "loading"
                            ? "chargement…"
                            : list.status === "error"
                              ? "indisponible"
                              : ""}
                      </span>
                    </div>
                    <div
                      className="set-models"
                      // biome-ignore lint/a11y/useSemanticElements: ARIA listbox ported from the mockup; a native <select> can't carry the per-row metadata chrome. tabIndex below makes the interactive role focusable.
                      role="listbox"
                      tabIndex={0}
                      aria-label="Modèle local"
                    >
                      {list.status === "error" && (
                        <div className="set-model is-error" role="alert">
                          <span className="set-model-mark">⚠</span>
                          <span className="set-model-name">Serveur LM Studio injoignable</span>
                          <span className="set-model-spec">{list.detail}</span>
                        </div>
                      )}

                      {list.status === "ready" &&
                        list.models.map((mm) => {
                          const on = mm.id === currentModelId;
                          const isSwapping = swap.status === "loading" && swap.target === mm.id;
                          const swapFailed = swap.status === "error" && swap.target === mm.id;
                          const busy = swap.status === "loading";
                          const ramLabel = isSwapping
                            ? "chargement…"
                            : swapFailed
                              ? "erreur"
                              : mm.loaded
                                ? "chargé"
                                : "";
                          return (
                            <button
                              key={mm.id}
                              type="button"
                              // biome-ignore lint/a11y/useSemanticElements: ARIA option inside the listbox above; rows carry custom multi-field metadata chrome a native <option> can't render. <button> keeps it natively focusable + clickable.
                              role="option"
                              aria-selected={on}
                              aria-busy={isSwapping}
                              disabled={busy}
                              className={`set-model ${on ? "on" : ""} ${isSwapping ? "is-loading" : ""} ${swapFailed ? "is-error" : ""}`}
                              data-testid={`set-model-${mm.id}`}
                              onClick={() => selectModel(mm.id)}
                            >
                              <span className="set-model-mark">{on ? "◆" : "◇"}</span>
                              <span className="set-model-name">{mm.id}</span>
                              <span className="set-model-spec">
                                {swapFailed ? swap.detail : modelSpec(mm)}
                              </span>
                              <span className="set-model-ram">{ramLabel}</span>
                            </button>
                          );
                        })}
                    </div>
                  </div>

                  {/* ctx-length slider + Apply (feature 0013) — only with a known
                    max for the active model. Dragging mutates LOCAL state only;
                    Apply fires the blocking reload-with-ctx. Clamped to
                    [MIN, max_context_length]. */}
                  {activeModel !== null && ctxMax !== null && ctxValue !== null && (
                    <div className="set-ctx" data-testid="set-ctx">
                      <div className="set-ctx-head">
                        <span className="settings-label">CONTEXTE</span>
                        <span className="set-ctx-val" data-testid="set-ctx-value">
                          {ctxValue.toLocaleString()} tok
                        </span>
                      </div>
                      <input
                        type="range"
                        className="set-ctx-slider"
                        data-testid="set-ctx-slider"
                        aria-label="Longueur de contexte"
                        min={Math.min(CTX_SLIDER_MIN, ctxMax)}
                        max={ctxMax}
                        step={CTX_SLIDER_STEP}
                        value={Math.min(ctxValue, ctxMax)}
                        disabled={ctxApplying}
                        onChange={(e) => setCtxValue(Number(e.target.value))}
                      />
                      <div className="set-ctx-actions">
                        <span className="set-ctx-max">max {ctxMax.toLocaleString()}</span>
                        <button
                          type="button"
                          className="set-ctx-apply"
                          data-testid="set-ctx-apply"
                          disabled={ctxApplying}
                          aria-busy={ctxApplying}
                          onClick={applyCtx}
                        >
                          {ctxApplying ? "application…" : "Appliquer"}
                        </button>
                      </div>
                      {ctxApply.status === "error" && (
                        <div className="set-ctx-error" role="alert">
                          {ctxApply.detail}
                        </div>
                      )}
                    </div>
                  )}
                </div>
              )}
            </div>
          </div>
        </>
      )}
    </div>
  );
}
