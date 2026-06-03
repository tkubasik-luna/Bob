// conscience-lab.jsx — the interactive lab around <Conscience>.
// Pick a form, cycle its states live, watch the vitals, or let it live on its own.

const { useState: useStateL, useEffect: useEffectL, useRef: useRefL, useCallback } = React;

const TWEAK_DEFAULTS = /*EDITMODE-BEGIN*/{
  "breathDepth": 1.0,
  "gazeGain": 1.0,
  "sphereScale": 1.0,
  "glowScale": 1.0
}/*EDITMODE-END*/;

// Each state is a full preset of the orb's body — count, speed, length, width,
// altitude, equatorial convergence, glow, core, brume, refraction, rim, size.
// The render loop eases between presets, so switching states reads as one
// organic consciousness settling into a new posture.
const STATE_PRESETS = {
  // calm, barely moving, dim
  idle:   { trailCount: 4,  trailSpeed: 0.22, trailLen: 1.2, trailWidth: 0.020, trailAlt: 1.06, equator: 0.12, trailGlow: 0.50, coreGlow: 0.70, fogAmt: 0.85, ior: 1.24, rim: 0.65, sphereSize: 1.00 },
  // listening: orbits draw together toward the equator, the diffuse core lifts,
  // satellites ride higher
  listen: { trailCount: 6,  trailSpeed: 0.38, trailLen: 1.5, trailWidth: 0.020, trailAlt: 1.40, equator: 0.72, trailGlow: 0.80, coreGlow: 1.70, fogAmt: 0.95, ior: 1.25, rim: 0.85, sphereSize: 1.00 },
  // thinking: a dense swarm, fast, folded in tight against itself
  think:  { trailCount: 22, trailSpeed: 1.50, trailLen: 2.3, trailWidth: 0.017, trailAlt: 1.02, equator: 0.18, trailGlow: 1.00, coreGlow: 1.10, fogAmt: 1.10, ior: 1.30, rim: 0.80, sphereSize: 0.92 },
  // speaking: billowing brume, bright edge halo, an active pulsing core
  speak:  { trailCount: 10, trailSpeed: 0.85, trailLen: 1.8, trailWidth: 0.022, trailAlt: 1.12, equator: 0.28, trailGlow: 1.00, coreGlow: 1.90, fogAmt: 1.50, ior: 1.30, rim: 1.70, sphereSize: 1.00 },
  // alert: tense but restrained
  alert:  { trailCount: 11, trailSpeed: 1.90, trailLen: 1.5, trailWidth: 0.020, trailAlt: 1.12, equator: 0.45, trailGlow: 1.05, coreGlow: 1.30, fogAmt: 1.00, ior: 1.28, rim: 1.10, sphereSize: 1.00 },
  // error: quick, unsettled, but not overwhelming (shader adds the red tint)
  error:  { trailCount: 12, trailSpeed: 2.40, trailLen: 1.35, trailWidth: 0.021, trailAlt: 1.10, equator: 0.38, trailGlow: 1.10, coreGlow: 1.25, fogAmt: 1.05, ior: 1.28, rim: 1.20, sphereSize: 1.00 },
};

function nebForState(state, t) {
  const p = STATE_PRESETS[state] || STATE_PRESETS.idle;
  return {
    trailCount: p.trailCount,
    trailSpeed: p.trailSpeed,
    trailLen:   p.trailLen,
    trailWidth: p.trailWidth,
    trailAlt:   p.trailAlt,
    equator:    p.equator,
    trailGlow:  p.trailGlow * t.glowScale,
    coreGlow:   p.coreGlow  * t.glowScale,
    fogAmt:     p.fogAmt,
    ior:        p.ior,
    rim:        p.rim,
    sphereSize: p.sphereSize * t.sphereScale,
  };
}

const STATE_META = {
  idle:   { fr: 'Au repos',  tone: 'calm' },
  listen: { fr: 'À l’écoute', tone: 'calm' },
  think:  { fr: 'Réflexion', tone: 'calm' },
  speak:  { fr: 'Parole',    tone: 'calm' },
  alert:  { fr: 'Alerte',    tone: 'warn' },
  error:  { fr: 'Erreur',    tone: 'err'  },
};
const STATE_ORDER = ['idle', 'listen', 'think', 'speak', 'alert', 'error'];

// A small rolling trace of the breath signal — the "vital sign".
function BreathTrace({ breath, attention }) {
  const ref = useRefL(null);
  const bufRef = useRefL([]);
  useEffectL(() => {
    const buf = bufRef.current;
    buf.push(breath);
    if (buf.length > 90) buf.shift();
    const cv = ref.current; if (!cv) return;
    const dpr = Math.min(window.devicePixelRatio || 1, 2);
    const w = cv.clientWidth, h = cv.clientHeight;
    if (cv.width !== w*dpr) { cv.width = w*dpr; cv.height = h*dpr; }
    const ctx = cv.getContext('2d');
    ctx.setTransform(dpr,0,0,dpr,0,0);
    ctx.clearRect(0,0,w,h);
    // baseline
    ctx.strokeStyle = 'rgba(255,231,221,0.10)';
    ctx.lineWidth = 1;
    ctx.beginPath(); ctx.moveTo(0,h*0.5); ctx.lineTo(w,h*0.5); ctx.stroke();
    // trace
    const n = buf.length;
    ctx.beginPath();
    for (let i = 0; i < n; i++) {
      const x = (i/(89)) * w;
      const y = h - 4 - buf[i] * (h - 8);
      i === 0 ? ctx.moveTo(x,y) : ctx.lineTo(x,y);
    }
    const a = 0.45 + attention*0.5;
    ctx.strokeStyle = `rgba(255,150,90,${a})`;
    ctx.lineWidth = 1.6;
    ctx.shadowColor = 'rgba(255,140,80,0.7)';
    ctx.shadowBlur = 6;
    ctx.stroke();
    // leading dot
    if (n > 0) {
      const x = ((n-1)/89)*w, y = h-4-buf[n-1]*(h-8);
      ctx.shadowBlur = 8;
      ctx.fillStyle = 'rgba(255,180,120,0.95)';
      ctx.beginPath(); ctx.arc(x,y,2.2,0,Math.PI*2); ctx.fill();
    }
  }, [breath, attention]);
  return <canvas ref={ref} className="vt-trace" />;
}

// A little radar showing where it is currently looking.
function GazeDot({ gaze, attention }) {
  const x = 50 + gaze[0] * 36;
  const y = 50 + gaze[1] * 36;
  return (
    <div className="vt-gaze">
      <div className="vt-gaze-ring" />
      <div className="vt-gaze-cross-h" />
      <div className="vt-gaze-cross-v" />
      <div className="vt-gaze-pupil" style={{ left: `${x}%`, top: `${y}%`,
        transform: `translate(-50%,-50%) scale(${0.7 + attention*0.8})` }} />
    </div>
  );
}

function Lab() {
  const [t, setTweak] = useTweaks(TWEAK_DEFAULTS);
  const form = 3;                 // only the Nébuleuse form remains
  const [state, setState] = useStateL('idle');
  const [vivant, setVivant] = useStateL(false);
  const [vitals, setVitals] = useStateL({ breath: 0, attention: 0, gaze: [0,0], bpm: 0, weights: {} });

  const onVitals = useCallback((v) => setVitals(v), []);

  // keyboard: 1-6 states
  useEffectL(() => {
    const onKey = (e) => {
      if (e.target.tagName === 'INPUT' || e.target.tagName === 'TEXTAREA') return;
      const sm = { '1':'idle','2':'listen','3':'think','4':'speak','5':'alert','6':'error' };
      if (sm[e.key]) { setVivant(false); setState(sm[e.key]); }
    };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, []);

  // autonomous "Vivant" mode — it lives a little life of its own on irregular timing
  useEffectL(() => {
    if (!vivant) return;
    let alive = true;
    const weighted = ['idle','idle','idle','listen','listen','think','think','speak','idle','listen'];
    const tick = () => {
      if (!alive) return;
      const next = weighted[Math.floor(Math.random()*weighted.length)];
      setState(next);
      const dwell = next === 'speak' ? 2200 + Math.random()*2500
                  : next === 'think' ? 3000 + Math.random()*3000
                  : 2500 + Math.random()*4500;
      timer = setTimeout(tick, dwell);
    };
    let timer = setTimeout(tick, 600);
    return () => { alive = false; clearTimeout(timer); };
  }, [vivant]);

  const pick = (s) => { setVivant(false); setState(s); };
  const att = vitals.attention || 0;
  const breathPct = Math.round((vitals.breath || 0) * 100);

  return (
    <div className={`cv-app state-${state} tone-${STATE_META[state].tone} ${vivant ? 'is-vivant' : ''}`}>
      <Conscience
        form={form}
        state={state}
        motion={0.6}
        glow={0.7}
        breathDepth={t.breathDepth}
        gazeGain={t.gazeGain}
        neb={nebForState(state, t)}
        onVitals={onVitals}
      />

      {/* corner frame */}
      <div className="cv-fr tl" /><div className="cv-fr tr" />
      <div className="cv-fr bl" /><div className="cv-fr br" />

      {/* identity */}
      <header className="cv-id">
        <div className="cv-id-mark">
          <span className="cv-id-glyph">◞</span>
          <span className="cv-id-name">BOB</span>
          <span className="cv-id-sub">CONSCIENCE</span>
        </div>
        <div className="cv-id-line">laboratoire du vivant · v0.3</div>
      </header>

      {/* vitals */}
      <aside className="cv-vitals">
        <div className="cv-vitals-head">
          <span>SIGNES VITAUX</span>
          <span className={`cv-vitals-pip ${vivant ? 'live' : ''}`} />
        </div>
        <div className="cv-vt-row">
          <span className="cv-vt-label">RESPIRATION</span>
          <span className="cv-vt-val">{breathPct}<i>%</i></span>
        </div>
        <BreathTrace breath={vitals.breath || 0} attention={att} />
        <div className="cv-vt-row">
          <span className="cv-vt-label">RYTHME</span>
          <span className="cv-vt-val">{vitals.bpm || 0}<i>/min</i></span>
        </div>
        <div className="cv-vt-bar"><span style={{ width: `${Math.round(att*100)}%` }} /></div>
        <div className="cv-vt-row tight">
          <span className="cv-vt-label">ATTENTION</span>
          <span className="cv-vt-val">{Math.round(att*100)}<i>%</i></span>
        </div>
        <div className="cv-vt-gazewrap">
          <span className="cv-vt-label">REGARD</span>
          <GazeDot gaze={vitals.gaze || [0,0]} attention={att} />
        </div>
      </aside>

      {/* state console */}
      <div className="cv-states">
        <button className={`cv-vivant ${vivant ? 'on' : ''}`} onClick={() => setVivant(v => !v)}>
          <span className="cv-vivant-dot" />
          {vivant ? 'VIVANT · autonome' : 'Mode vivant'}
        </button>
        <div className="cv-pills">
          {STATE_ORDER.map((s, i) => (
            <button key={s} className={`cv-pill ${state === s && !vivant ? 'on' : ''} tone-${STATE_META[s].tone}`}
              onClick={() => pick(s)}>
              <span className="cv-pill-key">{i+1}</span>
              <span className="cv-pill-name">{STATE_META[s].fr}</span>
            </button>
          ))}
        </div>
      </div>

      <TweaksPanel>
        <TweakSection label="Apparence" />
        <TweakSlider label="Taille" value={t.sphereScale} min={0.7} max={1.4} step={0.02}
          onChange={(v) => setTweak('sphereScale', v)} />
        <TweakSlider label="Intensité" value={t.glowScale} min={0.3} max={2.2} step={0.05}
          onChange={(v) => setTweak('glowScale', v)} />

        <TweakSection label="Vie" />
        <TweakSlider label="Profondeur du souffle" value={t.breathDepth} min={0.2} max={1.6} step={0.05}
          onChange={(v) => setTweak('breathDepth', v)} />
        <TweakSlider label="Sensibilité du regard" value={t.gazeGain} min={0} max={1.4} step={0.05}
          onChange={(v) => setTweak('gazeGain', v)} />
        <div className="cv-twk-hint">
          <div><b>Touches</b> · 1–6 état</div>
          <div>Chaque état a sa propre posture — les transitions sont fondues.</div>
        </div>
      </TweaksPanel>
    </div>
  );
}

const cvRoot = ReactDOM.createRoot(document.getElementById('root'));
cvRoot.render(<Lab />);
