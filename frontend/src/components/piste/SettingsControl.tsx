// SettingsControl.tsx — Piste 3D · Nacre top-right « RÉGLAGES » modal.
//
// PRD 0016 / issue 0108 evolves this panel from a single GLOBAL LLM switch
// (PRD 0012/0014) into a PER-ROLE picker. The frosted top-right modal now holds
// four sections:
//
//   1. MOTEURS LLM (par rôle) — one block per role (`jarvis`/`thinker`/`draft`/
//      `subagent`): a provider segmented control (Claude CLI ↔ LM Studio), and
//      under LM Studio a server URL field + a live model ROW-LIST (clickable,
//      from the role's committed host via `GET /models?base_url=`) + a
//      context-length slider. Each block wires `PUT /api/llm/roles/{role}` so
//      only that role's client is rebuilt.
//   2. TRANSCRIPTION (STT) — the whisper.cpp engine (read-only) + the model
//      (default `large-v3-turbo`). The backend exposes the stt block read-only
//      in `GET /roles`; there is no STT mutation endpoint yet (seam, below).
//   3. BUDGET MÉMOIRE — the per-host model-budget config (ceiling / reserve)
//      surfaced read-only, plus the over-budget warning raised when a per-role
//      swap is refused with `budget_exceeded` (Annexe G). Per-role `ready` /
//      `offline` badges come from a live LM Studio ping (Claude roles are always
//      ready).
//   4. VOIX — the TTS on/off toggle (unchanged from 0089) + the global `M`
//      shortcut.
//
// Backend contracts consumed (all committed, issues 0106/0107):
//   - `GET  /api/llm/roles`        → the per-role map (+ stt + budget).
//   - `PUT  /api/llm/roles/{role}` → swap ONE role; returns the updated map.
//   - `PUT  /api/llm/roles/{role}/reasoning` → set ONE role's LM Studio
//     reasoning level. LIGHTWEIGHT: reasoning is a per-request chat param, so
//     this persists + refreshes the client WITHOUT reloading the model.
//   - `GET  /api/llm/models?base_url=` → the live model list for the role's
//     committed host (so each role's catalogue follows its OWN URL).
//   - `GET  /api/llm/ping`         → reachability for the per-role badge.
//
// SEAMS (documented, NOT faked):
//   - The budget block is CONFIG only: the backend has no LIVE resident-usage
//     endpoint (`model_budget.HostBudget.resident_gib()` is in-process on the
//     manager). We render the ceiling/reserve + the over-budget refusal, but no
//     live usage gauge.
//   - There is no STT mutation endpoint; the STT model field is informational
//     (read-only display of the persisted value).
//
// Takes NO props — the shell renders `<SettingsControl/>`. Open/close + all
// state is owned here. Co-located styles live in `SettingsControl.css`.

import { useCallback, useEffect, useRef, useState } from "react";
import { useVoiceMode } from "../../hooks/useVoiceMode";
import {
  LLM_ROLES,
  type LlmModel,
  LlmModelsUnavailableError,
  type LlmRole,
  LlmRoleSwapError,
  type RoleMap,
  type RoleSelection,
  fetchLlmModels,
  fetchLlmRoles,
  pingLm,
  putLlmRole,
  putLlmRoleReasoning,
} from "../../lib/llmApi";
import "./SettingsControl.css";

// Backend provider ids (mirror of the server enum).
const PROVIDER_CLAUDE = "claude_cli";
const PROVIDER_LM = "lm_studio";

// Human label per role (French, matches the HUD language). Order follows
// LLM_ROLES so the blocks render Speaker → Thinker → Draft → Sub-agent.
const ROLE_LABEL: Record<LlmRole, string> = {
  jarvis: "Jarvis · voix",
  thinker: "Penseur",
  draft: "Brouillon",
  subagent: "Sous-agent",
};

// Default LM Studio base URL when a role has none pinned (the field seeds from
// the role's persisted base_url; this is the fallback for an unset one).
const LM_URL_DEFAULT = "http://localhost:1234/v1";

// LM Studio reasoning levels (mirror of the backend `REASONING_LEVELS` + an
// "auto" sentinel for `null`). `null` omits the field so the model picks its
// own setting; the others map 1:1 to the LM Studio `reasoning` body field.
// Errors server-side if the model doesn't support the chosen level.
const REASONING_OPTIONS: { value: string | null; label: string }[] = [
  { value: null, label: "auto" },
  { value: "off", label: "off" },
  { value: "low", label: "bas" },
  { value: "medium", label: "moyen" },
  { value: "high", label: "haut" },
  { value: "on", label: "on" },
];

/** Normalise a raw URL-field value into a committable LM Studio base URL. */
function normalizeLmUrl(raw: string): string {
  let s = raw.trim();
  if (!/^https?:\/\//i.test(s)) s = `http://${s}`;
  s = s.replace(/\/+$/, "");
  if (!/\/v\d+$/i.test(s)) s = `${s}/v1`;
  return s;
}

/** Compact "architecture · quant" spec line off the backend `LLMModel`. */
function modelSpec(m: LlmModel): string {
  const parts = [m.architecture, m.quantisation].filter((p): p is string => !!p);
  return parts.join(" · ");
}

const CTX_SLIDER_MIN = 1024;
const CTX_SLIDER_STEP = 1024;

/** The ctx value to seed the slider for `model` under `selection`: the persisted
 * per-model value if present, else the model's max (its default window). */
function defaultCtxFor(model: LlmModel | null, selection: RoleSelection): number | null {
  if (model === null) return null;
  const persisted = selection.context_length?.[model.id];
  if (typeof persisted === "number" && persisted > 0) return persisted;
  return model.max_context_length;
}

// --- per-block async lifecycles ----------------------------------------------

type PingState = "idle" | "checking" | "online" | "offline";

type ListState =
  | { status: "idle" }
  | { status: "loading" }
  | { status: "ready"; models: LlmModel[] }
  | { status: "error"; detail: string };

// Per-role swap (PUT /roles/{role}) lifecycle. `detail` carries the failure
// message; `code` lets the UI single out `budget_exceeded` for the over-budget
// warning. `target` is the model id a model-select swap is loading (null for a
// provider/url/ctx swap) so the clicked ROW can show its own spinner/error in
// the restored row-list picker. We stay on the PREVIOUS selection while loading
// and on failure.
type SwapState =
  | { status: "idle" }
  | { status: "loading"; target: string | null }
  | { status: "error"; code: string; detail: string; target: string | null };

export function SettingsControl() {
  const [open, setOpen] = useState(false);
  // The whole per-role map (roles + stt + budget + claude_model). Seeded once on
  // mount from `GET /roles`; each successful PUT replaces it with the server's
  // updated map (so every block re-renders from one source of truth).
  const [roleMap, setRoleMap] = useState<RoleMap | null>(null);
  const fetchedRef = useRef(false);

  // Voice/TTS toggle — unchanged from 0089. The control sits in the VOIX
  // section; the global `M` shortcut lives at this always-mounted root.
  const { voiceEnabled, toggle: toggleVoice } = useVoiceMode();
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key !== "m" && e.key !== "M") return;
      const active = document.activeElement;
      if (active instanceof HTMLInputElement || active instanceof HTMLTextAreaElement) return;
      toggleVoice();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [toggleVoice]);

  // Load the per-role map once on mount. Failure is non-fatal: the modal still
  // renders (the role blocks show a degraded "indisponible" state).
  useEffect(() => {
    const ctrl = new AbortController();
    fetchLlmRoles(ctrl.signal)
      .then((map) => setRoleMap(map))
      .catch(() => {
        // map unavailable — leave null; do not crash the HUD
      });
    return () => ctrl.abort();
  }, []);

  // Re-fetch the map on a fresh open so the picker reflects any server-side
  // change since the last open (e.g. a role swapped from another window).
  useEffect(() => {
    if (open && !fetchedRef.current) {
      fetchedRef.current = true;
      fetchLlmRoles()
        .then((map) => setRoleMap(map))
        .catch(() => {
          /* keep the last map */
        });
    }
    if (!open) fetchedRef.current = false;
  }, [open]);

  // A successful per-role PUT returns the full updated map — adopt it so every
  // block + the budget section re-render from the new state.
  const onRoleUpdated = useCallback((next: RoleMap) => setRoleMap(next), []);

  const budget = roleMap?.budget ?? null;
  const stt = roleMap?.stt ?? null;
  const claudeModel = roleMap?.claude_model ?? "claude-sonnet-4.5";

  return (
    <div className="settings-zone">
      {!open && (
        <div className="settings-status is-ok" data-testid="settings-status">
          <span className="set-dot is-ok" />
          <span className="settings-status-model">par rôle</span>
          <span className="settings-status-state">LLM</span>
        </div>
      )}
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
              <div className="settings-label">MOTEURS LLM · PAR RÔLE</div>
              {roleMap === null ? (
                <div className="set-status is-off" role="alert">
                  <span className="set-dot is-off" />
                  <span className="eng-name">Sélection par rôle indisponible</span>
                  <span className="set-status-state">hors ligne</span>
                </div>
              ) : (
                <div className="set-roles-grid">
                  {LLM_ROLES.map((role) => (
                    <RoleBlock
                      key={role}
                      role={role}
                      selection={roleMap.roles[role]}
                      claudeModel={claudeModel}
                      panelOpen={open}
                      onUpdated={onRoleUpdated}
                    />
                  ))}
                </div>
              )}
            </div>

            {/* STT section (Annexe D) — engine read-only, model informational. */}
            <div className="settings-section">
              <div className="settings-label">TRANSCRIPTION · STT</div>
              <div className="set-status is-ok" data-testid="stt-block">
                <span className="set-dot is-ok" />
                <span className="eng-name">{sttEngineLabel(stt?.engine)}</span>
                <span className="set-status-state">moteur</span>
              </div>
              <div className="set-field">
                <div className="set-field-row">
                  <span className="set-field-proto">modèle</span>
                  <input
                    className="set-input"
                    type="text"
                    spellCheck="false"
                    readOnly
                    value={stt?.model ?? "large-v3-turbo"}
                    data-testid="stt-model"
                    aria-label="Modèle de transcription (STT)"
                  />
                </div>
                <div className="set-field-hint">
                  Moteur whisper.cpp local. Modèle réglable côté serveur (pas d'endpoint d'écriture
                  STT exposé).
                </div>
              </div>
            </div>

            {/* Budget section (issue 0107) — config + over-budget warning. */}
            <BudgetSection budget={budget} />

            <div className="settings-section">
              <div className="settings-label">VOIX</div>
              <button
                type="button"
                className={`set-voice ${voiceEnabled ? "on" : ""}`}
                data-testid="set-voice-toggle"
                aria-pressed={voiceEnabled}
                onClick={toggleVoice}
              >
                <span className="set-voice-glyph" aria-hidden="true">
                  {voiceEnabled ? <SpeakerIcon /> : <SpeakerMutedIcon />}
                </span>
                <span className="set-voice-name">
                  {voiceEnabled ? "Voix activée" : "Voix coupée"}
                </span>
                <span className="set-voice-hint">M</span>
              </button>
            </div>
          </div>
        </>
      )}
    </div>
  );
}

/** Map the backend stt engine id to a friendly label. */
function sttEngineLabel(engine: string | undefined): string {
  if (engine === "whisper_cpp") return "whisper.cpp";
  return engine ?? "whisper.cpp";
}

// =============================================================================
// RoleBlock — one role's provider + (LM Studio: URL + model + ctx) controls.
// =============================================================================
//
// Owns the per-role async state (model list, ping, swap lifecycle) so each of
// the four roles is fully independent. Every mutation goes through ONE blocking
// `PUT /api/llm/roles/{role}` carrying the role's full {provider, base_url,
// lm_model, context_length}; on success the parent adopts the returned map.

type RoleBlockProps = {
  role: LlmRole;
  selection: RoleSelection;
  claudeModel: string;
  panelOpen: boolean;
  onUpdated: (next: RoleMap) => void;
};

function RoleBlock({ role, selection, claudeModel, panelOpen, onUpdated }: RoleBlockProps) {
  const isLM = selection.provider === PROVIDER_LM;

  const [list, setList] = useState<ListState>({ status: "idle" });
  const [swap, setSwap] = useState<SwapState>({ status: "idle" });
  const [ping, setPing] = useState<PingState>("idle");
  // The URL field is LOCAL until committed (mirrors the global picker). Seeded
  // from the role's persisted base_url.
  const [lmUrl, setLmUrl] = useState<string>(selection.base_url ?? LM_URL_DEFAULT);
  // Ctx slider is LOCAL state; the explicit swap fires the blocking PUT.
  const [ctxValue, setCtxValue] = useState<number | null>(null);

  // Reseed the URL field if the persisted base_url changes (e.g. after a swap).
  const persistedUrl = selection.base_url ?? LM_URL_DEFAULT;
  useEffect(() => {
    setLmUrl(persistedUrl);
  }, [persistedUrl]);

  // The role's COMMITTED server (null until a URL is applied). The model list
  // follows THIS — not the live-typed field — so the catalogue reflects the
  // server actually in use for the role. Refetched whenever it changes (after
  // an « OK » URL apply / a provider switch to LM Studio), mirroring the old
  // single-picker behaviour where committing a URL re-pointed the model list.
  const committedUrl = selection.base_url ?? null;

  // Fetch the model list for `url` (the role's committed server, or the active
  // server when null). A 503 surfaces an explicit error row.
  const loadModels = useCallback((url: string | null) => {
    setList({ status: "loading" });
    fetchLlmModels(url ?? undefined)
      .then((models) => setList({ status: "ready", models }))
      .catch((err: unknown) => {
        const detail =
          err instanceof LlmModelsUnavailableError
            ? err.message
            : "Erreur de chargement des modèles";
        setList({ status: "error", detail });
      });
  }, []);

  useEffect(() => {
    if (panelOpen && isLM) loadModels(committedUrl);
  }, [panelOpen, isLM, committedUrl, loadModels]);

  // Live reachability for the role's host — drives the ready/offline badge. A
  // Claude-CLI role is always "ready" (the bridge is local); an LM Studio role
  // pings its OWN base_url. Debounced on the URL, refreshed on an interval.
  useEffect(() => {
    if (!isLM) {
      setPing("idle");
      return;
    }
    const probe = () => {
      setPing("checking");
      pingLm(normalizeLmUrl(lmUrl)).then((r) => setPing(r.reachable ? "online" : "offline"));
    };
    const handle = setTimeout(probe, 400);
    const interval = setInterval(probe, 20000);
    return () => {
      clearTimeout(handle);
      clearInterval(interval);
    };
  }, [isLM, lmUrl]);

  const currentModelId = selection.lm_model;
  const models = list.status === "ready" ? list.models : [];
  const activeModel = models.find((m) => m.id === currentModelId) ?? null;
  const ctxMax = activeModel?.max_context_length ?? null;
  const seededCtx = defaultCtxFor(activeModel, selection);
  // Reseed the ctx slider when the active model / its persisted ctx / its max
  // changes — not on every local drag.
  // biome-ignore lint/correctness/useExhaustiveDependencies: intentional — reseed only on the model id / max / persisted-ctx edges.
  useEffect(() => {
    setCtxValue(seededCtx);
  }, [currentModelId, ctxMax, selection.context_length?.[currentModelId ?? ""]]);

  const swapping = swap.status === "loading";

  // The single per-role mutation. Builds the role's next full selection and
  // PUTs it; the parent adopts the returned map on success. On failure we stay
  // on the previous selection and surface the detail (singling out
  // `budget_exceeded` for the over-budget warning).
  const commit = useCallback(
    (next: RoleSelection, target: string | null = null) => {
      if (swap.status === "loading") return;
      setSwap({ status: "loading", target });
      putLlmRole(role, next)
        .then((map) => {
          onUpdated(map);
          setSwap({ status: "idle" });
        })
        .catch((err: unknown) => {
          if (err instanceof LlmRoleSwapError) {
            setSwap({ status: "error", code: err.code, detail: err.message, target });
          } else {
            setSwap({
              status: "error",
              code: "swap_failed",
              detail: "Échec du changement",
              target,
            });
          }
        });
    },
    [role, swap.status, onUpdated],
  );

  // Provider switch — rebuild this role with the target provider. Switching to
  // Claude drops the LM pins (base_url/model) per the Annexe D shape; switching
  // to LM Studio keeps the field URL + last model.
  const switchProvider = (provider: string) => {
    if (provider === selection.provider) return;
    if (provider === PROVIDER_CLAUDE) {
      // Claude has no reasoning knob — drop the LM pins AND the reasoning level.
      commit({
        provider: PROVIDER_CLAUDE,
        base_url: null,
        lm_model: null,
        context_length: {},
        reasoning: null,
      });
    } else {
      commit({
        provider: PROVIDER_LM,
        base_url: normalizeLmUrl(lmUrl),
        lm_model: selection.lm_model,
        context_length: selection.context_length,
        reasoning: selection.reasoning,
      });
    }
  };

  // Commit the field URL (the role keeps its current model under the new host).
  const applyUrl = () => {
    commit({
      provider: PROVIDER_LM,
      base_url: normalizeLmUrl(lmUrl),
      lm_model: selection.lm_model,
      context_length: selection.context_length,
      reasoning: selection.reasoning,
    });
  };

  // Select a model for this role — no-op if already current. Carries the
  // role's current base_url + ctx so the swap only changes the model.
  const selectModel = (modelId: string) => {
    if (modelId === currentModelId) return;
    commit(
      {
        provider: PROVIDER_LM,
        base_url: selection.base_url ?? normalizeLmUrl(lmUrl),
        lm_model: modelId,
        context_length: selection.context_length,
        reasoning: selection.reasoning,
      },
      modelId,
    );
  };

  // Apply the ctx slider — reload the current model at the slider window.
  const applyCtx = () => {
    if (currentModelId === null || ctxValue === null) return;
    commit({
      provider: PROVIDER_LM,
      base_url: selection.base_url ?? normalizeLmUrl(lmUrl),
      lm_model: currentModelId,
      context_length: { ...selection.context_length, [currentModelId]: ctxValue },
      reasoning: selection.reasoning,
    });
  };

  // Set the LM Studio reasoning level for this role — no-op if unchanged.
  // Reasoning is a per-REQUEST chat param, so this hits the LIGHTWEIGHT
  // `/reasoning` endpoint: it persists the level + refreshes the role's client
  // WITHOUT reloading the model or running the budget policy. We still drive the
  // shared swap state for in-flight feedback (it returns fast — no SDK load).
  const selectReasoning = (level: string | null) => {
    if (level === (selection.reasoning ?? null)) return;
    if (swap.status === "loading") return;
    setSwap({ status: "loading", target: null });
    putLlmRoleReasoning(role, level)
      .then((map) => {
        onUpdated(map);
        setSwap({ status: "idle" });
      })
      .catch((err: unknown) => {
        if (err instanceof LlmRoleSwapError) {
          setSwap({ status: "error", code: err.code, detail: err.message, target: null });
        } else {
          setSwap({
            status: "error",
            code: "swap_failed",
            detail: "Échec du réglage",
            target: null,
          });
        }
      });
  };

  // Per-role ready/offline badge. Claude → always ready; LM Studio → live ping.
  const badge = roleBadge(isLM, ping, swapping);
  const urlBody = lmUrl.replace(/^https?:\/\//i, "");

  return (
    <div className="set-role" data-testid={`set-role-${role}`} data-provider={selection.provider}>
      <div className="set-role-head">
        <span className="set-role-name">{ROLE_LABEL[role]}</span>
        <span
          className={`set-role-badge ${badge.cls}`}
          data-testid={`set-role-badge-${role}`}
          data-state={badge.state}
        >
          <span className={`set-dot ${badge.cls}`} />
          {badge.label}
        </span>
      </div>

      {/* segmented provider toggle — Claude CLI ↔ LM Studio, per role. */}
      <div className="set-seg" role="radiogroup" aria-label={`Moteur · ${ROLE_LABEL[role]}`}>
        <button
          type="button"
          // biome-ignore lint/a11y/useSemanticElements: ARIA radiogroup of buttons — <button> is the correct focusable element; the role only adds radio semantics.
          role="radio"
          aria-checked={!isLM}
          disabled={swapping}
          className={`set-seg-btn ${!isLM ? "on" : ""}`}
          onClick={() => switchProvider(PROVIDER_CLAUDE)}
        >
          <span className="set-seg-glyph">&gt;_</span>Claude CLI
        </button>
        <button
          type="button"
          // biome-ignore lint/a11y/useSemanticElements: ARIA radiogroup of buttons — <button> is the correct focusable element; the role only adds radio semantics.
          role="radio"
          aria-checked={isLM}
          disabled={swapping}
          className={`set-seg-btn ${isLM ? "on" : ""}`}
          onClick={() => switchProvider(PROVIDER_LM)}
        >
          <span className="set-seg-glyph">▦</span>LM Studio
        </button>
      </div>

      {swapping && (
        <output className="set-status is-loading" aria-busy="true">
          <span className="set-dot is-loading" />
          <span className="eng-name">Application…</span>
          <span className="set-status-state">chargement…</span>
        </output>
      )}

      {swap.status === "error" && swap.target === null && (
        <div
          className={`set-status is-off ${swap.code === "budget_exceeded" ? "is-budget" : ""}`}
          role="alert"
          data-testid={`set-role-error-${role}`}
        >
          <span className="set-dot is-off" />
          <span className="eng-name">{swap.detail}</span>
          <span className="set-status-state">
            {swap.code === "budget_exceeded" ? "budget" : "échec"}
          </span>
        </div>
      )}

      {!isLM ? (
        <div className="set-detail">
          <div className="set-field-hint">
            <b>Pont CLI local — modèle {claudeModel}, aucune URL requise.</b>
          </div>
        </div>
      ) : (
        <div className="set-detail">
          {/* server URL — http:// prefix + editable body + commit. */}
          <div className="set-field">
            <div className="set-field-row">
              <span className="set-field-proto">http://</span>
              <input
                className="set-input"
                type="text"
                inputMode="url"
                spellCheck="false"
                value={urlBody}
                placeholder="localhost:1234/v1"
                disabled={swapping}
                onChange={(e) => setLmUrl(`http://${e.target.value.replace(/^https?:\/\//i, "")}`)}
                onKeyDown={(e) => {
                  if (e.key === "Enter") applyUrl();
                }}
                aria-label={`URL du serveur · ${ROLE_LABEL[role]}`}
              />
              <button
                type="button"
                className="set-url-apply"
                data-testid={`set-role-url-apply-${role}`}
                disabled={swapping || normalizeLmUrl(lmUrl) === (selection.base_url ?? "")}
                onClick={applyUrl}
              >
                OK
              </button>
            </div>
          </div>

          {/* live local-model list — from GET /api/llm/models (active server).
            Restored pre-0108 row-list layout: clicking a non-current row fires
            the blocking per-role swap; the clicked row shows its own spinner. */}
          <div className="set-field">
            <div className="set-models-head">
              <span className="settings-label">MODÈLE</span>
              <span className="set-models-count">
                {list.status === "ready"
                  ? `${list.models.length} dispo`
                  : list.status === "loading"
                    ? "chargement…"
                    : list.status === "error"
                      ? "indisponible"
                      : ""}
              </span>
            </div>
            <div
              className="set-models"
              // biome-ignore lint/a11y/useSemanticElements: ARIA listbox ported from the mockup; a native <select> can't carry the per-row metadata chrome. tabIndex makes the interactive role focusable.
              role="listbox"
              tabIndex={0}
              aria-label={`Modèle · ${ROLE_LABEL[role]}`}
            >
              {list.status === "error" && (
                <div className="set-model is-error" role="alert">
                  <span className="set-model-mark">⚠</span>
                  <span className="set-model-name">Serveur LM Studio injoignable</span>
                  <span className="set-model-spec">{list.detail}</span>
                </div>
              )}
              {/* When the persisted model isn't in the live list (different host
                / not loaded), keep it as a selected row so we don't silently
                drop the role's pin. */}
              {list.status === "ready" &&
                currentModelId !== null &&
                !models.some((m) => m.id === currentModelId) && (
                  <div className="set-model on" aria-selected="true">
                    <span className="set-model-mark">◆</span>
                    <span className="set-model-name">{currentModelId}</span>
                    <span className="set-model-spec">non chargé sur cet hôte</span>
                  </div>
                )}
              {list.status === "ready" &&
                models.map((m) => {
                  const on = m.id === currentModelId;
                  const isSwapping = swap.status === "loading" && swap.target === m.id;
                  const swapFailed = swap.status === "error" && swap.target === m.id;
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
                      disabled={swapping}
                      className={`set-model ${on ? "on" : ""} ${isSwapping ? "is-loading" : ""} ${swapFailed ? "is-error" : ""}`}
                      data-testid={`set-role-model-${role}-${m.id}`}
                      onClick={() => selectModel(m.id)}
                    >
                      <span className="set-model-mark">{on ? "◆" : "◇"}</span>
                      <span className="set-model-name">{m.id}</span>
                      <span className="set-model-spec">
                        {swapFailed ? swap.detail : modelSpec(m)}
                      </span>
                      <span className="set-model-ram">{ramLabel}</span>
                    </button>
                  );
                })}
            </div>
          </div>

          {/* ctx-length slider + Apply — only with a known max for the active
            model. Dragging mutates LOCAL state; Apply fires the blocking PUT. */}
          {activeModel !== null && ctxMax !== null && ctxValue !== null && (
            <div className="set-ctx" data-testid={`set-role-ctx-${role}`}>
              <div className="set-ctx-head">
                <span className="settings-label">CONTEXTE</span>
                <span className="set-ctx-val">{ctxValue.toLocaleString()} tok</span>
              </div>
              <input
                type="range"
                className="set-ctx-slider"
                data-testid={`set-role-ctx-slider-${role}`}
                aria-label={`Longueur de contexte · ${ROLE_LABEL[role]}`}
                min={Math.min(CTX_SLIDER_MIN, ctxMax)}
                max={ctxMax}
                step={CTX_SLIDER_STEP}
                value={Math.min(ctxValue, ctxMax)}
                disabled={swapping}
                onChange={(e) => setCtxValue(Number(e.target.value))}
              />
              <div className="set-ctx-actions">
                <span className="set-ctx-max">max {ctxMax.toLocaleString()}</span>
                <button
                  type="button"
                  className="set-ctx-apply"
                  data-testid={`set-role-ctx-apply-${role}`}
                  disabled={swapping}
                  onClick={applyCtx}
                >
                  Appliquer
                </button>
              </div>
            </div>
          )}

          {/* reasoning level — LM Studio `reasoning` body field. "auto" omits
            it (model default); the others map 1:1. Errors server-side if the
            model doesn't support the chosen level (surfaced on the role error
            row). Each click fires the blocking per-role swap. */}
          <div className="set-reason" data-testid={`set-role-reason-${role}`}>
            <div className="set-reason-head">
              <span className="settings-label">RAISONNEMENT</span>
            </div>
            <div
              className="set-reason-seg"
              role="radiogroup"
              aria-label={`Raisonnement · ${ROLE_LABEL[role]}`}
            >
              {REASONING_OPTIONS.map((opt) => {
                const on = (selection.reasoning ?? null) === opt.value;
                return (
                  <button
                    key={opt.label}
                    type="button"
                    // biome-ignore lint/a11y/useSemanticElements: ARIA radiogroup of buttons — <button> is the correct focusable element; the role only adds radio semantics.
                    role="radio"
                    aria-checked={on}
                    disabled={swapping}
                    className={`set-reason-btn ${on ? "on" : ""}`}
                    data-testid={`set-role-reason-${role}-${opt.value ?? "auto"}`}
                    onClick={() => selectReasoning(opt.value)}
                  >
                    {opt.label}
                  </button>
                );
              })}
            </div>
          </div>
        </div>
      )}
    </div>
  );
}

/** Resolve the per-role ready/offline badge. A swap-in-flight shows "…"; a
 * Claude role is always ready; an LM Studio role tracks the live ping. */
function roleBadge(
  isLM: boolean,
  ping: PingState,
  swapping: boolean,
): { cls: string; state: string; label: string } {
  if (swapping) return { cls: "is-loading", state: "loading", label: "…" };
  if (!isLM) return { cls: "is-ok", state: "ready", label: "prêt" };
  if (ping === "online") return { cls: "is-ok", state: "ready", label: "prêt" };
  if (ping === "checking") return { cls: "is-loading", state: "checking", label: "ping" };
  return { cls: "is-off", state: "offline", label: "hors ligne" };
}

// =============================================================================
// BudgetSection — the per-host model-budget CONFIG (issue 0107).
// =============================================================================
//
// Renders the persisted budget block: the global ceiling (or "détection auto"
// when null), the OS reserve, and any per-host overrides. There is NO live
// resident-usage endpoint, so this shows config + the over-budget refusal
// (raised on a per-role PUT with `budget_exceeded`), NOT a live gauge.

function BudgetSection({ budget }: { budget: RoleMap["budget"] | null }) {
  if (budget === null) {
    return (
      <div className="settings-section">
        <div className="settings-label">BUDGET MÉMOIRE</div>
        <div className="set-field-hint">Budget indisponible.</div>
      </div>
    );
  }
  const overrides = Object.entries(budget.per_host_override);
  return (
    <div className="settings-section" data-testid="budget-section">
      <div className="settings-label">BUDGET MÉMOIRE · PAR HOST</div>
      <div className="set-budget-rows">
        <div className="set-budget-row">
          <span className="set-budget-key">Plafond</span>
          <span className="set-budget-val" data-testid="budget-ceiling">
            {budget.ceiling_gib === null
              ? "détection auto (RAM − réserve)"
              : `${budget.ceiling_gib.toLocaleString()} Gio`}
          </span>
        </div>
        <div className="set-budget-row">
          <span className="set-budget-key">Réserve OS</span>
          <span className="set-budget-val" data-testid="budget-reserve">
            {budget.reserve_gib.toLocaleString()} Gio
          </span>
        </div>
        {overrides.map(([host, gib]) => (
          <div className="set-budget-row" key={host} data-testid={`budget-override-${host}`}>
            <span className="set-budget-key">{host}</span>
            <span className="set-budget-val">{gib.toLocaleString()} Gio</span>
          </div>
        ))}
      </div>
      <div className="set-field-hint">
        L'usage résident en direct n'est pas exposé par le backend (vérification au chargement). Un
        modèle refusé pour dépassement s'affiche sur le rôle concerné.
      </div>
    </div>
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
