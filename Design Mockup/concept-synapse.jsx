// concept-synapse.jsx — PISTE 4 · SYNAPSE (v2 — dendrites vivantes + canaux de réflexion)
// Bob is the central soma. Each parallel task grows as a LIVING DENDRITE that
// meanders outward and breathes — never a rigid spoke. Nodes are the phases.
// The segment currently charging is a visible REFLECTION CHANNEL: a luminous
// conduit where the thought-front advances and raw activity tokens stream along
// it, with a clear caption spelling out what Bob is doing right now.

const { useRef: useRefSy } = React;

function hexAy(hex, a) {
  const h = hex.replace('#', '');
  const n = parseInt(h.length === 3 ? h.split('').map((c) => c + c).join('') : h, 16);
  return `rgba(${(n >> 16) & 255},${(n >> 8) & 255},${n & 255},${a})`;
}

function SynapseConcept() {
  const P = BOB.PALETTE, F = BOB.FONTS;
  const drawRef = useRefSy(null);
  const canvasRef = useStageCanvas(drawRef);
  const fires = useRefSy([]);          // node-firing rings
  const prevLit = useRefSy({});        // detect newly-lit nodes → fire

  // node spec per task: root + one per phase (lit when progress passes f) + terminal
  const specs = {};
  BOB.TASKS.forEach((t) => {
    const arr = [{ key: 'root', f: 0 }];
    t._spans.forEach((sp) => arr.push({ key: sp.key, f: (sp.end - t.gap) / (t._work - t.gap) }));
    arr.push({ key: 'terminal', f: 1, term: true });
    specs[t.id] = arr;
  });
  // base direction + meander character per lobe (kept apart so they don't tangle)
  const LOBE = [
    { base: -2.55, curl:  0.55 },   // up-left
    { base:  2.55, curl: -0.55 },   // down-left
    { base: -0.60, curl: -0.55 },   // up-right
    { base:  0.60, curl:  0.55 },   // down-right
  ];

  // quadratic bezier sample
  const q = (a, c, b, u) => ({
    x: (1 - u) * (1 - u) * a.x + 2 * (1 - u) * u * c.x + u * u * b.x,
    y: (1 - u) * (1 - u) * a.y + 2 * (1 - u) * u * c.y + u * u * b.y,
  });
  function roundRect(ctx, x, y, w, h, r) {
    ctx.beginPath();
    ctx.moveTo(x + r, y); ctx.arcTo(x + w, y, x + w, y + h, r);
    ctx.arcTo(x + w, y + h, x, y + h, r); ctx.arcTo(x, y + h, x, y, r);
    ctx.arcTo(x, y, x + w, y, r); ctx.closePath();
  }

  drawRef.current = (ctx, w, h, t) => {
    ctx.clearRect(0, 0, w, h);
    const snap = BOB.snapshot(t);
    const cx = w * 0.5, cy = h * 0.47;
    const R0 = 64, Rspan = Math.min(w * 0.33, h * 0.355), ySq = 0.94;
    const running = snap.filter((s) => s.status === 'running').length;

    // ── build living node positions for a task (curved, breathing spine) ──
    const posFor = (s, i) => {
      const spec = specs[s.id], N = spec.length;
      const L = LOBE[i];
      return spec.map((nd, k) => {
        const nr = N === 1 ? 0 : k / (N - 1);
        // S-bend along the spine + slow organic drift (breathing)
        const bend = L.curl * Math.sin(nr * 1.9);
        const drift = Math.sin(t * 0.5 + i * 1.7 + nr * 3.4) * 0.05
                    + Math.cos(t * 0.33 + k * 0.9) * 0.03;
        const ang = L.base + bend + drift;
        const R = R0 + nr * Rspan + Math.sin(t * 0.7 + k + i) * 3;
        return { x: cx + Math.cos(ang) * R, y: cy + Math.sin(ang) * R * ySq, nd, k, ang, nr };
      });
    };
    // control point between two nodes — bowed perpendicular for an organic vessel
    const ctrl = (a, b, i, k, t) => {
      const mx = (a.x + b.x) / 2, my = (a.y + b.y) / 2;
      const dx = b.x - a.x, dy = b.y - a.y, len = Math.hypot(dx, dy) || 1;
      const nx = -dy / len, ny = dx / len;
      const bow = (BOB.hash01(i * 9 + k) - 0.5) * 26 + Math.sin(t * 0.8 + k + i) * 6;
      return { x: mx + nx * bow, y: my + ny * bow };
    };

    snap.forEach((s, i) => {
      const nodes = posFor(s, i);
      const tc = s.tint;
      const errored = s.status === 'error';
      const queued = s.status === 'queued';

      // which nodes are lit
      const litArr = nodes.map((nn) => !queued && (nn.nd.term ? (s.status === 'done' || s.status === 'error') : s.progress >= nn.nd.f - 0.001));
      const lastLit = litArr.lastIndexOf(true);

      // fire a ring when a node newly lights
      const pk = s.id;
      if (!prevLit.current[pk]) prevLit.current[pk] = -1;
      if (lastLit > prevLit.current[pk] && lastLit >= 0) {
        const nn = nodes[lastLit];
        fires.current.push({ x: nn.x, y: nn.y, born: t, col: nn.nd.term ? (errored ? P.err : P.ok) : (BOB.PHASES[nn.nd.key]?.tint || tc) });
        prevLit.current[pk] = lastLit;
      }
      if (queued) prevLit.current[pk] = -1;

      // ── draw vessels (segments) ──
      for (let k = 0; k < nodes.length - 1; k++) {
        const a = nodes[k], b = nodes[k + 1];
        const c = ctrl(a, b, i, k, t);
        const aLit = litArr[k], bLit = litArr[k + 1];
        const active = aLit && !bLit && s.status === 'running';

        if (aLit && bLit) {
          // settled vessel — soft tapered glow
          ctx.beginPath(); ctx.moveTo(a.x, a.y); ctx.quadraticCurveTo(c.x, c.y, b.x, b.y);
          ctx.strokeStyle = hexAy(errored ? P.err : tc, 0.5); ctx.lineWidth = 2.4; ctx.lineCap = 'round';
          ctx.shadowBlur = 6; ctx.shadowColor = hexAy(errored ? P.err : tc, 0.5); ctx.stroke(); ctx.shadowBlur = 0;
          // gentle resting flow
          for (let m = 0; m < 2; m++) {
            const u = (t * 0.4 + m * 0.5 + BOB.hash01(i + k)) % 1;
            const pt = q(a, c, b, u);
            ctx.beginPath(); ctx.arc(pt.x, pt.y, 1.3, 0, Math.PI * 2);
            ctx.fillStyle = hexAy(P.accent3, 0.45); ctx.fill();
          }
        } else if (active) {
          // ── REFLECTION CHANNEL ── front advances along this segment
          const prevF = nodes[k].nd.f || 0;
          const nextF = nodes[k + 1].nd.f || 1;
          const frac = BOB.clamp01((s.progress - prevF) / Math.max(0.0001, nextF - prevF));
          const pc = errored ? P.err : s.tintPhase;

          // dim future part of the channel
          ctx.beginPath(); ctx.moveTo(a.x, a.y); ctx.quadraticCurveTo(c.x, c.y, b.x, b.y);
          ctx.setLineDash([2, 6]); ctx.strokeStyle = hexAy(tc, 0.22); ctx.lineWidth = 1.2; ctx.stroke(); ctx.setLineDash([]);

          // wide translucent conduit (the visible channel), up to the front
          const STEP = 24;
          ctx.lineCap = 'round';
          for (let m = 0; m < STEP; m++) {
            const u0 = (m / STEP) * frac, u1 = ((m + 1) / STEP) * frac;
            const p0 = q(a, c, b, u0), p1 = q(a, c, b, u1);
            ctx.beginPath(); ctx.moveTo(p0.x, p0.y); ctx.lineTo(p1.x, p1.y);
            ctx.strokeStyle = hexAy(pc, 0.10 + 0.16 * (m / STEP));
            ctx.lineWidth = 7 - 2 * (m / STEP); ctx.stroke();
          }
          // bright core line
          for (let m = 0; m < STEP; m++) {
            const u0 = (m / STEP) * frac, u1 = ((m + 1) / STEP) * frac;
            const p0 = q(a, c, b, u0), p1 = q(a, c, b, u1);
            ctx.beginPath(); ctx.moveTo(p0.x, p0.y); ctx.lineTo(p1.x, p1.y);
            ctx.strokeStyle = hexAy(pc, 0.35 + 0.5 * (m / STEP)); ctx.lineWidth = 1.6; ctx.stroke();
          }

          // raw activity tokens streaming inside the channel
          const intensity = s.phaseKey === 'thinking' ? 1.5 : s.phaseKey === 'tools' ? 1.25 : 1;
          ctx.font = `8.5px ${F.mono}`; ctx.textAlign = 'center'; ctx.textBaseline = 'middle';
          const NT = 5;
          for (let n = 0; n < NT; n++) {
            const seed = BOB.hash01(i * 23 + n * 7);
            const u = ((t * (0.16 + 0.06 * intensity) + seed) % 1) * frac;
            const pt = q(a, c, b, u);
            const fade = Math.sin((u / Math.max(0.001, frac)) * Math.PI);
            const tok = BOB.STREAM_TOKENS[Math.floor(BOB.hash01(i * 5 + n * 3 + Math.floor(t * 0.7 + n)) * BOB.STREAM_TOKENS.length)];
            ctx.fillStyle = hexAy(P.accent2, fade * 0.7);
            ctx.fillText(tok, pt.x, pt.y - 7);
          }

          // travelling front spark
          const fp = q(a, c, b, frac);
          const fr = 3 + Math.sin(t * 7 + i) * 1.1;
          ctx.beginPath(); ctx.arc(fp.x, fp.y, fr, 0, Math.PI * 2);
          ctx.fillStyle = pc; ctx.shadowBlur = 16; ctx.shadowColor = pc; ctx.fill(); ctx.shadowBlur = 0;

          // ── clear reflection caption: phase + current action ──
          const goRight = Math.cos(nodes[lastLit >= 0 ? lastLit : 0].ang) >= 0;
          drawReflection(ctx, fp.x, fp.y, s, pc, goRight, w, F, P, roundRect);
        } else {
          // unlit — faint dashed potential
          ctx.beginPath(); ctx.moveTo(a.x, a.y); ctx.quadraticCurveTo(c.x, c.y, b.x, b.y);
          ctx.setLineDash([2, 6]); ctx.strokeStyle = hexAy(tc, 0.14); ctx.lineWidth = 1; ctx.stroke(); ctx.setLineDash([]);
        }
      }

      // ── sub-agent dendrite offshoots near the tools node ──
      const ti = nodes.findIndex((nn) => nn.nd.key === 'tools');
      const toolsNode = ti >= 0 ? nodes[ti] : null;
      if (toolsNode && litArr[ti]) {
        s.subs.forEach((sub, j) => {
          if (sub.status === 'queued') return;
          const sa = toolsNode.ang + (j - (s.subs.length - 1) / 2) * 0.42 + Math.sin(t * 0.6 + j) * 0.03;
          const sR = (30 + j * 5) * (0.4 + 0.6 * sub.progress);
          const sx = toolsNode.x + Math.cos(sa) * sR;
          const sy = toolsNode.y + Math.sin(sa) * sR * ySq;
          const mc = sub.status === 'error' ? P.err : sub.status === 'done' ? P.ok : tc;
          // bowed twig
          const mx = (toolsNode.x + sx) / 2 + Math.sin(t + j) * 4;
          const my = (toolsNode.y + sy) / 2 - 6;
          ctx.beginPath(); ctx.moveTo(toolsNode.x, toolsNode.y); ctx.quadraticCurveTo(mx, my, sx, sy);
          ctx.strokeStyle = hexAy(mc, 0.42); ctx.lineWidth = 1.1; ctx.lineCap = 'round'; ctx.stroke();
          const r = sub.status === 'running' ? 2.4 + Math.sin(t * 6 + j) * 0.8 : 3;
          ctx.beginPath(); ctx.arc(sx, sy, r, 0, Math.PI * 2);
          ctx.fillStyle = mc;
          if (sub.status === 'running') { ctx.shadowBlur = 9; ctx.shadowColor = mc; }
          ctx.fill(); ctx.shadowBlur = 0;
          // tiny sub label for running only (keeps the field uncluttered)
          if (sub.status === 'running') {
            ctx.font = `7.5px ${F.mono}`; ctx.textAlign = 'center'; ctx.textBaseline = 'middle';
            ctx.fillStyle = hexAy(mc, 0.7);
            ctx.fillText(sub.name, sx, sy + (sy > toolsNode.y ? 11 : -11));
          }
        });
      }

      // ── nodes (organic somata) ──
      nodes.forEach((nn, k) => {
        const lit = litArr[k];
        const phaseCol = nn.nd.term ? (errored ? P.err : P.ok) : (BOB.PHASES[nn.nd.key] ? BOB.PHASES[nn.nd.key].tint : tc);
        const col = nn.nd.key === 'root' ? tc : phaseCol;
        const activeNode = lit && k === lastLit && s.status === 'running';
        const baseR = nn.nd.key === 'root' ? 5 : nn.nd.term ? 5.5 : 4;
        if (lit) {
          const pr = activeNode ? baseR * (1 + Math.sin(t * 5 + i) * 0.18) : baseR;
          ctx.beginPath(); ctx.arc(nn.x, nn.y, pr, 0, Math.PI * 2);
          ctx.fillStyle = col; ctx.shadowBlur = activeNode ? 16 : 7; ctx.shadowColor = col; ctx.fill(); ctx.shadowBlur = 0;
          ctx.beginPath(); ctx.arc(nn.x, nn.y, pr + 3, 0, Math.PI * 2);
          ctx.strokeStyle = hexAy(col, activeNode ? 0.45 : 0.25); ctx.lineWidth = 1; ctx.stroke();
        } else {
          ctx.beginPath(); ctx.arc(nn.x, nn.y, baseR, 0, Math.PI * 2);
          ctx.strokeStyle = hexAy(tc, 0.28); ctx.lineWidth = 1; ctx.setLineDash([2, 3]); ctx.stroke(); ctx.setLineDash([]);
        }
      });

      // ── terminal label (title + status) ──
      const outer = nodes[nodes.length - 1];
      let right = Math.cos(outer.ang) >= 0;
      if (!right && outer.x < 230) right = true;
      if (right && outer.x > w - 230) right = false;
      ctx.textAlign = right ? 'left' : 'right';
      ctx.textBaseline = 'middle';
      const lx = outer.x + (right ? 14 : -14);
      ctx.font = `500 12px ${F.sans}`;
      ctx.fillStyle = hexAy(P.ink, queued ? 0.42 : 0.92);
      ctx.fillText(s.title, lx, outer.y - 7);
      ctx.font = `9px ${F.mono}`;
      ctx.fillStyle = errored ? P.err : s.status === 'done' ? P.ok : queued ? P.inkFaint : s.tintPhase;
      const subL = queued ? 'QUEUED'
        : s.status === 'done' ? '✓ COMPLETE'
        : s.status === 'error' ? '✗ FAILED'
        : `${Math.round(s.progress * 100)}%`;
      ctx.fillText(subL, lx, outer.y + 8);
    });

    // ── firing rings ──
    fires.current = fires.current.filter((f) => t - f.born < 0.9);
    fires.current.forEach((f) => {
      const k = (t - f.born) / 0.9;
      ctx.beginPath(); ctx.arc(f.x, f.y, BOB.lerp(3, 24, BOB.easeOut(k)), 0, Math.PI * 2);
      ctx.strokeStyle = hexAy(f.col, (1 - k) * 0.6); ctx.lineWidth = 1.4; ctx.stroke();
    });

    // ── central soma (Bob) ──
    const breathe = 1 + Math.sin(t * 1.6) * 0.06;
    const thinking = snap.some((s) => s.phaseKey === 'thinking');
    const hr = 19 * breathe * (running ? 1.05 : 1) * (thinking ? 1.04 : 1);
    const hg = ctx.createRadialGradient(cx, cy, 1, cx, cy, hr * 3.2);
    hg.addColorStop(0, hexAy(P.accent, 0.55)); hg.addColorStop(0.5, hexAy(P.accent, 0.15)); hg.addColorStop(1, hexAy(P.accent, 0));
    ctx.beginPath(); ctx.arc(cx, cy, hr * 3.2, 0, Math.PI * 2); ctx.fillStyle = hg; ctx.fill();
    for (let a = 0; a < 3; a++) {
      const off = t * (0.5 + a * 0.35) * (a % 2 ? -1 : 1);
      ctx.beginPath(); ctx.arc(cx, cy, hr - a * 5, off, off + 1.7);
      ctx.strokeStyle = hexAy(P.accent3, 0.5 - a * 0.13); ctx.lineWidth = 1.3; ctx.stroke();
    }
    ctx.beginPath(); ctx.arc(cx, cy, hr * 0.4, 0, Math.PI * 2); ctx.fillStyle = P.accent3; ctx.fill();
    ctx.textAlign = 'center'; ctx.textBaseline = 'middle';
    ctx.font = `300 16px ${F.sans}`; ctx.fillStyle = P.bg;
    ctx.fillText(String(running), cx, cy + 0.5);
    ctx.font = `8px ${F.mono}`; ctx.fillStyle = P.inkFaint;
    ctx.fillText('EN COURS', cx, cy + hr + 16);
  };

  return (
    <ConceptShell
      name="SYNAPSE"
      tagline="Des dendrites vivantes qui ondulent depuis Bob. La connexion active est un canal de réflexion : le front de pensée y avance et l'activité brute y défile, légendée en clair."
      accent={P.accent}
      footNote="dendrites = sous-agents · ◌ nœud = phase">
      <canvas ref={canvasRef} style={{ position: 'absolute', inset: 0, width: '100%', height: '100%', display: 'block' }} />
    </ConceptShell>
  );
}

// clear "what is Bob doing right now" caption near the advancing front
function drawReflection(ctx, fx, fy, s, pc, goRight, w, F, P, roundRect) {
  const phase = s.phaseLabel.toUpperCase();
  const act = s.act || '';
  ctx.font = `600 9px ${F.mono}`;
  const pw = ctx.measureText(phase).width;
  ctx.font = `11px ${F.sans}`;
  const aw = ctx.measureText(act).width;
  const padX = 9, gap = 8, dot = 7;
  const boxW = padX + dot + 6 + Math.max(pw, aw) + padX;
  const boxH = 34;
  let bx = goRight ? fx + 16 : fx - 16 - boxW;
  let by = fy - boxH / 2;
  // keep inside frame (both axes)
  bx = Math.max(28, Math.min(w - boxW - 28, bx));
  by = Math.max(70, by);

  // connector
  ctx.beginPath(); ctx.moveTo(fx, fy); ctx.lineTo(goRight ? bx : bx + boxW, by + boxH / 2);
  ctx.strokeStyle = hexAy(pc, 0.4); ctx.lineWidth = 1; ctx.stroke();

  // pill
  roundRect(ctx, bx, by, boxW, boxH, 5);
  ctx.fillStyle = 'rgba(10,6,6,0.82)'; ctx.fill();
  ctx.strokeStyle = hexAy(pc, 0.4); ctx.lineWidth = 1; ctx.stroke();
  // left accent
  roundRect(ctx, bx, by, 2.5, boxH, 1);
  ctx.fillStyle = pc; ctx.fill();

  const tx = bx + padX + dot + 6;
  // phase row
  ctx.beginPath(); ctx.arc(bx + padX + dot / 2, by + 11, dot / 2, 0, Math.PI * 2);
  ctx.fillStyle = pc; ctx.fill();
  ctx.textAlign = 'left'; ctx.textBaseline = 'middle';
  ctx.font = `600 8.5px ${F.mono}`; ctx.fillStyle = hexAy(pc, 0.95);
  ctx.save(); ctx.letterSpacing = '0.12em'; ctx.fillText(phase, tx, by + 11); ctx.restore();
  // action row
  ctx.font = `11px ${F.sans}`; ctx.fillStyle = P.ink;
  ctx.fillText(act, bx + padX, by + 24);

  function hexAy(hex, a) {
    const hh = hex.replace('#', '');
    const n = parseInt(hh.length === 3 ? hh.split('').map((c) => c + c).join('') : hh, 16);
    return `rgba(${(n >> 16) & 255},${(n >> 8) & 255},${n & 255},${a})`;
  }
}

window.SynapseConcept = SynapseConcept;
