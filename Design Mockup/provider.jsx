// provider.jsx — LLM provider picker. Choose the engine that backs Bob:
//   • Claude CLI   — local CLI bridge, fixed model
//   • LM Studio    — local server, pick from loaded GGUF models
// Selection persists via tweaks (provider / lmModel) and feeds the live
// agent-console footer so the running model label stays truthful.

const { useState: useStateP, useEffect: useEffectP, useRef: useRefP } = React;

const CLAUDE_MODEL = 'claude-sonnet-4.5';

// LM Studio catalogue — local GGUF builds with params + quantisation, the way
// LM Studio lists them. `ram` is the resident footprint when loaded.
const LM_MODELS = [
  { id: 'qwen2.5-coder-32b',  name: 'Qwen2.5 Coder',       params: '32B', quant: 'Q4_K_M', ram: '18.5 GB' },
  { id: 'llama-3.3-70b',      name: 'Llama 3.3 Instruct',  params: '70B', quant: 'Q3_K_L', ram: '37.1 GB' },
  { id: 'mistral-small-3',    name: 'Mistral Small 3',     params: '24B', quant: 'Q4_K_M', ram: '14.3 GB' },
  { id: 'gemma-2-27b',        name: 'Gemma 2',             params: '27B', quant: 'Q5_K_M', ram: '19.4 GB' },
  { id: 'deepseek-r1-14b',    name: 'DeepSeek R1 Distill', params: '14B', quant: 'Q4_K_M', ram: '8.9 GB'  },
  { id: 'phi-4',              name: 'Phi-4',               params: '14B', quant: 'Q4_K_M', ram: '8.4 GB'  },
];

function lmModelById(id) {
  return LM_MODELS.find((m) => m.id === id) || LM_MODELS[0];
}

// Public helper: the label the rest of the app shows for the active engine.
function activeModelLabel(provider, lmModel) {
  if (provider === 'lmstudio') {
    const m = lmModelById(lmModel);
    return `${m.id} · local`;
  }
  return `${CLAUDE_MODEL} · cli`;
}

function ProviderPicker({ provider, lmModel, onProvider, onModel }) {
  const [open, setOpen] = useStateP(false);
  // brief load animation when the engine/model changes — local models take a
  // moment to page into VRAM; the CLI bridge just reconnects.
  const [status, setStatus] = useStateP('ready'); // ready | loading
  const firstRun = useRefP(true);

  useEffectP(() => {
    if (firstRun.current) { firstRun.current = false; return; }
    setStatus('loading');
    const ms = provider === 'lmstudio' ? 1500 : 700;
    const id = setTimeout(() => setStatus('ready'), ms);
    return () => clearTimeout(id);
  }, [provider, lmModel]);

  const isLM = provider === 'lmstudio';
  const m = lmModelById(lmModel);
  const loading = status === 'loading';

  const pickModel = (id) => {
    if (id !== lmModel) onModel(id);
    setOpen(false);
  };

  const switchProvider = (p) => {
    if (p === provider) return;
    onProvider(p);
    if (p !== 'lmstudio') setOpen(false);
  };

  return (
    <div className={`pv ${loading ? 'is-loading' : ''} ${isLM ? 'is-lm' : 'is-claude'}`}>
      <div className="pv-head">
        <span className="pv-head-cap">MOTEUR LLM</span>
        <span className="pv-head-link">local</span>
      </div>

      {/* segmented provider toggle */}
      <div className="pv-seg" role="radiogroup" aria-label="LLM provider">
        <button
          type="button" role="radio" aria-checked={!isLM}
          className={`pv-seg-btn ${!isLM ? 'on' : ''}`}
          onClick={() => switchProvider('claude')}
        >
          <span className="pv-seg-glyph">&gt;_</span>
          <span className="pv-seg-name">Claude CLI</span>
        </button>
        <button
          type="button" role="radio" aria-checked={isLM}
          className={`pv-seg-btn ${isLM ? 'on' : ''}`}
          onClick={() => switchProvider('lmstudio')}
        >
          <span className="pv-seg-glyph pv-seg-glyph-grid">▦</span>
          <span className="pv-seg-name">LM Studio</span>
        </button>
      </div>

      {/* active-engine status row — clickable to open the model list under LM Studio */}
      <button
        type="button"
        className={`pv-active ${isLM ? 'pv-active-btn' : ''} ${open ? 'is-open' : ''}`}
        onClick={() => isLM && setOpen((o) => !o)}
        disabled={!isLM}
        aria-expanded={isLM ? open : undefined}
      >
        <span className={`pv-dot ${loading ? 'is-loading' : 'is-ok'}`} />
        <span className="pv-active-main">
          {isLM ? (
            <>
              <span className="pv-active-name">{m.name}</span>
              <span className="pv-active-spec">{m.params} · {m.quant}</span>
            </>
          ) : (
            <>
              <span className="pv-active-name">{CLAUDE_MODEL}</span>
              <span className="pv-active-spec">CLI bridge</span>
            </>
          )}
        </span>
        <span className="pv-active-state">
          {loading ? 'chargement…' : isLM ? 'chargé' : 'connecté'}
        </span>
        {isLM && <span className="pv-chev">{open ? '▴' : '▾'}</span>}
      </button>

      {/* model list — only under LM Studio */}
      {isLM && open && (
        <div className="pv-list" role="listbox" aria-label="LM Studio models">
          <div className="pv-list-head">
            <span>MODÈLE LOCAL</span>
            <span>{LM_MODELS.length} disponibles</span>
          </div>
          {LM_MODELS.map((mm) => {
            const on = mm.id === lmModel;
            return (
              <button
                key={mm.id} type="button" role="option" aria-selected={on}
                className={`pv-row ${on ? 'on' : ''}`}
                onClick={() => pickModel(mm.id)}
              >
                <span className="pv-row-mark">{on ? '◆' : '◇'}</span>
                <span className="pv-row-name">{mm.name}</span>
                <span className="pv-row-spec">{mm.params} · {mm.quant}</span>
                <span className="pv-row-ram">{mm.ram}</span>
              </button>
            );
          })}
        </div>
      )}
    </div>
  );
}

window.ProviderPicker = ProviderPicker;
window.PROVIDER_HELPERS = { activeModelLabel, LM_MODELS, CLAUDE_MODEL, lmModelById };
