// concept-bloom.jsx — PISTE 3 · BLOOM
// Each parallel task grows as a living organism from a seed. The stalk's height
// is its progress; every phase forms a leaf-node as the growth passes it;
// sub-agents bud as side-branches; the answer blooms as a flower at the apex
// (a wilted, drooping tip if the task failed). Pollen rises from the active
// node as raw activity. Metaphor: generative / botanical growth.

const { useRef: useRefBl } = React;

function hexAb(hex, a) {
  const h = hex.replace('#', '');
  const n = parseInt(h.length === 3 ? h.split('').map((c) => c + c).join('') : h, 16);
  return `rgba(${(n >> 16) & 255},${(n >> 8) & 255},${n & 255},${a})`;
}

function BloomConcept() {
  const P = BOB.PALETTE, F = BOB.FONTS;
  const drawRef = useRefBl(null);
  const canvasRef = useStageCanvas(drawRef);

  const bounds = {};
  BOB.TASKS.forEach((t) => {
    bounds[t.id] = t._spans.map((sp) => ({ key: sp.key, f0: (sp.start - t.gap) / (t._work - t.gap), f1: (sp.end - t.gap) / (t._work - t.gap) }));
  });

  function leaf(ctx, x, y, ang, size, color) {
    ctx.save(); ctx.translate(x, y); ctx.rotate(ang);
    ctx.beginPath();
    ctx.moveTo(0, 0);
    ctx.quadraticCurveTo(size * 0.5, -size * 0.4, size, 0);
    ctx.quadraticCurveTo(size * 0.5, size * 0.4, 0, 0);
    ctx.fillStyle = color; ctx.fill();
    ctx.restore();
  }

  drawRef.current = (ctx, w, h, t) => {
    ctx.clearRect(0, 0, w, h);
    const snap = BOB.snapshot(t);
    const baseY = h * 0.78, maxH = baseY - h * 0.16;
    const colW = (w - 120) / snap.length, x0 = 60;

    // soil line
    ctx.beginPath(); ctx.moveTo(40, baseY); ctx.lineTo(w - 40, baseY);
    ctx.strokeStyle = hexAb(P.ink, 0.08); ctx.lineWidth = 1; ctx.stroke();

    snap.forEach((s, i) => {
      const cx = x0 + (i + 0.5) * colW;
      const g = s.progress;
      const queued = s.status === 'queued';
      const done = s.status === 'done';
      const errored = s.status === 'error';
      const tc = s.tint, pc = s.tintPhase;
      const sway = (s01) => Math.sin(s01 * 2.6 + t * 1.1 + i * 1.7) * (s01 * 16 * (errored ? 0.5 : 1));
      const xAt = (s01) => cx + sway(s01);
      const yAt = (s01) => baseY - s01 * g * maxH;

      // seed / core glow at base
      const seedPulse = 1 + Math.sin(t * 2 + i) * 0.2;
      const sg = ctx.createRadialGradient(cx, baseY, 1, cx, baseY, 18 * seedPulse);
      sg.addColorStop(0, hexAb(queued ? tc : pc, 0.5)); sg.addColorStop(1, hexAb(tc, 0));
      ctx.beginPath(); ctx.arc(cx, baseY, 18 * seedPulse, 0, Math.PI * 2); ctx.fillStyle = sg; ctx.fill();
      ctx.beginPath(); ctx.arc(cx, baseY, queued ? 3.5 : 4.5, 0, Math.PI * 2);
      ctx.fillStyle = queued ? hexAb(tc, 0.6) : tc; ctx.fill();

      if (queued) {
        ctx.textAlign = 'center'; ctx.font = `9px ${F.mono}`; ctx.fillStyle = P.inkFaint;
        ctx.fillText('DORMANT', cx, baseY + 26);
        drawLabel(ctx, s, cx, baseY, F, P);
        return;
      }

      // ── stalk (tapering, swaying) ──
      const SEG = 36;
      for (let k = 0; k < SEG; k++) {
        const a = k / SEG, b = (k + 1) / SEG;
        ctx.beginPath();
        ctx.moveTo(xAt(a), yAt(a)); ctx.lineTo(xAt(b), yAt(b));
        ctx.lineWidth = BOB.lerp(3.2, 0.8, a);
        ctx.strokeStyle = hexAb(errored ? P.err : tc, errored ? 0.55 : 0.85);
        ctx.lineCap = 'round'; ctx.stroke();
      }

      // ── phase leaf-nodes at fixed heights, revealed as growth passes ──
      bounds[s.id].forEach((bd, k) => {
        if (g < bd.f0 + 0.01) return;
        const reveal = BOB.clamp01((g - bd.f0) / 0.12);
        const s01 = bd.f1 / 1;                  // node sits at end of its phase band
        const hf = Math.min(s01, g) / g;        // param along current grown stalk
        const nx = xAt(hf), ny = yAt(hf);
        const active = s.phaseKey === bd.key && s.status === 'running';
        const ncol = BOB.PHASES[bd.key].tint;
        const side = k % 2 ? 1 : -1;
        // leaf
        leaf(ctx, nx, ny, side > 0 ? -0.5 : Math.PI + 0.5, (10 + (active ? Math.sin(t * 5) * 2 : 0)) * reveal, hexAb(ncol, 0.55));
        // joint
        ctx.beginPath(); ctx.arc(nx, ny, (active ? 3.4 + Math.sin(t * 6) * 0.8 : 2.6) * reveal, 0, Math.PI * 2);
        ctx.fillStyle = ncol;
        if (active) { ctx.shadowBlur = 12; ctx.shadowColor = ncol; }
        ctx.fill(); ctx.shadowBlur = 0;
      });

      // ── sub-agent branches near the tools node ──
      const toolsB = bounds[s.id].find((b) => b.key === 'tools');
      if (toolsB && g >= toolsB.f0) {
        const hf = Math.min(toolsB.f1, g) / g;
        const ax = xAt(hf), ay = yAt(hf);
        s.subs.forEach((sub, j) => {
          if (sub.status === 'queued') return;
          const side = j % 2 ? 1 : -1;
          const len = 30 + j * 6;
          const droop = sub.status === 'error' ? 1 : -1;   // error droops down
          const ex = ax + side * len * sub.progress;
          const ey = ay + (droop * 18 + j * 4) * sub.progress;
          const mc = sub.status === 'error' ? P.err : sub.status === 'done' ? P.ok : tc;
          ctx.beginPath();
          ctx.moveTo(ax, ay);
          ctx.quadraticCurveTo(ax + side * len * 0.5, ay - (droop < 0 ? 14 : -4), ex, ey);
          ctx.strokeStyle = hexAb(mc, 0.55); ctx.lineWidth = 1.4; ctx.lineCap = 'round'; ctx.stroke();
          // bud / flower / wilt at tip
          if (sub.status === 'done') {
            for (let p = 0; p < 5; p++) {
              const pa = (p / 5) * Math.PI * 2 + t * 0.4;
              leaf(ctx, ex, ey, pa, 5, hexAb(P.ok, 0.7));
            }
            ctx.beginPath(); ctx.arc(ex, ey, 2.2, 0, Math.PI * 2); ctx.fillStyle = P.accent3; ctx.fill();
          } else if (sub.status === 'error') {
            ctx.beginPath(); ctx.arc(ex, ey, 2.6, 0, Math.PI * 2); ctx.fillStyle = P.err; ctx.fill();
          } else {
            const pp = 2 + Math.sin(t * 5 + j) * 0.8;
            ctx.beginPath(); ctx.arc(ex, ey, pp, 0, Math.PI * 2); ctx.fillStyle = mc;
            ctx.shadowBlur = 8; ctx.shadowColor = mc; ctx.fill(); ctx.shadowBlur = 0;
          }
        });
      }

      // ── apex: bloom (writing/done) or wilt (error) ──
      const tx = xAt(1), ty = yAt(1);
      if (errored && g > 0.9) {
        // drooping wilted tip
        ctx.beginPath();
        ctx.moveTo(tx, ty);
        ctx.quadraticCurveTo(tx + 6, ty + 4, tx + 12, ty + 14);
        ctx.strokeStyle = hexAb(P.err, 0.7); ctx.lineWidth = 1.4; ctx.stroke();
        ctx.beginPath(); ctx.arc(tx + 12, ty + 14, 3, 0, Math.PI * 2); ctx.fillStyle = P.err; ctx.fill();
      } else if (s.phaseKey === 'writing' || done) {
        const open = done ? 1 : BOB.easeOut(s.inPhase);
        const petals = 7;
        ctx.save(); ctx.translate(tx, ty);
        for (let p = 0; p < petals; p++) {
          const pa = (p / petals) * Math.PI * 2 + t * 0.25;
          const pl = 13 * open;
          leaf(ctx, 0, 0, pa, pl, hexAb(done ? P.ok : P.accent2, 0.4 + 0.35 * open));
        }
        ctx.beginPath(); ctx.arc(0, 0, 4 * open + 1.5, 0, Math.PI * 2);
        ctx.fillStyle = done ? P.accent3 : P.ok; ctx.shadowBlur = 14; ctx.shadowColor = done ? P.ok : P.accent2;
        ctx.fill(); ctx.shadowBlur = 0; ctx.restore();
      } else {
        // growing tip bud
        ctx.beginPath(); ctx.arc(tx, ty, 3 + Math.sin(t * 4 + i) * 0.6, 0, Math.PI * 2);
        ctx.fillStyle = pc; ctx.shadowBlur = 10; ctx.shadowColor = pc; ctx.fill(); ctx.shadowBlur = 0;
      }

      // ── pollen / spores rising from active node (raw activity) ──
      if (s.status === 'running') {
        const N = 7;
        for (let n = 0; n < N; n++) {
          const seed = BOB.hash01(i * 41 + n * 13);
          const u = (t * 0.35 + seed) % 1;
          const py = ty + 6 - u * 46;
          const px = tx + Math.sin(u * 8 + seed * 6) * 7;
          ctx.beginPath(); ctx.arc(px, py, 1.3, 0, Math.PI * 2);
          ctx.fillStyle = hexAb(pc, (1 - u) * 0.6); ctx.fill();
        }
      }

      drawLabel(ctx, s, cx, baseY, F, P);
    });
  };

  return (
    <ConceptShell
      name="BLOOM"
      tagline="Each task grows like an organism. The stalk's height is its progress; phases sprout leaves, sub-agents bud as branches, the answer blooms at the tip."
      accent={P.accent}
      footNote="branches = sub-agents · flower = answer">
      <canvas ref={canvasRef} style={{ position: 'absolute', inset: 0, width: '100%', height: '100%', display: 'block' }} />
    </ConceptShell>
  );
}

function drawLabel(ctx, s, cx, baseY, F, P) {
  ctx.textAlign = 'center'; ctx.textBaseline = 'alphabetic';
  ctx.font = `500 12px ${F.sans}`;
  ctx.fillStyle = s.status === 'queued' ? P.inkFaint : P.ink;
  ctx.fillText(s.title, cx, baseY + 42);
  ctx.font = `9px ${F.mono}`;
  const col = s.status === 'error' ? P.err : s.status === 'done' ? P.ok : s.status === 'queued' ? P.inkFaint : s.tintPhase;
  ctx.fillStyle = col;
  const sub = s.status === 'queued' ? 'QUEUED'
    : s.status === 'done' ? 'BLOOMED'
    : s.status === 'error' ? 'WILTED · FAILED'
    : `${s.phaseLabel.toUpperCase()} · ${Math.round(s.progress * 100)}%`;
  ctx.fillText(sub, cx, baseY + 56);
  if (s.status === 'running') { ctx.fillStyle = P.inkDim; ctx.fillText(s.act, cx, baseY + 70); }
}

window.BloomConcept = BloomConcept;
