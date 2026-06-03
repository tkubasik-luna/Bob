// concept-orbit.jsx — PISTE 1 · ORBIT
// Bob is a breathing core. Each parallel task is a body orbiting on its own
// ring; the luminous comet-tail trailing the body IS its progress (a full ring
// = done). Sub-agents are moons around each body. A readable ledger on the
// right keeps the numbers legible. Metaphor: spatial / gravitational presence.

const { useRef: useRefO } = React;

function hexA(hex, a) {
  const h = hex.replace('#', '');
  const n = parseInt(h.length === 3 ? h.split('').map((c) => c + c).join('') : h, 16);
  return `rgba(${(n >> 16) & 255},${(n >> 8) & 255},${n & 255},${a})`;
}

function OrbitConcept() {
  const P = BOB.PALETTE, F = BOB.FONTS;
  const drawRef = useRefO(null);
  const pulses = useRefO([]);       // completion shockwaves
  const prevStatus = useRefO({});   // detect transitions → spawn pulse
  const canvasRef = useStageCanvas(drawRef);
  const tickRef = useDomTick(160);

  drawRef.current = (ctx, w, h, t) => {
    ctx.clearRect(0, 0, w, h);
    const snap = BOB.snapshot(t);
    const cx = w * 0.40, cy = h * 0.53;
    const maxR = Math.min(w * 0.40, h * 0.40);
    const ringR = [0.40, 0.60, 0.80, 1.0].map((k) => maxR * k);
    const running = snap.filter((s) => s.status === 'running').length;

    // ── faint full-ring tracks ──
    snap.forEach((s, i) => {
      const R = ringR[i];
      ctx.beginPath(); ctx.arc(cx, cy, R, 0, Math.PI * 2);
      ctx.strokeStyle = hexA(s.tint, 0.10); ctx.lineWidth = 1; ctx.stroke();
    });

    // ── per-task body + progress tail + moons + beam + label ──
    snap.forEach((s, i) => {
      const R = ringR[i];
      const dir = i % 2 ? -1 : 1;
      const speed = 0.085 + i * 0.012;
      const angle = (t * speed * dir) + i * 1.9;
      const bx = cx + Math.cos(angle) * R;
      const by = cy + Math.sin(angle) * R;
      const pc = s.tintPhase;       // phase color
      const tc = s.tint;            // task identity color
      const live = s.status === 'running';
      const errored = s.status === 'error';

      // detect completion → spawn a core shockwave once per cycle
      const prev = prevStatus.current[s.id];
      if (prev === 'running' && (s.status === 'done' || s.status === 'error')) {
        pulses.current.push({ born: t, r0: R, err: errored });
      }
      prevStatus.current[s.id] = s.status;

      // attention beam core → body
      if (s.status !== 'queued') {
        const g = ctx.createLinearGradient(cx, cy, bx, by);
        g.addColorStop(0, hexA(tc, 0));
        g.addColorStop(1, hexA(pc, live ? 0.32 : 0.12));
        ctx.beginPath(); ctx.moveTo(cx, cy); ctx.lineTo(bx, by);
        ctx.strokeStyle = g; ctx.lineWidth = 1; ctx.stroke();
      }

      // progress comet-tail (trails the body, length = progress)
      const tail = s.progress * Math.PI * 1.98;
      if (tail > 0.001) {
        const steps = Math.max(8, Math.floor(tail * 22));
        ctx.lineWidth = errored ? 2 : 2.4;
        for (let k = 0; k < steps; k++) {
          const a0 = angle - tail * (k / steps);
          const a1 = angle - tail * ((k + 1) / steps);
          const al = (1 - k / steps) * (live ? 0.9 : 0.6);
          ctx.beginPath(); ctx.arc(cx, cy, R, a1, a0);
          ctx.strokeStyle = hexA(errored ? P.err : tc, al); ctx.stroke();
        }
      }

      // sub-agent moons orbiting the body
      s.subs.forEach((sub, j) => {
        if (sub.status === 'queued') return;
        const mr = 15 + j * 6;
        const ma = t * (0.9 + j * 0.3) + j * 2.1;
        const mx = bx + Math.cos(ma) * mr, my = by + Math.sin(ma) * mr;
        const mc = sub.status === 'error' ? P.err : sub.status === 'done' ? P.ok : tc;
        const pr = sub.status === 'running' ? 2.2 + Math.sin(t * 5 + j) * 0.6 : 2;
        // moon trail
        ctx.beginPath(); ctx.moveTo(bx, by); ctx.lineTo(mx, my);
        ctx.strokeStyle = hexA(mc, 0.18); ctx.lineWidth = 0.75; ctx.stroke();
        ctx.beginPath(); ctx.arc(mx, my, pr, 0, Math.PI * 2);
        ctx.fillStyle = mc; ctx.fill();
      });

      // body
      const pulse = live ? 1 + Math.sin(t * 4 + i) * 0.12 : 1;
      const br = (s.status === 'queued' ? 3.5 : 6.5) * pulse;
      const jit = errored ? (Math.random() - 0.5) * 1.4 : 0;
      ctx.save();
      ctx.shadowBlur = live ? 16 : 6; ctx.shadowColor = pc;
      ctx.beginPath(); ctx.arc(bx + jit, by, br, 0, Math.PI * 2);
      ctx.fillStyle = s.status === 'queued' ? hexA(tc, 0.4) : pc; ctx.fill();
      ctx.restore();
      // identity halo ring
      ctx.beginPath(); ctx.arc(bx, by, br + 4, 0, Math.PI * 2);
      ctx.strokeStyle = hexA(tc, live ? 0.6 : 0.25); ctx.lineWidth = 1; ctx.stroke();

      // label outside the body (always outward — keeps clear of the core caption)
      const lo = 1;
      const lx = cx + Math.cos(angle) * (R + 22 * lo);
      const ly = cy + Math.sin(angle) * (R + 22 * lo);
      ctx.textAlign = Math.cos(angle) * lo >= 0 ? 'left' : 'right';
      ctx.textBaseline = 'middle';
      ctx.font = `500 12px ${F.sans}`;
      ctx.fillStyle = hexA(P.ink, s.status === 'queued' ? 0.4 : 0.92);
      ctx.fillText(s.title, lx, ly - 7);
      ctx.font = `9px ${F.mono}`;
      ctx.fillStyle = hexA(pc, 0.95);
      const pct = s.status === 'done' ? 'COMPLETE' : s.status === 'error' ? 'FAILED' : s.status === 'queued' ? 'QUEUED' : `${s.phaseLabel.toUpperCase()} · ${Math.round(s.progress * 100)}%`;
      ctx.fillText(pct, lx, ly + 8);
    });

    // ── shockwaves ──
    pulses.current = pulses.current.filter((p) => t - p.born < 1.6);
    pulses.current.forEach((p) => {
      const k = (t - p.born) / 1.6;
      const r = BOB.lerp(8, maxR * 1.18, BOB.easeOut(k));
      ctx.beginPath(); ctx.arc(cx, cy, r, 0, Math.PI * 2);
      ctx.strokeStyle = hexA(p.err ? P.err : P.ok, (1 - k) * 0.5);
      ctx.lineWidth = 1.5; ctx.stroke();
    });

    // ── breathing core ──
    const breathe = 1 + Math.sin(t * 1.6) * 0.05;
    const thinking = snap.some((s) => s.phaseKey === 'thinking');
    const cr = 30 * breathe * (thinking ? 1.06 : 1);
    const cg = ctx.createRadialGradient(cx, cy, 2, cx, cy, cr * 2.6);
    cg.addColorStop(0, hexA(P.accent, 0.55));
    cg.addColorStop(0.4, hexA(P.accent, 0.16));
    cg.addColorStop(1, hexA(P.accent, 0));
    ctx.beginPath(); ctx.arc(cx, cy, cr * 2.6, 0, Math.PI * 2); ctx.fillStyle = cg; ctx.fill();
    // rotating inner arcs
    for (let a = 0; a < 3; a++) {
      const off = t * (0.5 + a * 0.3) * (a % 2 ? -1 : 1);
      ctx.beginPath(); ctx.arc(cx, cy, cr - a * 6, off, off + 1.6);
      ctx.strokeStyle = hexA(P.accent3, 0.5 - a * 0.13); ctx.lineWidth = 1.4; ctx.stroke();
    }
    ctx.beginPath(); ctx.arc(cx, cy, cr * 0.42, 0, Math.PI * 2);
    ctx.fillStyle = hexA(P.accent3, 0.9); ctx.fill();
    // working count
    ctx.textAlign = 'center'; ctx.textBaseline = 'alphabetic';
    ctx.font = `300 26px ${F.sans}`; ctx.fillStyle = P.ink;
    ctx.fillText(String(running), cx, cy + cr * 2.6 + 28);
    ctx.font = `9px ${F.mono}`; ctx.fillStyle = P.inkFaint;
    ctx.fillText('TASKS IN FLIGHT', cx, cy + cr * 2.6 + 42);
  };

  // right-side legible ledger (DOM, low frequency)
  const snap = BOB.snapshot();
  return (
    <ConceptShell
      name="ORBIT"
      tagline="Tasks gravitate around Bob. The tail trailing each body is its progress — a full ring means done."
      accent={P.accent}
      footNote="◍ moons = sub-agents">
      <canvas ref={canvasRef} style={{ position: 'absolute', inset: 0, width: '100%', height: '100%', display: 'block' }} />
      <div ref={tickRef} style={{ position: 'absolute', top: '50%', right: 40, transform: 'translateY(-50%)', width: 215, zIndex: 5, display: 'flex', flexDirection: 'column', gap: 10 }}>
        <div style={{ fontFamily: F.mono, fontSize: 9, letterSpacing: '0.28em', color: P.inkFaint, paddingBottom: 8, borderBottom: `1px solid ${P.inkGhost}` }}>MANIFEST</div>
        {snap.map((s) => {
          const dim = s.status === 'queued';
          const col = s.status === 'error' ? P.err : s.status === 'done' ? P.ok : s.tintPhase;
          return (
            <div key={s.id} style={{ opacity: dim ? 0.5 : 1 }}>
              <div style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
                <span style={{ width: 7, height: 7, borderRadius: 2, background: s.tint, transform: 'rotate(45deg)', flex: '0 0 auto' }} />
                <span style={{ fontFamily: F.sans, fontSize: 12, fontWeight: 500, color: P.ink, whiteSpace: 'nowrap', overflow: 'hidden', textOverflow: 'ellipsis' }}>{s.title}</span>
                <span style={{ marginLeft: 'auto', fontFamily: F.mono, fontSize: 10, color: col, fontVariantNumeric: 'tabular-nums' }}>
                  {s.status === 'done' ? '✓' : s.status === 'error' ? '✗' : s.status === 'queued' ? '··' : Math.round(s.progress * 100) + '%'}
                </span>
              </div>
              <div style={{ fontFamily: F.mono, fontSize: 9, letterSpacing: '0.1em', color: dim ? P.inkFaint : col, marginLeft: 15, marginTop: 3, whiteSpace: 'nowrap', overflow: 'hidden', textOverflow: 'ellipsis' }}>
                {s.status === 'running' ? s.act : s.status === 'queued' ? 'in queue' : s.phaseLabel.toLowerCase()}
              </div>
              <div style={{ height: 2, background: P.inkGhost, marginTop: 5, marginLeft: 15, overflow: 'hidden' }}>
                <div style={{ height: '100%', width: (s.progress * 100) + '%', background: col, transition: 'width .3s linear' }} />
              </div>
            </div>
          );
        })}
      </div>
    </ConceptShell>
  );
}

window.OrbitConcept = OrbitConcept;
