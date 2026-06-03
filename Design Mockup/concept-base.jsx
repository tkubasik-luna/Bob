// concept-base.jsx — shared scaffolding for the four progress explorations.
// Crisp DPR canvas + off-screen pause (IntersectionObserver) + the common dark
// "Bob at work" shell (corner brackets, concept title, shared phase legend).
// Exports atoms to window so each concept file stays small.

const { useRef: useRefB, useEffect: useEffectB, useState: useStateB, useLayoutEffect: useLayoutEffectB } = React;

// ── canvas stage: device-pixel-sharp, paused when scrolled off-screen ────
// drawRef.current(ctx, w, h, t, dt) is called each animation frame. `t` is the
// SHARED global clock (BOB.now()) so every concept is in lockstep.
function useStageCanvas(drawRef) {
  const canvasRef = useRefB(null);
  useEffectB(() => {
    const cv = canvasRef.current;
    if (!cv) return;
    const ctx = cv.getContext('2d');
    let raf = 0, running = false, last = 0;

    const size = () => {
      const r = cv.getBoundingClientRect();
      const dpr = Math.min(2, window.devicePixelRatio || 1);
      const w = Math.max(1, Math.round(r.width));
      const h = Math.max(1, Math.round(r.height));
      if (cv.width !== w * dpr || cv.height !== h * dpr) {
        cv.width = w * dpr; cv.height = h * dpr;
      }
      cv._cssW = w; cv._cssH = h; cv._dpr = dpr;
    };

    const frame = () => {
      if (!running) return;
      const t = BOB.now();
      const dt = last ? Math.min(0.05, t - last) : 0.016; last = t;
      const dpr = cv._dpr || 1;
      ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
      const fn = drawRef.current;
      if (fn) fn(ctx, cv._cssW || 1, cv._cssH || 1, t, dt);
      raf = requestAnimationFrame(frame);
    };
    const start = () => { if (!running) { running = true; last = 0; raf = requestAnimationFrame(frame); } };
    const stop = () => { running = false; cancelAnimationFrame(raf); };

    size();
    const ro = new ResizeObserver(size);
    ro.observe(cv);
    // Start optimistically — IntersectionObserver is unreliable for elements
    // living inside the canvas's transformed/will-change world, so we run by
    // default and only let the observer PAUSE us when clearly off-screen.
    start();
    const io = new IntersectionObserver(
      (es) => { const vis = es.some((e) => e.isIntersecting); vis ? start() : stop(); },
      { threshold: 0 }
    );
    io.observe(cv);

    return () => { stop(); ro.disconnect(); io.disconnect(); };
  }, []);
  return canvasRef;
}

// ── low-frequency DOM tick (for non-moving chrome that reads the snapshot) ─
function useDomTick(ms = 150) {
  const [, force] = useStateB(0);
  const ref = useRefB(null);
  useEffectB(() => {
    let running = true, id = 0;
    const node = ref.current;
    const loop = () => { if (running) force((x) => (x + 1) & 0xffff); };
    const startTimer = () => { if (!id) id = setInterval(loop, ms); };
    const stopTimer = () => { clearInterval(id); id = 0; };
    let io;
    if (node) {
      io = new IntersectionObserver((es) => {
        es.forEach((e) => (e.isIntersecting ? startTimer() : stopTimer()));
      }, { threshold: 0.01 });
      io.observe(node);
    } else { startTimer(); }
    return () => { running = false; stopTimer(); io && io.disconnect(); };
  }, [ms]);
  return ref;
}

// ── shared dark shell: corner brackets, concept title, optional legend ───
function ConceptShell({ name, tagline, accent, children, legend = true, footNote }) {
  const P = BOB.PALETTE, F = BOB.FONTS;
  const ac = accent || P.accent;
  return (
    <div style={{
      position: 'absolute', inset: 0, overflow: 'hidden',
      background:
        `radial-gradient(120% 90% at 50% -10%, ${P.bg3} 0%, ${P.bg2} 38%, ${P.bg} 78%)`,
      color: P.ink, fontFamily: F.sans,
    }}>
      {/* corner brackets */}
      {[['tl', { top: 18, left: 18, borderTop: '1px solid', borderLeft: '1px solid' }],
        ['tr', { top: 18, right: 18, borderTop: '1px solid', borderRight: '1px solid' }],
        ['bl', { bottom: 18, left: 18, borderBottom: '1px solid', borderLeft: '1px solid' }],
        ['br', { bottom: 18, right: 18, borderBottom: '1px solid', borderRight: '1px solid' }]]
        .map(([k, s]) => (
          <div key={k} style={{ position: 'absolute', width: 26, height: 26, borderColor: `${ac}44`, pointerEvents: 'none', zIndex: 6, ...s }} />
        ))}

      {/* concept identity (top-left) */}
      <div style={{ position: 'absolute', top: 30, left: 36, zIndex: 6, pointerEvents: 'none' }}>
        <div style={{ display: 'flex', alignItems: 'baseline', gap: 10 }}>
          <span style={{ width: 7, height: 7, borderRadius: '50%', background: ac, boxShadow: `0 0 12px ${ac}`, display: 'inline-block' }} />
          <span style={{ fontFamily: F.mono, fontSize: 11, letterSpacing: '0.34em', color: P.inkDim }}>BOB</span>
          <span style={{ fontFamily: F.mono, fontSize: 11, letterSpacing: '0.34em', color: ac }}>{name}</span>
        </div>
        {tagline && <div style={{ fontFamily: F.mono, fontSize: 10, letterSpacing: '0.12em', color: P.inkFaint, marginTop: 6, maxWidth: 360, lineHeight: 1.5 }}>{tagline}</div>}
      </div>

      {/* live badge (top-right) */}
      <div style={{ position: 'absolute', top: 30, right: 36, zIndex: 6, pointerEvents: 'none', display: 'flex', flexDirection: 'column', alignItems: 'flex-end', gap: 8 }}>
        <div style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
          <span style={{ fontFamily: F.mono, fontSize: 10, letterSpacing: '0.3em', color: P.inkFaint }}>LIVE</span>
          <span className="bob-livedot" style={{ width: 6, height: 6, borderRadius: '50%', background: P.ok }} />
        </div>
        {footNote && (
          <div style={{ fontFamily: F.mono, fontSize: 9.5, letterSpacing: '0.12em', color: P.inkFaint, maxWidth: 210, textAlign: 'right', lineHeight: 1.6 }}>{footNote}</div>
        )}
      </div>

      {children}

      {legend && <PhaseLegend />}
    </div>
  );
}

// shared phase color key (bottom-center)
function PhaseLegend() {
  const P = BOB.PALETTE, F = BOB.FONTS;
  const keys = ['reading', 'thinking', 'tools', 'writing', 'done'];
  return (
    <div style={{
      position: 'absolute', bottom: 28, left: '50%', transform: 'translateX(-50%)', zIndex: 6,
      display: 'flex', gap: 18, alignItems: 'center', pointerEvents: 'none',
      padding: '7px 16px', borderRadius: 3,
      background: 'rgba(10,6,6,0.5)', backdropFilter: 'blur(8px)',
      border: `1px solid ${P.inkGhost}`,
    }}>
      {keys.map((k) => (
        <div key={k} style={{ display: 'flex', alignItems: 'center', gap: 7 }}>
          <span style={{ width: 8, height: 8, borderRadius: 2, background: BOB.PHASES[k].tint, transform: 'rotate(45deg)' }} />
          <span style={{ fontFamily: F.mono, fontSize: 9, letterSpacing: '0.16em', color: P.inkDim, textTransform: 'uppercase' }}>{BOB.PHASES[k].label}</span>
        </div>
      ))}
    </div>
  );
}

// one-time keyframes for shared micro-motion
if (!document.getElementById('bob-base-css')) {
  const s = document.createElement('style');
  s.id = 'bob-base-css';
  s.textContent = `
    @keyframes bob-live { 0%,100%{opacity:1;transform:scale(1)} 50%{opacity:.35;transform:scale(.7)} }
    .bob-livedot{ animation: bob-live 2.2s ease-in-out infinite; box-shadow:0 0 8px currentColor; }
    @keyframes bob-fade-up { from{opacity:0;transform:translateY(6px)} to{opacity:1;transform:translateY(0)} }
  `;
  document.head.appendChild(s);
}

Object.assign(window, { useStageCanvas, useDomTick, ConceptShell, PhaseLegend });
