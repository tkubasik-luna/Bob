// p3d-core.jsx — the CORE (the AI's consciousness), one per piste.
//   pearl     — liquid nacre sphere that breathes (CSS volume + iridescent sheen)
//   particles — luminous dust organising on a sphere (canvas)
//   aurora    — drifting silk veil of light (CSS blurred volumes)
//   rings     — concentric wire orb in real 3D (CSS preserve-3d)
// All soft, feminine, low-chroma. Driven by the shared task phase so the core
// reacts: calmer at rest, more alive while thinking / answering.

const { useRef: useRefCore, useEffect: useEffectCore } = React;

// ── PEARL — liquid nacre sphere ──────────────────────────────────────────
function CorePearl({ energy }) {
  return (
    <div className="core core-pearl" style={{ '--energy': energy }}>
      <div className="pearl-halo" />
      <div className="pearl-body">
        <div className="pearl-sheen" />
        <div className="pearl-blob b1" />
        <div className="pearl-blob b2" />
        <div className="pearl-light" />
      </div>
    </div>
  );
}

// ── PARTICLES — luminous dust on a slowly turning sphere ─────────────────
function CoreParticles({ energy, accent, accent2 }) {
  const cv = useRefCore(null);
  const en = useRefCore(energy);
  en.current = energy;
  useEffectCore(() => {
    const canvas = cv.current;
    const ctx = canvas.getContext('2d');
    const dpr = Math.min(window.devicePixelRatio || 1, 2);
    let raf, t0 = performance.now();
    const N = 170;
    const phi = Math.PI * (3 - Math.sqrt(5));
    const pts = Array.from({ length: N }, (_, i) => {
      const y = 1 - (i / (N - 1)) * 2;
      const r = Math.sqrt(1 - y * y);
      return { y, r, a: phi * i, jitter: Math.random() * Math.PI * 2 };
    });
    const hex = (h) => [parseInt(h.slice(1, 3), 16), parseInt(h.slice(3, 5), 16), parseInt(h.slice(5, 7), 16)];
    const c1 = hex(accent), c2 = hex(accent2);
    const resize = () => {
      const w = canvas.clientWidth, h = canvas.clientHeight;
      canvas.width = w * dpr; canvas.height = h * dpr;
    };
    resize();
    const ro = new ResizeObserver(resize); ro.observe(canvas);
    const loop = (now) => {
      const t = (now - t0) / 1000;
      const e = en.current;
      const W = canvas.width, H = canvas.height;
      ctx.clearRect(0, 0, W, H);
      const cx = W / 2, cy = H / 2;
      const R = Math.min(W, H) * (0.30 + 0.02 * Math.sin(t * 0.8));
      const rotY = t * (0.12 + e * 0.18);
      const rotX = Math.sin(t * 0.18) * 0.35;
      const cosY = Math.cos(rotY), sinY = Math.sin(rotY);
      const cosX = Math.cos(rotX), sinX = Math.sin(rotX);
      for (let i = 0; i < N; i++) {
        const p = pts[i];
        const breathe = 1 + 0.05 * Math.sin(t * 1.3 + p.jitter) * (0.4 + e);
        let x = Math.cos(p.a) * p.r, z = Math.sin(p.a) * p.r, y = p.y;
        // rotate Y
        let x1 = x * cosY - z * sinY, z1 = x * sinY + z * cosY;
        // rotate X
        let y1 = y * cosX - z1 * sinX, z2 = y * sinX + z1 * cosX;
        const depth = (z2 + 1) / 2;
        const px = cx + x1 * R * breathe;
        const py = cy + y1 * R * breathe;
        const size = (0.6 + depth * 2.0) * dpr;
        const alpha = 0.10 + depth * 0.6;
        const mix = depth;
        const r = Math.round(c1[0] + (c2[0] - c1[0]) * mix);
        const g = Math.round(c1[1] + (c2[1] - c1[1]) * mix);
        const b = Math.round(c1[2] + (c2[2] - c1[2]) * mix);
        const hot = ((i + Math.floor(t * (0.8 + e))) % 23) === 0;
        ctx.beginPath();
        ctx.arc(px, py, hot ? size * 1.8 : size, 0, Math.PI * 2);
        if (hot) {
          ctx.fillStyle = `rgba(255,255,255,${alpha})`;
          ctx.shadowColor = `rgb(${r},${g},${b})`; ctx.shadowBlur = 10 * dpr;
        } else {
          ctx.fillStyle = `rgba(${r},${g},${b},${alpha})`;
          ctx.shadowBlur = 0;
        }
        ctx.fill();
      }
      ctx.shadowBlur = 0;
      raf = requestAnimationFrame(loop);
    };
    raf = requestAnimationFrame(loop);
    return () => { cancelAnimationFrame(raf); ro.disconnect(); };
  }, [accent, accent2]);
  return (
    <div className="core core-particles">
      <div className="particles-halo" />
      <canvas ref={cv} className="particles-canvas" />
    </div>
  );
}

// ── AURORA — drifting silk veil ──────────────────────────────────────────
function CoreAurora({ energy }) {
  return (
    <div className="core core-aurora" style={{ '--energy': energy }}>
      <div className="aurora-veil v1" />
      <div className="aurora-veil v2" />
      <div className="aurora-veil v3" />
      <div className="aurora-veil v4" />
      <div className="aurora-glow" />
    </div>
  );
}

// ── RINGS — concentric wire orb in real 3D ───────────────────────────────
function CoreRings({ energy }) {
  return (
    <div className="core core-rings" style={{ '--energy': energy }}>
      <div className="rings-space">
        <div className="ring r1" />
        <div className="ring r2" />
        <div className="ring r3" />
        <div className="ring r4" />
        <div className="ring r5" />
        <div className="rings-nucleus" />
      </div>
      <div className="rings-halo" />
    </div>
  );
}

// ── NEBULA — the finalized WebGL consciousness orb, tinted to this screen ──
// Reuses <Conscience> (form 3). Task phase drives the mood; palette + glass
// tint are matched to the Nacre screen (rose / lavender) instead of Bob's warm.
const NEB_PRESETS = {
  idle:   { trailCount: 4,  trailSpeed: 0.22, trailLen: 1.2, trailWidth: 0.020, trailAlt: 1.06, equator: 0.12, trailGlow: 0.50, coreGlow: 0.70, fogAmt: 0.85, ior: 1.24, rim: 0.65, sphereSize: 1.00 },
  listen: { trailCount: 6,  trailSpeed: 0.38, trailLen: 1.5, trailWidth: 0.020, trailAlt: 1.40, equator: 0.72, trailGlow: 0.80, coreGlow: 1.70, fogAmt: 0.95, ior: 1.25, rim: 0.85, sphereSize: 1.00 },
  think:  { trailCount: 22, trailSpeed: 1.50, trailLen: 2.3, trailWidth: 0.017, trailAlt: 1.02, equator: 0.18, trailGlow: 1.00, coreGlow: 1.10, fogAmt: 1.10, ior: 1.30, rim: 0.80, sphereSize: 0.92 },
  speak:  { trailCount: 10, trailSpeed: 0.85, trailLen: 1.8, trailWidth: 0.022, trailAlt: 1.12, equator: 0.28, trailGlow: 1.00, coreGlow: 1.90, fogAmt: 1.50, ior: 1.30, rim: 1.70, sphereSize: 1.00 },
  alert:  { trailCount: 11, trailSpeed: 1.90, trailLen: 1.5, trailWidth: 0.020, trailAlt: 1.12, equator: 0.45, trailGlow: 1.05, coreGlow: 1.30, fogAmt: 1.00, ior: 1.28, rim: 1.10, sphereSize: 1.00 },
  error:  { trailCount: 12, trailSpeed: 2.40, trailLen: 1.35, trailWidth: 0.021, trailAlt: 1.10, equator: 0.38, trailGlow: 1.10, coreGlow: 1.25, fogAmt: 1.05, ior: 1.28, rim: 1.20, sphereSize: 1.00 },
};

// Palettes kept in the screen's rose/lavender family, shifting subtly per mood.
const NEB_PALETTES = {
  idle:   { accent: '#E7B4CB', accent2: '#C6A2DB', accent3: '#F1E3EC', bg: '#160F18' },
  listen: { accent: '#D9A8D6', accent2: '#BBA6E0', accent3: '#F1E6F2', bg: '#160F18' },
  think:  { accent: '#C6A2DB', accent2: '#A98FD8', accent3: '#ECE0F4', bg: '#18101C' },
  speak:  { accent: '#ECB0C8', accent2: '#D7A8D8', accent3: '#F6E6EE', bg: '#170F18' },
  alert:  { accent: '#E59AC0', accent2: '#D08FC8', accent3: '#F2DCE8', bg: '#170E18' },
  error:  { accent: '#D77A9E', accent2: '#C77FB0', accent3: '#EFCEDD', bg: '#180C16' },
};
const NEB_TINT = [0.95, 0.92, 0.98];   // cool, faintly lavender glass

const NEB_HEX = (h) => { h = h.replace('#',''); return [parseInt(h.slice(0,2),16)/255, parseInt(h.slice(2,4),16)/255, parseInt(h.slice(4,6),16)/255]; };
const NEB_PALETTES_RGB = Object.fromEntries(Object.entries(NEB_PALETTES).map(([k,v]) => [k, {
  accent: NEB_HEX(v.accent), accent2: NEB_HEX(v.accent2), accent3: NEB_HEX(v.accent3), bg: NEB_HEX(v.bg),
}]));

const NEB_PHASE_TO_STATE = { think: 'think', delegate: 'listen', answer: 'speak', done: 'idle' };

function CoreNebula({ phaseKey }) {
  const state = NEB_PHASE_TO_STATE[phaseKey] || 'idle';
  const neb = NEB_PRESETS[state] || NEB_PRESETS.idle;
  return (
    <div className="core core-nebula">
      <div className="nebula-halo" />
      {React.createElement(window.Conscience, {
        form: 3, state, neb,
        palettes: NEB_PALETTES_RGB, tint: NEB_TINT,
        motion: 0.6, glow: 0.7, breathDepth: 1.0, gazeGain: 1.0,
      })}
    </div>
  );
}

function Core({ variant, energy, accent, accent2, phaseKey }) {
  if (variant === 'nebula') return <CoreNebula phaseKey={phaseKey} />;
  if (variant === 'pearl') return <CorePearl energy={energy} />;
  if (variant === 'particles') return <CoreParticles energy={energy} accent={accent} accent2={accent2} />;
  if (variant === 'aurora') return <CoreAurora energy={energy} />;
  if (variant === 'rings') return <CoreRings energy={energy} />;
  return null;
}

window.Core = Core;
