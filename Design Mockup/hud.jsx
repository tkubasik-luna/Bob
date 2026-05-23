// hud.jsx — Atmospheric chrome around the sphere. Asymmetric, minimal.
// Style intent: cinematic annotations, not a dashboard.

const { useEffect, useRef, useState: useStateH } = React;

// State display config
const STATE_META = {
  idle:   { label: 'STAND BY',     sub: 'Ambient listening',          tone: 'calm' },
  listen: { label: 'LISTENING',    sub: 'Capturing voice input',      tone: 'active' },
  think:  { label: 'PROCESSING',   sub: 'Reasoning in progress',      tone: 'active' },
  speak:  { label: 'RESPONDING',   sub: 'Synthesising voice output',  tone: 'active' },
  alert:  { label: 'ALERT',        sub: 'Attention requested',        tone: 'warn'   },
  error:  { label: 'FAULT',        sub: 'Recovering — please retry',  tone: 'err'    },
};

const SAMPLE_TRANSCRIPTS = {
  idle:   '',
  listen: '“…hey bob, can you remind me to…”',
  think:  '',
  speak:  '“I’ve noted that down. Anything else you’d like to add?”',
  alert:  '“Battery at 14%. Charge soon.”',
  error:  '“Model temporarily unreachable. Retrying.”',
};

function useNow() {
  const [now, setNow] = useStateH(() => new Date());
  useEffect(() => {
    const id = setInterval(() => setNow(new Date()), 1000);
    return () => clearInterval(id);
  }, []);
  return now;
}

function pad(n) { return String(n).padStart(2, '0'); }

function formatTime(d) {
  return `${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}`;
}

// ───── Top-left: identity & time ─────
function HUDIdentity({ state, theme }) {
  const now = useNow();
  return (
    <div className="hud-identity">
      <div className="hud-id-mark">
        <span className="hud-id-glyph">⌬</span>
        <span className="hud-id-name">BOB</span>
        <span className="hud-id-ver">/0.3a</span>
      </div>
      <div className="hud-id-time">{formatTime(now)}</div>
      <div className="hud-id-sess">SESSION · 04:17:22</div>
    </div>
  );
}

// ───── Top-right: telemetry, sparse ─────
function HUDTelemetry({ state }) {
  const [vals, setVals] = useStateH({ cpu: 18, gpu: 42, mem: 6.3, lat: 84 });
  useEffect(() => {
    const id = setInterval(() => {
      setVals((v) => ({
        cpu: Math.max(4, Math.min(94, v.cpu + (Math.random() - 0.5) * 12 + (state === 'think' ? 4 : -1))),
        gpu: Math.max(4, Math.min(94, v.gpu + (Math.random() - 0.5) * 14 + (state === 'think' ? 6 : -2))),
        mem: Math.max(2, Math.min(15, v.mem + (Math.random() - 0.5) * 0.3)),
        lat: Math.max(30, Math.min(220, v.lat + (Math.random() - 0.5) * 24)),
      }));
    }, 700);
    return () => clearInterval(id);
  }, [state]);

  const row = (label, val, unit, frac) => (
    <div className="hud-tel-row">
      <span className="hud-tel-label">{label}</span>
      <span className="hud-tel-val">{val}<span className="hud-tel-unit">{unit}</span></span>
      <span className="hud-tel-bar">
        <span className="hud-tel-fill" style={{ width: `${Math.max(2, Math.min(100, frac))}%` }} />
      </span>
    </div>
  );

  return (
    <div className="hud-telemetry">
      {row('CPU', vals.cpu.toFixed(0), '%',  vals.cpu)}
      {row('GPU', vals.gpu.toFixed(0), '%',  vals.gpu)}
      {row('MEM', vals.mem.toFixed(1), 'GB', vals.mem * 6.67)}
      {row('LAT', vals.lat.toFixed(0), 'ms', vals.lat / 2.2)}
    </div>
  );
}

// ───── Left edge: vertical state display ─────
function HUDStateRail({ state }) {
  const meta = STATE_META[state] || STATE_META.idle;
  return (
    <div className={`hud-staterail tone-${meta.tone}`} key={state}>
      <div className="hud-staterail-pip" />
      <div className="hud-staterail-label">{meta.label}</div>
      <div className="hud-staterail-sub">{meta.sub}</div>
    </div>
  );
}

// ───── Right edge: tick scale (confidence / audio level) ─────
function HUDTickScale({ state }) {
  const [level, setLevel] = useStateH(0);
  useEffect(() => {
    let raf, t0 = performance.now();
    function tick(now) {
      const t = (now - t0) / 1000;
      let target = 0.15;
      if (state === 'listen') target = 0.55 + 0.35 * Math.abs(Math.sin(t * 4));
      else if (state === 'speak') target = 0.4 + 0.5 * Math.abs(Math.sin(t * 7));
      else if (state === 'think') target = 0.7 + 0.2 * Math.sin(t * 1.4);
      else if (state === 'alert') target = 0.95;
      else if (state === 'error') target = 0.2 + 0.7 * Math.sin(t * 18) ** 2;
      setLevel((l) => l + (target - l) * 0.2);
      raf = requestAnimationFrame(tick);
    }
    raf = requestAnimationFrame(tick);
    return () => cancelAnimationFrame(raf);
  }, [state]);

  const ticks = 28;
  return (
    <div className="hud-tickscale">
      <div className="hud-tick-cap">SIGNAL</div>
      <div className="hud-tick-col">
        {Array.from({ length: ticks }).map((_, i) => {
          const frac = 1 - i / (ticks - 1);
          const on = level >= frac - 0.001;
          return <div key={i} className={`hud-tick ${on ? 'on' : ''}`} />;
        })}
      </div>
      <div className="hud-tick-cap">{(level * 100).toFixed(0).padStart(2, '0')}</div>
    </div>
  );
}

// ───── Bottom: transcript / prompt ─────
function HUDTranscript({ state, surfaceLine }) {
  // when an overlay is showing, the AI's transcript is contextual
  if (surfaceLine) {
    return (
      <div className="hud-transcript has-text" key={'surf_' + surfaceLine}>
        <div className="hud-transcript-text">{surfaceLine}</div>
      </div>
    );
  }
  const text = SAMPLE_TRANSCRIPTS[state];
  return (
    <div className={`hud-transcript ${text ? 'has-text' : ''}`} key={state + '_' + (text ? 'on' : 'off')}>
      {state === 'think' ? (
        <div className="hud-transcript-thinking">
          <span>thinking</span>
          <span className="dot d1">·</span>
          <span className="dot d2">·</span>
          <span className="dot d3">·</span>
        </div>
      ) : text ? (
        <div className="hud-transcript-text">{text}</div>
      ) : (
        <div className="hud-transcript-hint">Say “Hey Bob” to begin</div>
      )}
    </div>
  );
}

// ───── Drifting thought fragments (visible during THINK state) ─────
const THOUGHT_FRAGMENTS = [
  'parse intent', 'recall: project · sphere lab', 'retrieve: 14 entities',
  'tool · calendar', 'tool · notes', 'rerank candidates', 'check: user prefs',
  'memory · cold storage', 'embedding · 1536d', 'route to · gpt-mini',
  'tokenize prompt', 'compose plan', 'verify constraints', 'draft response',
  'check tone', 'cite source', 'pre-warm tts', 'fetch context',
];

function HUDThoughts({ state }) {
  const [items, setItems] = useStateH([]);
  useEffect(() => {
    if (state !== 'think') { setItems([]); return; }
    let i = 0;
    const id = setInterval(() => {
      const t = THOUGHT_FRAGMENTS[Math.floor(Math.random() * THOUGHT_FRAGMENTS.length)];
      const angle = Math.random() * Math.PI * 2;
      const fragment = {
        key: ++i,
        text: t,
        angle,
        born: performance.now(),
      };
      setItems((arr) => [...arr.slice(-12), fragment]);
    }, 240);
    return () => clearInterval(id);
  }, [state]);

  // Cull old
  useEffect(() => {
    if (!items.length) return;
    const id = setInterval(() => {
      setItems((arr) => arr.filter((f) => performance.now() - f.born < 3200));
    }, 400);
    return () => clearInterval(id);
  }, [items.length]);

  return (
    <div className="hud-thoughts">
      {items.map((f) => {
        const age = (performance.now() - f.born) / 3200;
        const dist = 180 + age * 220;
        const x = Math.cos(f.angle) * dist;
        const y = Math.sin(f.angle) * dist * 0.7;
        const op = age < 0.1 ? age * 10 : 1 - (age - 0.1) / 0.9;
        return (
          <div
            key={f.key}
            className="hud-thought"
            style={{
              transform: `translate(${x}px, ${y}px)`,
              opacity: op,
            }}
          >
            <span className="hud-thought-tick">›</span>
            {f.text}
          </div>
        );
      })}
    </div>
  );
}

// ───── Background tasks panel — top-right, under telemetry ─────
const TASK_POOL = [
  'Synthèse · réunion produit',
  'Indexation · 142 e-mails',
  'Recherche · documentation API',
  'Transcription · note vocale',
  'Génération · résumé hebdo',
  'Analyse · pull request #284',
  'Veille · actualité IA',
  'Téléchargement · dataset 3.2 Go',
  'Compression · vidéo brut',
  'Traduction · contrat ES → FR',
  'Vérification · agenda demain',
  'Pré-chargement · contexte projet',
  'Embedding · 1 248 fragments',
  'Export · présentation Q3',
];
let __taskId = 1;
function makeTask(name) {
  return {
    id: __taskId++,
    name: name || TASK_POOL[Math.floor(Math.random() * TASK_POOL.length)],
    status: 'queued',
    progress: 0,
    elapsed: 0,
    duration: 7 + Math.random() * 18,
    fadeAt: null,
  };
}
function initialTasks() {
  return [
    { id: __taskId++, name: 'Synthèse · réunion produit', status: 'running', progress: 0.42, elapsed: 5.9, duration: 14, fadeAt: null },
    { id: __taskId++, name: 'Indexation · 142 e-mails',    status: 'running', progress: 0.12, elapsed: 2.6, duration: 22, fadeAt: null },
    { id: __taskId++, name: 'Génération · résumé hebdo',   status: 'queued',  progress: 0,    elapsed: 0,   duration: 9,  fadeAt: null },
    { id: __taskId++, name: 'Veille · actualité IA',       status: 'done',    progress: 1,    elapsed: 8,   duration: 8,  fadeAt: performance.now() + 3000 },
  ];
}
function advanceTasks(prev, dt, now) {
  // Remove faded tasks
  let tasks = prev.filter(t => !t.fadeAt || now < t.fadeAt);

  // Advance running tasks
  tasks = tasks.map(t => {
    if (t.status !== 'running') return t;
    const elapsed = t.elapsed + dt;
    const progress = Math.min(1, elapsed / t.duration);
    if (progress >= 1) {
      const failed = Math.random() < 0.10;
      return { ...t, progress: 1, elapsed, status: failed ? 'error' : 'done', fadeAt: now + 3200 + Math.random() * 1200 };
    }
    return { ...t, elapsed, progress };
  });

  // Promote queued -> running, keep <= 2 in flight
  const runningCount = tasks.filter(t => t.status === 'running').length;
  if (runningCount < 2) {
    const idx = tasks.findIndex(t => t.status === 'queued');
    if (idx >= 0) tasks = tasks.map((t, i) => i === idx ? { ...t, status: 'running' } : t);
  }

  // Occasionally enqueue a new task
  const activeCount = tasks.filter(t => t.status === 'running' || t.status === 'queued').length;
  if (activeCount < 3 && Math.random() < dt * 0.18) {
    tasks = [...tasks, makeTask()];
  }

  return tasks;
}
function formatTaskSub(t) {
  if (t.status === 'queued')  return 'EN FILE';
  if (t.status === 'running') return `${Math.round(t.progress * 100)} %`;
  if (t.status === 'done')    return 'OK';
  if (t.status === 'error')   return 'ÉCHEC';
  return '';
}

function HUDTasks() {
  const [tasks, setTasks] = useStateH(initialTasks);
  useEffect(() => {
    let last = performance.now();
    const id = setInterval(() => {
      const now = performance.now();
      const dt = Math.min(0.5, (now - last) / 1000);
      last = now;
      setTasks(prev => advanceTasks(prev, dt, now));
    }, 220);
    return () => clearInterval(id);
  }, []);

  const live = tasks.filter(t => t.status === 'running' || t.status === 'queued').length;
  const runningCount = tasks.filter(t => t.status === 'running').length;

  return (
    <div className="hud-tasks">
      <div className="hud-tasks-head">
        <span className="hud-tasks-title">TÂCHES · ARRIÈRE-PLAN</span>
        <span className="hud-tasks-count">
          <span className={runningCount > 0 ? 'is-live' : ''}>{String(live).padStart(2, '0')}</span>
          <span className="hud-tasks-sep">/</span>
          <span>{String(tasks.length).padStart(2, '0')}</span>
        </span>
      </div>
      <div className="hud-tasks-list">
        {tasks.slice(-4).map(t => (
          <div key={t.id} className={`hud-task is-${t.status}`}>
            <span className="hud-task-status" aria-hidden="true" />
            <span className="hud-task-name">{t.name}</span>
            <span className="hud-task-sub">{formatTaskSub(t)}</span>
            <span className="hud-task-prog">
              <span
                className="hud-task-prog-fill"
                style={{ width: `${(t.status === 'running' ? t.progress : t.status === 'done' || t.status === 'error' ? 1 : 0) * 100}%` }}
              />
            </span>
          </div>
        ))}
      </div>
    </div>
  );
}

// ───── Corner brackets — minimal frame indicator ─────
function HUDFrame() {
  return (
    <>
      <div className="hud-frame fr-tl" />
      <div className="hud-frame fr-tr" />
      <div className="hud-frame fr-bl" />
      <div className="hud-frame fr-br" />
    </>
  );
}

// ───── Bottom-right diagnostic strip (state-dependent) ─────
function HUDDiag({ state, variant }) {
  const meta = STATE_META[state];
  return (
    <div className="hud-diag">
      <div className="hud-diag-row"><span>OBSERVER</span><b>shell · {window.VARIANT_NAMES[variant]}</b></div>
      <div className="hud-diag-row"><span>CHANNEL</span><b>local · tauri</b></div>
      <div className="hud-diag-row"><span>MODEL</span><b>haiku-4.5 / o4-mini</b></div>
      <div className="hud-diag-row"><span>STATE</span><b className={`tone-${meta.tone}`}>{state.toUpperCase()}</b></div>
    </div>
  );
}

window.HUD = {
  HUDIdentity, HUDTelemetry, HUDStateRail, HUDTickScale,
  HUDTranscript, HUDThoughts, HUDFrame, HUDDiag, HUDTasks, STATE_META,
};
