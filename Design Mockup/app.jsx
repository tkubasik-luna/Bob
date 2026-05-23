// app.jsx — Main composition. Sphere + HUD + Tweaks.

const { useState: useStateA, useEffect: useEffectA } = React;

const TWEAK_DEFAULTS = /*EDITMODE-BEGIN*/{
  "state": "idle",
  "motion": 0.55,
  "glow": 0.7,
  "autoCycle": false,
  "surface": "none"
}/*EDITMODE-END*/;

// Locked aesthetic — warm palette + calm mood + liquid shell.
const THEME = 'warm';
const MOOD  = 'calm';
const VARIANT = 0; // liquid

function App() {
  const [t, setTweak] = useTweaks(TWEAK_DEFAULTS);

  // Auto-cycle states if enabled
  useEffectA(() => {
    if (!t.autoCycle) return;
    const order = ['idle', 'listen', 'think', 'speak', 'alert', 'error'];
    let i = order.indexOf(t.state);
    const id = setInterval(() => {
      i = (i + 1) % order.length;
      setTweak('state', order[i]);
    }, 4500);
    return () => clearInterval(id);
  }, [t.autoCycle]);

  // Keyboard shortcuts: 1-6 = state, 7-9/a/s/d = surface, 0 = none
  useEffectA(() => {
    const onKey = (e) => {
      if (e.target.tagName === 'INPUT' || e.target.tagName === 'TEXTAREA') return;
      const stateMap = { '1':'idle','2':'listen','3':'think','4':'speak','5':'alert','6':'error' };
      const surfMap  = { '0':'none','7':'email','8':'image','9':'video','a':'map','s':'doc','d':'contact','f':'notes' };
      if (stateMap[e.key]) setTweak('state', stateMap[e.key]);
      if (surfMap[e.key]) setTweak('surface', surfMap[e.key]);
    };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, [t.state]);

  const { HUDIdentity, HUDTelemetry, HUDStateRail, HUDTickScale,
          HUDTranscript, HUDThoughts, HUDFrame, HUDDiag, HUDTasks } = window.HUD;
  const { OverlayCard, SurfacePicker, SURFACE_META, SURFACE_ORDER } = window.Overlay;

  const surfaceActive = t.surface && t.surface !== 'none';

  return (
    <div className={`app theme-${THEME} mood-${MOOD} state-${t.state} surface-${t.surface || 'none'} ${surfaceActive ? 'has-surface' : ''}`}>
      <Sphere
        variant={VARIANT}
        state={t.state}
        motion={t.motion}
        glow={t.glow}
        theme={THEME}
        mood={MOOD}
      />

      <HUDFrame />

      <div className="hud-zone tl"><HUDIdentity state={t.state} theme={THEME} /></div>
      <div className="hud-zone tr">
        <HUDTelemetry state={t.state} />
        <HUDTasks />
      </div>
      <div className="hud-zone l"><HUDStateRail state={t.state} /></div>
      <div className="hud-zone r"><HUDTickScale state={t.state} /></div>
      <div className="hud-zone b">
        <HUDTranscript state={t.state} surfaceLine={surfaceActive ? SURFACE_META[t.surface].line : null} />
      </div>
      <div className="hud-zone br"><HUDDiag state={t.state} variant={VARIANT} /></div>

      {/* Thought stream — overlays sphere region */}
      <div className="hud-thoughts-stage"><HUDThoughts state={t.state} /></div>

      {/* Data overlay — surfaces email/image/video/map/doc/contact */}
      {surfaceActive && <OverlayCard surface={t.surface} onClose={() => setTweak('surface', 'none')} />}

      {/* Surface picker — above state-pills */}
      <SurfacePicker surface={t.surface || 'none'} onChange={(s) => setTweak('surface', s)} />

      {/* Quick state pills (always visible) */}
      <div className="state-pills">
        {['idle','listen','think','speak','alert','error'].map((s, i) => (
          <button
            key={s}
            className={`pill ${t.state === s ? 'on' : ''} tone-${window.HUD.STATE_META[s].tone}`}
            onClick={() => setTweak('state', s)}
          >
            <span className="pill-key">{i+1}</span>
            <span className="pill-name">{s}</span>
          </button>
        ))}
      </div>

      <TweaksPanel>
        <TweakSection label="AI State" />
        <TweakSelect label="State" value={t.state}
          options={['idle','listen','think','speak','alert','error']}
          onChange={(v) => setTweak('state', v)} />
        <TweakToggle label="Auto-cycle" value={t.autoCycle}
          onChange={(v) => setTweak('autoCycle', v)} />

        <TweakSection label="Sphere" />
        <TweakSlider label="Motion" value={t.motion} min={0} max={1} step={0.01}
          onChange={(v) => setTweak('motion', v)} />
        <TweakSlider label="Glow" value={t.glow} min={0} max={1} step={0.01}
          onChange={(v) => setTweak('glow', v)} />

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
