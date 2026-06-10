// SetupScreen — the per-role model gate shown BEFORE the main HUD (Sphere)
// mounts. Launched first by `App` so the user wires every orchestrator role
// (jarvis / thinker / draft / subagent) on a calm dedicated screen instead of
// discovering a misconfigured backend after the HUD is already up.
//
// Flow (PRD 0013 → per-role v2):
//   1. Optional LM Studio server URL — live ping + model-list fetch. Left
//      empty (or unreachable), only the Claude entry is offered per role.
//   2. ONE flat select per role mixing the LM Studio models and the Claude
//      model — no intermediate provider toggle; the provider is inferred from
//      the picked entry and shown as a passive badge.
//   3. Démarrer → a loading phase that commits each role sequentially via
//      `PUT /api/llm/roles/{role}` (blocking — the backend actually loads the
//      model) with a live per-role status board.
//
// Per-role tuning (reasoning, ctx sliders, budget) stays in the in-HUD
// Réglages panel; this screen only wires provider + model per role.

import { useCallback, useEffect, useRef, useState } from "react";
import {
  LLM_ROLES,
  type LlmModel,
  LlmModelsUnavailableError,
  type LlmRole,
  type RoleMap,
  type RoleSelection,
  fetchLlmModels,
  fetchLlmRoles,
  pingLm,
  putLlmRole,
} from "../../lib/llmApi";
import "./SetupScreen.css";

/** localStorage key — set once the user completes setup so later launches skip
 * straight to the HUD (unless a configured server is unreachable, in which
 * case `App` re-shows this screen). */
export const SETUP_COMPLETE_KEY = "bob.setupComplete";

/** Select-value encoding: a Claude pick is the literal `"claude"`; an LM Studio
 * pick is `"lm:<model id>"` (the prefix keeps an LM model that happens to be
 * named "claude" unambiguous). */
const CLAUDE_CHOICE = "claude";
const LM_PREFIX = "lm:";

const ROLE_META: { key: LlmRole; label: string; hint: string }[] = [
  { key: "jarvis", label: "Jarvis · voix", hint: "Tour parlé, face utilisateur" },
  { key: "thinker", label: "Penseur", hint: "Raisonnement toujours chaud" },
  { key: "draft", label: "Brouillon", hint: "Anticipation spéculative" },
  { key: "subagent", label: "Sous-agent", hint: "Délégation / outils" },
];

type CommitState =
  | { state: "pending" }
  | { state: "loading"; startedAt: number }
  | { state: "done"; seconds: number }
  | { state: "error"; detail: string };

type Props = {
  /** Called once every role is committed on a reachable provider/model. */
  onReady: () => void;
};

export function SetupScreen({ onReady }: Props) {
  const [screen, setScreen] = useState<"form" | "loading">("form");
  const [baseUrl, setBaseUrl] = useState("");
  const [claudeModel, setClaudeModel] = useState("Claude CLI");
  const [models, setModels] = useState<LlmModel[]>([]);
  const [choices, setChoices] = useState<Record<LlmRole, string>>({
    jarvis: CLAUDE_CHOICE,
    thinker: CLAUDE_CHOICE,
    draft: CLAUDE_CHOICE,
    subagent: CLAUDE_CHOICE,
  });
  const [reachable, setReachable] = useState<boolean | null>(null);
  const [modelsError, setModelsError] = useState<string | null>(null);
  const [loadingModels, setLoadingModels] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [commits, setCommits] = useState<Record<LlmRole, CommitState>>({
    jarvis: { state: "pending" },
    thinker: { state: "pending" },
    draft: { state: "pending" },
    subagent: { state: "pending" },
  });
  // Re-render tick while a role is loading so its elapsed counter advances.
  const [, setClock] = useState(0);
  // The seed (fetchLlmRoles) is async; if the user starts interacting before it
  // resolves, the seed must NOT clobber what they typed/picked (the race that
  // made an edited URL "revert" and the ping keep probing the stale server).
  const userTouchedRef = useRef(false);
  // Per-role ctx maps from the seed, round-tripped on commit so a re-setup
  // never wipes a ctx window pinned earlier from the in-HUD panel.
  const seedRolesRef = useRef<Record<string, RoleSelection>>({});

  // Seed the form from the backend's CURRENT per-role map so the URL and the
  // per-role picks shown are the ones really in use.
  useEffect(() => {
    let cancelled = false;
    fetchLlmRoles()
      .then((map: RoleMap) => {
        if (cancelled || userTouchedRef.current) return;
        seedRolesRef.current = map.roles;
        setClaudeModel(map.claude_model || "Claude CLI");
        const seeded: Partial<Record<LlmRole, string>> = {};
        let seedUrl = "";
        for (const role of LLM_ROLES) {
          const sel = map.roles[role];
          if (!sel) continue;
          if (sel.provider === "lm_studio" && sel.lm_model) {
            seeded[role] = `${LM_PREFIX}${sel.lm_model}`;
            if (!seedUrl && sel.base_url) seedUrl = sel.base_url;
          } else {
            seeded[role] = CLAUDE_CHOICE;
          }
        }
        if (seedUrl) setBaseUrl(seedUrl);
        setChoices((cur) => ({ ...cur, ...seeded }));
      })
      .catch(() => {
        // Best-effort: a fresh install / down backend just starts blank.
      });
    return () => {
      cancelled = true;
    };
  }, []);

  // Ping the candidate URL (debounced) so the user sees live reachability
  // before committing — same probe the in-HUD panel uses.
  useEffect(() => {
    const url = baseUrl.trim();
    if (!url) {
      setReachable(null);
      setModels([]);
      setModelsError(null);
      return;
    }
    let cancelled = false;
    const handle = setTimeout(() => {
      pingLm(url).then((res) => {
        if (!cancelled) setReachable(res.reachable);
      });
    }, 300);
    return () => {
      cancelled = true;
      clearTimeout(handle);
    };
  }, [baseUrl]);

  const refreshModels = useCallback(async () => {
    const url = baseUrl.trim();
    if (!url) return;
    setLoadingModels(true);
    setModelsError(null);
    try {
      const list = await fetchLlmModels(url);
      setModels(list);
      // Drop any LM pick that no longer exists on this server.
      setChoices((cur) => {
        const next = { ...cur };
        for (const role of LLM_ROLES) {
          const v = next[role];
          if (v.startsWith(LM_PREFIX) && !list.some((m) => m.id === v.slice(LM_PREFIX.length))) {
            next[role] = CLAUDE_CHOICE;
          }
        }
        return next;
      });
    } catch (err) {
      setModels([]);
      setModelsError(
        err instanceof LlmModelsUnavailableError ? err.message : "Liste des modèles indisponible",
      );
    } finally {
      setLoadingModels(false);
    }
  }, [baseUrl]);

  // (Re)fetch the model list whenever the server becomes reachable.
  useEffect(() => {
    if (reachable) void refreshModels();
  }, [reachable, refreshModels]);

  // 1s tick driving the "chargement… Ns" counters on the loading screen.
  useEffect(() => {
    if (screen !== "loading") return;
    const handle = setInterval(() => setClock((c) => c + 1), 1000);
    return () => clearInterval(handle);
  }, [screen]);

  const lmAvailable = reachable === true && models.length > 0;

  const start = useCallback(async () => {
    setError(null);
    setScreen("loading");
    setCommits({
      jarvis: { state: "pending" },
      thinker: { state: "pending" },
      draft: { state: "pending" },
      subagent: { state: "pending" },
    });
    const url = baseUrl.trim();
    for (const { key } of ROLE_META) {
      const choice = choices[key];
      const startedAt = performance.now();
      setCommits((cur) => ({ ...cur, [key]: { state: "loading", startedAt } }));
      const seed = seedRolesRef.current[key];
      const body: RoleSelection =
        choice === CLAUDE_CHOICE
          ? { provider: "claude_cli", base_url: null, lm_model: null, context_length: {}, reasoning: null }
          : {
              provider: "lm_studio",
              base_url: url,
              lm_model: choice.slice(LM_PREFIX.length),
              // Round-trip any ctx windows pinned earlier so a re-setup never
              // silently resets them.
              context_length: seed?.context_length ?? {},
              reasoning: seed?.provider === "lm_studio" ? (seed?.reasoning ?? null) : null,
            };
      try {
        await putLlmRole(key, body);
        const seconds = Math.round((performance.now() - startedAt) / 1000);
        setCommits((cur) => ({ ...cur, [key]: { state: "done", seconds } }));
      } catch (err) {
        const detail = err instanceof Error ? err.message : "Échec du chargement";
        setCommits((cur) => ({ ...cur, [key]: { state: "error", detail } }));
        setError(detail);
        return;
      }
    }
    window.localStorage.setItem(SETUP_COMPLETE_KEY, "1");
    onReady();
  }, [baseUrl, choices, onReady]);

  // Every role resolves to a valid target: Claude always works; an LM pick
  // needs the server reachable with that model still listed.
  const canStart =
    screen === "form" &&
    ROLE_META.every(({ key }) => {
      const v = choices[key];
      if (v === CLAUDE_CHOICE) return true;
      return lmAvailable && models.some((m) => m.id === v.slice(LM_PREFIX.length));
    });

  const doneCount = ROLE_META.filter(({ key }) => commits[key].state === "done").length;

  if (screen === "loading") {
    return (
      <div className="setup-screen">
        <div className="setup-card setup-card--wide">
          <h1 className="setup-title">BOB</h1>
          <p className="setup-subtitle">
            Chargement des modèles — {doneCount}/{ROLE_META.length} rôle
            {doneCount > 1 ? "s" : ""} prêt{doneCount > 1 ? "s" : ""}
          </p>

          <ul className="setup-commit-list" data-testid="setup-loading">
            {ROLE_META.map(({ key, label }) => {
              const commit = commits[key];
              const choice = choices[key];
              const modelId =
                choice === CLAUDE_CHOICE ? claudeModel : choice.slice(LM_PREFIX.length);
              const meta = models.find((m) => m.id === modelId);
              return (
                <li key={key} className={`setup-commit setup-commit--${commit.state}`}>
                  <span className="setup-commit-status" aria-hidden="true">
                    {commit.state === "pending" && "·"}
                    {commit.state === "loading" && <span className="setup-spinner" />}
                    {commit.state === "done" && "✓"}
                    {commit.state === "error" && "✗"}
                  </span>
                  <span className="setup-commit-role">{label}</span>
                  <span className="setup-commit-model">
                    {modelId}
                    {meta?.quantisation ? (
                      <em className="setup-commit-quant"> · {meta.quantisation}</em>
                    ) : null}
                  </span>
                  <span className="setup-commit-detail">
                    {commit.state === "pending" && "en attente"}
                    {commit.state === "loading" &&
                      `chargement… ${Math.max(0, Math.round((performance.now() - commit.startedAt) / 1000))}s`}
                    {commit.state === "done" &&
                      (commit.seconds < 1 ? "adopté (déjà résident)" : `chargé en ${commit.seconds}s`)}
                    {commit.state === "error" && "échec"}
                  </span>
                </li>
              );
            })}
          </ul>

          <p className="setup-hint">
            Un modèle déjà résident dans LM Studio est adopté sans rechargement&nbsp;; les rôles
            partageant le même modèle se suivent instantanément.
          </p>

          {error ? (
            <>
              <p className="setup-error">{error}</p>
              <button type="button" className="setup-start" onClick={() => setScreen("form")}>
                Retour à la configuration
              </button>
            </>
          ) : null}
        </div>
      </div>
    );
  }

  return (
    <div className="setup-screen">
      <div className="setup-card setup-card--wide">
        <h1 className="setup-title">BOB</h1>
        <p className="setup-subtitle">Un serveur LM Studio (optionnel), puis un modèle par rôle.</p>

        <label className="setup-field">
          <span className="setup-label">Serveur LM Studio — optionnel</span>
          <div className="setup-url-row">
            <input
              className="setup-input"
              type="text"
              value={baseUrl}
              placeholder="http://localhost:1234/v1"
              onChange={(e) => {
                userTouchedRef.current = true;
                setBaseUrl(e.target.value);
              }}
              spellCheck={false}
            />
            <span
              className={`setup-ping ${reachable === true ? "is-online" : reachable === false ? "is-offline" : ""}`}
              data-testid="setup-ping"
            >
              {baseUrl.trim() === ""
                ? "non configuré"
                : reachable === true
                  ? `en ligne · ${models.length} modèle${models.length > 1 ? "s" : ""}`
                  : reachable === false
                    ? "injoignable"
                    : "…"}
            </span>
          </div>
        </label>

        {modelsError ? <p className="setup-error">{modelsError}</p> : null}
        {loadingModels ? <p className="setup-hint">Lecture des modèles…</p> : null}

        <div className="setup-roles" data-testid="setup-roles">
          {ROLE_META.map(({ key, label, hint }) => {
            const choice = choices[key];
            const isClaude = choice === CLAUDE_CHOICE;
            return (
              <div className="setup-role-row" key={key}>
                <div className="setup-role-id">
                  <span className="setup-role-label">{label}</span>
                  <span className="setup-role-hint">{hint}</span>
                </div>
                <select
                  className="setup-role-select"
                  aria-label={`Modèle ${label}`}
                  value={choice}
                  onChange={(e) => {
                    userTouchedRef.current = true;
                    setChoices((cur) => ({ ...cur, [key]: e.target.value }));
                  }}
                >
                  {lmAvailable ? (
                    <optgroup label={`LM Studio · ${baseUrl.trim()}`}>
                      {models.map((m) => (
                        <option key={m.id} value={`${LM_PREFIX}${m.id}`}>
                          {m.id}
                        </option>
                      ))}
                    </optgroup>
                  ) : null}
                  <optgroup label="Claude">
                    <option value={CLAUDE_CHOICE}>{claudeModel}</option>
                  </optgroup>
                </select>
                <span className={`setup-role-badge ${isClaude ? "is-claude" : "is-lm"}`}>
                  {isClaude ? "Claude" : "LM Studio"}
                </span>
              </div>
            );
          })}
        </div>

        {error ? <p className="setup-error">{error}</p> : null}

        <button
          type="button"
          className="setup-start"
          disabled={!canStart}
          onClick={() => void start()}
        >
          Démarrer
        </button>
      </div>
    </div>
  );
}
