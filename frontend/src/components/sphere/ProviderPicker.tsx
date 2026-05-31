import { useCallback, useEffect, useRef, useState } from "react";
import {
  type LlmModel,
  LlmModelSwapError,
  LlmModelsUnavailableError,
  type LlmSelection,
  fetchLlmModels,
  fetchLlmSelection,
  putLlmModel,
} from "../../lib/llmApi";

// ProviderPicker — LLM engine picker mounted in the Sphere HUD top-left zone
// (PRD 0012 / issues 0079-0080). Ported from `Design Mockup/provider.jsx`.
//
// SCOPE:
//   - The segmented toggle renders (Claude CLI / LM Studio) but does NOT yet
//     mutate the backend — the live provider switch is issue 0081.
//   - Under LM Studio, opening the active-engine row fetches
//     `GET /api/llm/models` and renders the LIVE list, highlighting the CURRENT
//     selection from `GET /api/llm/selection`.
//   - Issue 0080: clicking a model fires the BLOCKING `PUT /api/llm/selection`
//     to load+swap it. While the swap runs the row shows a loading state; on
//     failure the picker stays on the previous model and shows the error; on
//     success the active-engine label (the HUD's engine/model footer) updates.
// The hardcoded `LM_MODELS` catalogue from the mockup is replaced by the live
// fetch; the `.pv-*` class structure and French labels are preserved.

const CLAUDE_MODEL = "claude-sonnet-4.5";

/** Compact "params · quant" spec line. `params` is derived from the model id
 * is not exposed by the live API at this stage, so we show architecture · quant
 * (both come straight off the backend `LLMModel`). Falls back gracefully when
 * a field is null. */
function modelSpec(m: LlmModel): string {
  const parts = [m.architecture, m.quantisation].filter((p): p is string => !!p);
  return parts.join(" · ");
}

type ListState =
  | { status: "idle" }
  | { status: "loading" }
  | { status: "ready"; models: LlmModel[] }
  | { status: "error"; detail: string };

// Swap (PUT) lifecycle. `target` is the model id the user clicked so the row
// can show its own spinner; `detail` carries the failure message. The picker
// stays on the PREVIOUS selection while loading and on failure.
type SwapState =
  | { status: "idle" }
  | { status: "loading"; target: string }
  | { status: "error"; target: string; detail: string };

export function ProviderPicker() {
  const [open, setOpen] = useState(false);
  // Provider toggle is local-only at this stage (no backend mutation). Seeded
  // from the current selection once it loads.
  const [provider, setProvider] = useState<string>("lm_studio");
  const [selection, setSelection] = useState<LlmSelection | null>(null);
  const [list, setList] = useState<ListState>({ status: "idle" });
  const [swap, setSwap] = useState<SwapState>({ status: "idle" });
  const fetchedRef = useRef(false);

  const isLM = provider === "lm_studio";

  // Load the current selection once on mount to highlight it and seed the
  // provider toggle. Failure is non-fatal: the picker still renders.
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

  // Fetch the model list ON OPEN (once per open transition under LM Studio).
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

  // Fire the BLOCKING swap PUT for the clicked model (issue 0080). No-op when
  // it is already the current selection or a swap is already in flight. On
  // success we update the local selection so the active-engine label (the HUD
  // engine/model footer) reflects the new model; on failure we stay put and
  // surface the detail on the row.
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

  const toggleOpen = () => {
    if (!isLM) return;
    setOpen((wasOpen) => {
      const next = !wasOpen;
      if (next && !fetchedRef.current) {
        fetchedRef.current = true;
        loadModels();
      }
      return next;
    });
  };

  const switchProvider = (p: string) => {
    if (p === provider) return;
    setProvider(p);
    if (p !== "lm_studio") setOpen(false);
  };

  const currentModelId = selection?.lm_model ?? null;
  const models = list.status === "ready" ? list.models : [];
  const activeModel = models.find((m) => m.id === currentModelId) ?? null;

  return (
    <div className={`pv ${isLM ? "is-lm" : "is-claude"}`} data-testid="provider-picker">
      <div className="pv-head">
        <span className="pv-head-cap">MOTEUR LLM</span>
        <span className="pv-head-link">{isLM ? "local" : "cli"}</span>
      </div>

      {/* segmented provider toggle (display-only this slice). The radiogroup
        of buttons is the mockup's chosen ARIA pattern; <button> is already the
        right interactive element, so the role just narrows the semantics. */}
      <div className="pv-seg" role="radiogroup" aria-label="LLM provider">
        <button
          type="button"
          // biome-ignore lint/a11y/useSemanticElements: ARIA radiogroup of buttons per the mockup — <button> is the correct focusable element; the role only adds radio semantics.
          role="radio"
          aria-checked={!isLM}
          className={`pv-seg-btn ${!isLM ? "on" : ""}`}
          onClick={() => switchProvider("claude_cli")}
        >
          <span className="pv-seg-glyph">&gt;_</span>
          <span className="pv-seg-name">Claude CLI</span>
        </button>
        <button
          type="button"
          // biome-ignore lint/a11y/useSemanticElements: ARIA radiogroup of buttons per the mockup — <button> is the correct focusable element; the role only adds radio semantics.
          role="radio"
          aria-checked={isLM}
          className={`pv-seg-btn ${isLM ? "on" : ""}`}
          onClick={() => switchProvider("lm_studio")}
        >
          <span className="pv-seg-glyph pv-seg-glyph-grid">▦</span>
          <span className="pv-seg-name">LM Studio</span>
        </button>
      </div>

      {/* active-engine row — clickable under LM Studio to open the model list */}
      <button
        type="button"
        className={`pv-active ${isLM ? "pv-active-btn" : ""} ${open ? "is-open" : ""}`}
        onClick={toggleOpen}
        disabled={!isLM}
        aria-expanded={isLM ? open : undefined}
      >
        <span className="pv-dot is-ok" />
        <span className="pv-active-main">
          {isLM ? (
            <>
              <span className="pv-active-name">{activeModel?.id ?? currentModelId ?? "—"}</span>
              <span className="pv-active-spec">
                {activeModel ? modelSpec(activeModel) : "modèle local"}
              </span>
            </>
          ) : (
            <>
              <span className="pv-active-name">{CLAUDE_MODEL}</span>
              <span className="pv-active-spec">CLI bridge</span>
            </>
          )}
        </span>
        <span className="pv-active-state">{isLM ? "chargé" : "connecté"}</span>
        {isLM && <span className="pv-chev">{open ? "▴" : "▾"}</span>}
      </button>

      {/* live model list — only under LM Studio, only when open. Read-only this
        slice: the options DISPLAY the live list + highlight the current
        selection; they are not yet selectable (no PUT until issue 0081). */}
      {isLM && open && (
        // biome-ignore lint/a11y/useSemanticElements: ARIA listbox of the live model list, ported from the mockup; a native <select> can't carry the per-row metadata chrome.
        <div className="pv-list" role="listbox" tabIndex={0} aria-label="LM Studio models">
          <div className="pv-list-head">
            <span>MODÈLE LOCAL</span>
            <span>
              {list.status === "ready"
                ? `${list.models.length} disponibles`
                : list.status === "loading"
                  ? "chargement…"
                  : list.status === "error"
                    ? "indisponible"
                    : ""}
            </span>
          </div>

          {list.status === "error" && (
            <div className="pv-row pv-row-error" role="alert">
              <span className="pv-row-name">Serveur LM Studio injoignable</span>
              <span className="pv-row-spec">{list.detail}</span>
            </div>
          )}

          {list.status === "ready" &&
            list.models.map((m) => {
              const on = m.id === currentModelId;
              const isSwapping = swap.status === "loading" && swap.target === m.id;
              const swapFailed = swap.status === "error" && swap.target === m.id;
              const busy = swap.status === "loading";
              const ramLabel = isSwapping
                ? "chargement…"
                : swapFailed
                  ? "erreur"
                  : m.loaded
                    ? "chargé"
                    : "";
              return (
                <button
                  key={m.id}
                  type="button"
                  // biome-ignore lint/a11y/useSemanticElements: ARIA option inside the listbox above; rows carry custom multi-field metadata chrome a native <option> can't render. <button> keeps it natively focusable + clickable.
                  role="option"
                  aria-selected={on}
                  aria-busy={isSwapping}
                  disabled={busy}
                  className={`pv-row ${on ? "on" : ""} ${isSwapping ? "is-loading" : ""} ${swapFailed ? "is-error" : ""}`}
                  data-testid={`pv-row-${m.id}`}
                  onClick={() => selectModel(m.id)}
                >
                  <span className="pv-row-mark">{on ? "◆" : "◇"}</span>
                  <span className="pv-row-name">{m.id}</span>
                  <span className="pv-row-spec">{swapFailed ? swap.detail : modelSpec(m)}</span>
                  <span className="pv-row-ram">{ramLabel}</span>
                </button>
              );
            })}
        </div>
      )}
    </div>
  );
}
