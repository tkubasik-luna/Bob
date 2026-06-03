// concept-stream.jsx — PISTE 2 · STREAM
// Bob is a source aperture on the left, emitting a current of thought into one
// channel per parallel task. Raw activity (short tokens) flows from the source
// toward a luminous crest — and the crest's position along the channel IS the
// progress. Phase gates light as the current passes them. Sub-agents are
// tributaries peeling off during the tool phase. Metaphor: flux / living river.

const { useRef: useRefS } = React;

function hexAs(hex, a) {
  const h = hex.replace('#', '');
  const n = parseInt(h.length === 3 ? h.split('').map((c) => c + c).join('') : h, 16);
  return `rgba(${(n >> 16) & 255},${(n >> 8) & 255},${n & 255},${a})`;
}

function StreamConcept() {
  const P = BOB.PALETTE, F = BOB.FONTS;
  const drawRef = useRefS(null);
  const canvasRef = useStageCanvas(drawRef);

  // phase boundary fractions (within the working span) per task, for gates
  const bounds = {};
  BOB.TASKS.forEach((t) => {
    bounds[t.id] = t._spans.map((sp) => ({ key: sp.key, f: (sp.end - t.gap) / (t._work - t.gap) }));
  });

  drawRef.current = (ctx, w, h, t) => {
    ctx.clearRect(0, 0, w, h);
    const snap = BOB.snapshot(t);
    const x0 = 250, x1 = w - 150;
    const top = h * 0.20, bot = h * 0.74;
    const laneY = (i) => top + (i + 0.5) * ((bot - top) / snap.length);
    const span = x1 - x0;

    // ── source aperture (Bob) ──
    const srcX = x0 - 34;
    const breathe = 1 + Math.sin(t * 1.7) * 0.06;
    const apTop = top + 4, apBot = bot - 4;
    const sg = ctx.createLinearGradient(srcX - 14, 0, srcX + 14, 0);
    sg.addColorStop(0, hexAs(P.accent, 0));
    sg.addColorStop(0.5, hexAs(P.accent, 0.5 * breathe));
    sg.addColorStop(1, hexAs(P.accent, 0));
    ctx.fillStyle = sg;
    ctx.fillRect(srcX - 14, apTop, 28, apBot - apTop);
    ctx.beginPath();
    ctx.moveTo(srcX, apTop); ctx.lineTo(srcX, apBot);
    ctx.strokeStyle = hexAs(P.accent3, 0.8 * breathe); ctx.lineWidth = 2;
    ctx.shadowBlur = 18; ctx.shadowColor = P.accent; ctx.stroke(); ctx.shadowBlur = 0;
    ctx.save();
    ctx.translate(srcX - 26, (apTop + apBot) / 2); ctx.rotate(-Math.PI / 2);
    ctx.textAlign = 'center'; ctx.font = `10px ${F.mono}`; ctx.fillStyle = P.inkFaint;
    ctx.fillText('BOB · SOURCE', 0, 0); ctx.restore();

    snap.forEach((s, i) => {
      const y = laneY(i);
      const live = s.status === 'running';
      const errored = s.status === 'error';
      const done = s.status === 'done';
      const queued = s.status === 'queued';
      const front = x0 + s.progress * span;
      const pc = s.tintPhase, tc = s.tint;

      // future track (ahead of crest)
      ctx.setLineDash([2, 6]);
      ctx.beginPath(); ctx.moveTo(front, y); ctx.lineTo(x1, y);
      ctx.strokeStyle = hexAs(tc, 0.16); ctx.lineWidth = 1; ctx.stroke();
      ctx.setLineDash([]);

      // phase gates
      bounds[s.id].forEach((b, k) => {
        if (k === bounds[s.id].length - 1) return;
        const gx = x0 + b.f * span;
        const passed = s.progress >= b.f;
        ctx.beginPath(); ctx.moveTo(gx, y - 9); ctx.lineTo(gx, y + 9);
        ctx.strokeStyle = passed ? hexAs(BOB.PHASES[b.key].tint, 0.7) : hexAs(tc, 0.2);
        ctx.lineWidth = passed ? 1.5 : 1; ctx.stroke();
      });

      // completed current band (source → crest): soft fill + flowing sine
      if (!queued && front > x0 + 1) {
        const bg = ctx.createLinearGradient(x0, 0, front, 0);
        bg.addColorStop(0, hexAs(errored ? P.err : tc, 0.05));
        bg.addColorStop(1, hexAs(errored ? P.err : pc, 0.22));
        ctx.fillStyle = bg;
        ctx.fillRect(x0, y - 6, front - x0, 12);
        // flowing sine line
        ctx.beginPath();
        for (let x = x0; x <= front; x += 5) {
          const yy = y + Math.sin((x - t * 90) * 0.045 + i) * 3.2;
          x === x0 ? ctx.moveTo(x, yy) : ctx.lineTo(x, yy);
        }
        ctx.strokeStyle = hexAs(errored ? P.err : tc, 0.5); ctx.lineWidth = 1; ctx.stroke();
      }

      // flowing tokens (raw activity) — procedural, deterministic
      if (!queued && front > x0 + 8) {
        const intensity = s.phaseKey === 'thinking' ? 1.5 : s.phaseKey === 'tools' ? 1.2 : 1;
        const N = 11;
        ctx.font = `9px ${F.mono}`; ctx.textAlign = 'center'; ctx.textBaseline = 'middle';
        for (let n = 0; n < N; n++) {
          const seed = BOB.hash01(i * 53 + n * 17);
          const u = ((t * (0.10 + 0.05 * intensity) + seed) % 1);
          const x = x0 + u * (front - x0);
          const fade = Math.sin(u * Math.PI);          // fade in/out across run
          const yy = y + Math.sin(t * 1.5 + seed * 9) * 4;
          const tk = BOB.STREAM_TOKENS[Math.floor(BOB.hash01(i * 7 + n * 3 + Math.floor(u * 4)) * BOB.STREAM_TOKENS.length)];
          ctx.fillStyle = hexAs(errored ? P.err : P.accent2, fade * 0.75);
          ctx.fillText(tk, x, yy);
        }
      }

      // crest (the progress head)
      if (!queued) {
        const cpulse = live ? 1 + Math.sin(t * 6 + i) * 0.18 : 1;
        ctx.save();
        ctx.shadowBlur = live ? 20 : 8; ctx.shadowColor = errored ? P.err : pc;
        // vertical glow blade
        const bgrad = ctx.createLinearGradient(0, y - 16, 0, y + 16);
        bgrad.addColorStop(0, hexAs(errored ? P.err : pc, 0));
        bgrad.addColorStop(0.5, hexAs(errored ? P.err : pc, live ? 0.9 : 0.5));
        bgrad.addColorStop(1, hexAs(errored ? P.err : pc, 0));
        ctx.fillStyle = bgrad;
        ctx.fillRect(front - 1.5, y - 16 * cpulse, 3, 32 * cpulse);
        ctx.beginPath(); ctx.arc(front, y, (done ? 4 : 5) * cpulse, 0, Math.PI * 2);
        ctx.fillStyle = done ? P.ok : errored ? P.err : pc; ctx.fill();
        ctx.restore();
      } else {
        // queued: dim seed at source
        ctx.beginPath(); ctx.arc(x0, y, 3, 0, Math.PI * 2);
        ctx.fillStyle = hexAs(tc, 0.4); ctx.fill();
      }

      // sub-agent tributaries (peel below during/after tools)
      s.subs.forEach((sub, j) => {
        if (sub.status === 'queued') return;
        const baseX = x0 + span * (bounds[s.id].find((b) => b.key === 'tools')?.f || 0.5) * 0.7;
        const tx = baseX + j * 26;
        if (tx > front + 4) return;
        const depth = 14 + j * 4;
        const mc = sub.status === 'error' ? P.err : sub.status === 'done' ? P.ok : tc;
        ctx.beginPath();
        ctx.moveTo(tx, y);
        ctx.quadraticCurveTo(tx + 10, y + depth * 0.5, tx + 22, y + depth);
        ctx.strokeStyle = hexAs(mc, 0.4); ctx.lineWidth = 1; ctx.stroke();
        const dotp = sub.status === 'running' ? (Math.sin(t * 3 + j) * 0.5 + 0.5) : 1;
        ctx.beginPath(); ctx.arc(tx + 22, y + depth, sub.status === 'running' ? 2 + dotp : 2, 0, Math.PI * 2);
        ctx.fillStyle = mc; ctx.fill();
      });

      // result crystal at the end
      if (done || errored) {
        const rx = x1 + 14;
        ctx.textAlign = 'left'; ctx.textBaseline = 'middle'; ctx.font = `9px ${F.mono}`;
        ctx.fillStyle = done ? P.ok : P.err;
        ctx.fillText(done ? '✓' : '✗', rx, y);
      }

      // lane label (left gutter)
      ctx.textAlign = 'left'; ctx.textBaseline = 'alphabetic';
      ctx.font = `500 12px ${F.sans}`;
      ctx.fillStyle = hexAs(P.ink, queued ? 0.42 : 0.92);
      ctx.fillText(s.title, 36, y - 5);
      ctx.font = `9px ${F.mono}`;
      ctx.fillStyle = errored ? P.err : done ? P.ok : queued ? P.inkFaint : pc;
      const sub = queued ? 'QUEUED'
        : done ? 'DELIVERED'
        : errored ? 'FAILED · RECOVERED'
        : `${s.phaseLabel.toUpperCase()} · ${Math.round(s.progress * 100)}%`;
      ctx.fillText(sub, 36, y + 9);
      // live activity word under the label
      if (live) {
        ctx.fillStyle = hexAs(P.inkDim, 0.8);
        ctx.fillText(s.act, 36, y + 22);
      }
    });
  };

  return (
    <ConceptShell
      name="STREAM"
      tagline="Bob emits a current of thought into one channel per task. The luminous crest is the progress; the drifting tokens are the raw activity."
      accent={P.accent}
      footNote="affluents = sub-agents">
      <canvas ref={canvasRef} style={{ position: 'absolute', inset: 0, width: '100%', height: '100%', display: 'block' }} />
    </ConceptShell>
  );
}

window.StreamConcept = StreamConcept;
