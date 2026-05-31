// app.jsx — Main composition. Sphere + HUD + Tweaks.

const { useState: useStateA, useEffect: useEffectA } = React;

const TWEAK_DEFAULTS = /*EDITMODE-BEGIN*/{
  "state": "idle",
  "motion": 0.55,
  "glow": 0.7,
  "autoCycle": false,
  "surface": "none",
  "provider": "claude",
  "lmModel": "qwen2.5-coder-32b"
}/*EDITMODE-END*/;

// Locked aesthetic — warm palette + calm mood + liquid shell.
const THEME = 'warm';
const MOOD  = 'calm';
const VARIANT = 0; // liquid

function App() {
  const [t, setTweak] = useTweaks(TWEAK_DEFAULTS);

  // Live agent console drives the sphere by default. Clicking a state pill (or
  // pressing a number key) takes manual control until re-enabled.
  const [liveState, setLiveState] = useStateA('idle');
  const [liveSync, setLiveSync]   = useStateA(true);
  const eff = liveSync ? liveState : t.state;

  // Auto-cycle states if enabled (manual mode only)
  useEffectA(() => {
    if (!t.autoCycle || liveSync) return;
    const order = ['idle', 'listen', 'think', 'speak', 'alert', 'error'];
    let i = order.indexOf(t.state);
    const id = setInterval(() => {
      i = (i + 1) % order.length;
      setTweak('state', order[i]);
    }, 4500);
    return () => clearInterval(id);
  }, [t.autoCycle, liveSync]);

  // Keyboard shortcuts: 1-6 = state, 7-9/a/s/d = surface, 0 = none
  useEffectA(() => {
    const onKey = (e) => {
      if (e.target.tagName === 'INPUT' || e.target.tagName === 'TEXTAREA') return;
      const stateMap = { '1':'idle','2':'listen','3':'think','4':'speak','5':'alert','6':'error' };
      const surfMap  = { '0':'none','7':'email','8':'image','9':'video','a':'map','s':'doc','d':'contact','f':'notes' };
      if (stateMap[e.key]) { setLiveSync(false); setTweak('state', stateMap[e.key]); }
      if (surfMap[e.key]) setTweak('surface', surfMap[e.key]);
    };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, [t.state]);

  const { HUDIdentity, HUDStateRail, HUDTranscript, HUDThoughts, HUDFrame } = window.HUD;
  const AgentConsole = window.AgentConsole;
  const ProviderPicker = window.ProviderPicker;
  const { activeModelLabel, LM_MODELS } = window.PROVIDER_HELPERS;
  const { OverlayCard, SurfacePicker, SURFACE_META, SURFACE_ORDER } = window.Overlay;

  const modelLabel = activeModelLabel(t.provider, t.lmModel);

  const surfaceActive = t.surface && t.surface !== 'none';

  return (
    <div className={`app theme-${THEME} mood-${MOOD} state-${eff} surface-${t.surface || 'none'} ${surfaceActive ? 'has-surface' : ''}`}>
      <Sphere
        variant={VARIANT}
        state={eff}
        motion={t.motion}
        glow={t.glow}
        theme={THEME}
        mood={MOOD}
      />

      <HUDFrame />

      <div className="hud-zone tl">
        <HUDIdentity state={eff} theme={THEME} />
        <ProviderPicker
          provider={t.provider}
          lmModel={t.lmModel}
          onProvider={(p) => setTweak('provider', p)}
          onModel={(m) => setTweak('lmModel', m)}
        />
      </div>
      <div className="hud-zone l"><HUDStateRail state={eff} /></div>
      <div className="hud-zone b">
        <HUDTranscript state={eff} surfaceLine={surfaceActive ? SURFACE_META[t.surface].line : null} />
      </div>

      {/* Live agent console — Jarvis orchestrator + spawned sub-agents */}
      <AgentConsole modelLabel={modelLabel} onSphereState={(s) => { if (liveSync) setLiveState(s); }} />

      {/* Thought stream — overlays sphere region */}
      <div className="hud-thoughts-stage"><HUDThoughts state={eff} /></div>

      {/* Data overlay — surfaces email/image/video/map/doc/contact */}
      {surfaceActive && <OverlayCard surface={t.surface} onClose={() => setTweak('surface', 'none')} />}

      {/* Surface picker — above state-pills */}
      <SurfacePicker surface={t.surface || 'none'} onChange={(s) => setTweak('surface', s)} />

      {/* Quick state pills — click takes manual control of the sphere */}
      <div className={`state-pills ${liveSync ? 'is-live' : ''}`}>
        {['idle','listen','think','speak','alert','error'].map((s, i) => (
          <button
            key={s}
            className={`pill ${eff === s ? 'on' : ''} tone-${window.HUD.STATE_META[s].tone}`}
            onClick={() => { setLiveSync(false); setTweak('state', s); }}
          >
            <span className="pill-key">{i+1}</span>
            <span className="pill-name">{s}</span>
          </button>
        ))}
      </div>

      <TweaksPanel>
        <TweakSection label="AI State" />
        <TweakToggle label="Sphere follows Jarvis" value={liveSync}
          onChange={(v) => setLiveSync(v)} />
        <TweakSelect label="State (manual)" value={eff}
          options={['idle','listen','think','speak','alert','error']}
          onChange={(v) => { setLiveSync(false); setTweak('state', v); }} />
        <TweakToggle label="Auto-cycle" value={t.autoCycle}
          onChange={(v) => setTweak('autoCycle', v)} />

        <TweakSection label="Sphere" />
        <TweakSlider label="Motion" value={t.motion} min={0} max={1} step={0.01}
          onChange={(v) => setTweak('motion', v)} />
        <TweakSlider label="Glow" value={t.glow} min={0} max={1} step={0.01}
          onChange={(v) => setTweak('glow', v)} />

        <TweakSection label="LLM provider" />
        <TweakRadio label="Engine" value={t.provider}
          options={[{ value: 'claude', label: 'Claude CLI' }, { value: 'lmstudio', label: 'LM Studio' }]}
          onChange={(v) => setTweak('provider', v)} />
        {t.provider === 'lmstudio' && (
          <TweakSelect label="Local model" value={t.lmModel}
            options={LM_MODELS.map((m) => ({ value: m.id, label: `${m.name} · ${m.params}` }))}
            onChange={(v) => setTweak('lmModel', v)} />
        )}

        <TweakSection label="Surface" />
        <TweakSelect label="Show data" value={t.surface || 'none'}
          options={['none', ...SURFACE_ORDER]}
          onChange={(v) => setTweak('surface', v)} />

        <div className="twk-hint">
          <div><b>Keys:</b> 1–6 state</div>
          <div><b>Surface:</b> 0 none · 7 mail · 8 image · 9 video · A map · S doc · D contact · F notes</div>
        </div>
      </TweaksPanel>
    </div>
  );
}

const root = ReactDOM.createRoot(document.getElementById('root'));
root.render(<App />);
