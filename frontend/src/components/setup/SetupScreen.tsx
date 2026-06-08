// SetupScreen — the model/provider gate shown BEFORE the main HUD (Sphere)
// mounts. Launched first by `App` so the user picks (and, for LM Studio,
// actually loads + confirms) a model on a calm dedicated screen instead of
// discovering a misconfigured backend after the HUD is already up.
//
// It drives the v1 GLOBAL selection surface (`/api/llm/selection`) — the right
// granularity for "pick one model to start with"; per-role tuning stays in the
// in-HUD Réglages panel. On confirm it commits the choice, waits for the model
// to actually be resident, persists a `setup_complete` flag, and calls
// `onReady` to enter the HUD.
//
// Why a real gate (not just the in-HUD panel): the reported pain was a
// misconfigured backend launching straight into the HUD with the wrong URL /
// no model. Committing the URL + model here means no role is ever left with a
// null base_url (the "wrong URL shown" bug) and the model is explicitly loaded
// once — never the JIT/duplicate-load surprise.

import { useCallback, useEffect, useState } from "react";
import {
  type LlmModel,
  LlmModelsUnavailableError,
  type LlmSelection,
  fetchLlmModels,
  fetchLlmSelection,
  pingLm,
  putLlmBaseUrl,
  putLlmModel,
  putLlmProvider,
} from "../../lib/llmApi";
import "./SetupScreen.css";

/** localStorage key — set once the user completes setup so later launches skip
 * straight to the HUD (unless the configured LM Studio server is unreachable,
 * in which case `App` re-shows this screen). */
export const SETUP_COMPLETE_KEY = "bob.setupComplete";

type Provider = "lm_studio" | "claude_cli";

type Props = {
  /** Called once the user has confirmed a reachable provider/model. */
  onReady: () => void;
};

export function SetupScreen({ onReady }: Props) {
  const [provider, setProvider] = useState<Provider>("lm_studio");
  const [baseUrl, setBaseUrl] = useState("");
  const [claudeModel, setClaudeModel] = useState<string>("Claude CLI");
  const [models, setModels] = useState<LlmModel[]>([]);
  const [selectedModel, setSelectedModel] = useState<string | null>(null);
  const [reachable, setReachable] = useState<boolean | null>(null);
  const [modelsError, setModelsError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [loadingModels, setLoadingModels] = useState(false);

  // Seed the form from the backend's CURRENT (effective) selection so the URL
  // shown is the one really in use — never a hardcoded localhost placeholder.
  useEffect(() => {
    let cancelled = false;
    fetchLlmSelection()
      .then((sel: LlmSelection) => {
        if (cancelled) return;
        setProvider(sel.provider === "claude_cli" ? "claude_cli" : "lm_studio");
        setBaseUrl(sel.base_url ?? "");
        setClaudeModel(sel.claude_model || "Claude CLI");
        setSelectedModel(sel.lm_model);
      })
      .catch(() => {
        // Best-effort: a fresh install / down backend just starts blank.
      });
    return () => {
      cancelled = true;
    };
  }, []);

  // Ping the candidate URL (debounced via the effect dependency) so the user
  // sees live reachability before committing — same probe the in-HUD panel uses.
  useEffect(() => {
    if (provider !== "lm_studio") return;
    const url = baseUrl.trim();
    if (!url) {
      setReachable(null);
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
  }, [provider, baseUrl]);

  const refreshModels = useCallback(async () => {
    if (provider !== "lm_studio") return;
    const url = baseUrl.trim();
    if (!url) return;
    setLoadingModels(true);
    setModelsError(null);
    try {
      const list = await fetchLlmModels(url);
      setModels(list);
      // Default the selection to the already-loaded model when nothing picked.
      setSelectedModel((cur) => cur ?? list.find((m) => m.loaded)?.id ?? list[0]?.id ?? null);
    } catch (err) {
      setModels([]);
      setModelsError(
        err instanceof LlmModelsUnavailableError ? err.message : "Liste des modèles indisponible",
      );
    } finally {
      setLoadingModels(false);
    }
  }, [provider, baseUrl]);

  // (Re)fetch the model list whenever the server becomes reachable.
  useEffect(() => {
    if (provider === "lm_studio" && reachable) void refreshModels();
  }, [provider, reachable, refreshModels]);

  const start = useCallback(async () => {
    setBusy(true);
    setError(null);
    try {
      if (provider === "claude_cli") {
        await putLlmProvider("claude_cli");
      } else {
        const url = baseUrl.trim();
        if (!selectedModel) {
          setError("Choisissez un modèle pour démarrer.");
          setBusy(false);
          return;
        }
        // Commit the URL first (server validates reachability → throws on a
        // dead server) so the role never lands on a null/placeholder base_url.
        await putLlmBaseUrl(url);
        // Then load + pin the chosen model. This is the SINGLE explicit load —
        // the backend adopts it if LM Studio already has it resident.
        await putLlmModel(selectedModel);
      }
      window.localStorage.setItem(SETUP_COMPLETE_KEY, "1");
      onReady();
    } catch (err) {
      setError(err instanceof Error ? err.message : "Échec du démarrage");
      setBusy(false);
    }
  }, [provider, baseUrl, selectedModel, onReady]);

  const canStart =
    !busy && (provider === "claude_cli" || (reachable === true && selectedModel !== null));

  return (
    <div className="setup-screen">
      <div className="setup-card">
        <h1 className="setup-title">BOB</h1>
        <p className="setup-subtitle">Choisissez le moteur avant de démarrer.</p>

        <div className="setup-providers" role="radiogroup" aria-label="Fournisseur">
          <button
            type="button"
            className={`setup-provider ${provider === "lm_studio" ? "is-active" : ""}`}
            aria-pressed={provider === "lm_studio"}
            onClick={() => setProvider("lm_studio")}
          >
            LM Studio
          </button>
          <button
            type="button"
            className={`setup-provider ${provider === "claude_cli" ? "is-active" : ""}`}
            aria-pressed={provider === "claude_cli"}
            onClick={() => setProvider("claude_cli")}
          >
            Claude CLI
          </button>
        </div>

        {provider === "lm_studio" ? (
          <>
            <label className="setup-field">
              <span className="setup-label">Serveur LM Studio</span>
              <div className="setup-url-row">
                <input
                  className="setup-input"
                  type="text"
                  value={baseUrl}
                  placeholder="http://localhost:1234/v1"
                  onChange={(e) => setBaseUrl(e.target.value)}
                  spellCheck={false}
                />
                <span
                  className={`setup-ping ${reachable === true ? "is-online" : reachable === false ? "is-offline" : ""}`}
                  data-testid="setup-ping"
                >
                  {reachable === true ? "en ligne" : reachable === false ? "injoignable" : "…"}
                </span>
              </div>
            </label>

            <div className="setup-models" data-testid="setup-models">
              {loadingModels ? (
                <p className="setup-hint">Chargement des modèles…</p>
              ) : modelsError ? (
                <p className="setup-error">{modelsError}</p>
              ) : models.length === 0 ? (
                <p className="setup-hint">
                  {reachable ? "Aucun modèle disponible." : "En attente d'un serveur joignable…"}
                </p>
              ) : (
                <ul className="setup-model-list">
                  {models.map((m) => (
                    <li key={m.id}>
                      <button
                        type="button"
                        className={`setup-model ${selectedModel === m.id ? "is-active" : ""}`}
                        aria-pressed={selectedModel === m.id}
                        onClick={() => setSelectedModel(m.id)}
                      >
                        <span className="setup-model-id">{m.id}</span>
                        {m.loaded ? <span className="setup-model-loaded">chargé</span> : null}
                      </button>
                    </li>
                  ))}
                </ul>
              )}
            </div>
          </>
        ) : (
          <p className="setup-hint">
            Modèle&nbsp;: <strong>{claudeModel}</strong> (via le binaire <code>claude</code>).
          </p>
        )}

        {error ? <p className="setup-error">{error}</p> : null}

        <button
          type="button"
          className="setup-start"
          disabled={!canStart}
          onClick={() => void start()}
        >
          {busy ? "Démarrage…" : "Démarrer"}
        </button>
      </div>
    </div>
  );
}
