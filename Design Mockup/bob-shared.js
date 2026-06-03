// bob-shared.js — shared backbone for the four progress explorations.
// One palette, one task fixture, one synced clock. Every concept renders the
// SAME four parallel tasks at the SAME instant, in a different visual language.
// Plain script (no JSX) — loaded before the babel concept files.

(function () {
  // ── warm / calm palette (lifted from the live Bob app) ───────────────
  const PALETTE = {
    bg:      '#0A0606',
    bg2:     '#170B0A',
    bg3:     '#1E0F0C',
    ink:     '#FFE7DD',
    inkDim:  'rgba(255,231,221,0.55)',
    inkFaint:'rgba(255,231,221,0.26)',
    inkGhost:'rgba(255,231,221,0.10)',
    accent:  '#FF7A45',
    accent2: '#FFB6A0',
    accent3: '#FFE4D7',
    warn:    '#E8B463',
    err:     '#D77A6A',
    ok:      '#8FBA9F',
  };

  // Muted polychrome — one per parallel task. Calm, distinguishable.
  const TINTS = {
    teal:  '#7FB0BE',
    sage:  '#9CB58C',
    amber: '#D2A766',
    mauve: '#B895AE',
  };

  const FONTS = {
    sans: "'Space Grotesk', system-ui, sans-serif",
    mono: "'JetBrains Mono', ui-monospace, monospace",
    serif:"'Newsreader', Georgia, serif",
  };

  // ── phase vocabulary ─────────────────────────────────────────────────
  // The stages an autonomous task moves through. tint = phase accent.
  const PHASES = {
    queued:  { label: 'Queued',          tint: PALETTE.inkFaint, verb: 'waiting' },
    reading: { label: 'Reading context', tint: '#8FB3C6',        verb: 'reading' },
    thinking:{ label: 'Thinking',        tint: '#B6A6CE',        verb: 'reasoning' },
    tools:   { label: 'Using tools',     tint: PALETTE.warn,     verb: 'acting' },
    writing: { label: 'Composing',       tint: PALETTE.ok,       verb: 'writing' },
    done:    { label: 'Complete',        tint: PALETTE.ok,       verb: 'done' },
    error:   { label: 'Failed',          tint: PALETTE.err,      verb: 'failed' },
  };
  const PHASE_ORDER = ['reading', 'thinking', 'tools', 'writing'];

  // ── task fixture ─────────────────────────────────────────────────────
  // Each task loops on its own clock (period = queued gap + work + done hold),
  // staggered by `offset`, so at any instant there is a lively mix of
  // queued / running / done across the four. `outcome` is the terminal state.
  const TASKS = [
    {
      id: 'inbox', key: 'teal', tint: TINTS.teal,
      title: 'Inbox triage',
      prompt: 'Catch me up on what Daniela sent this week — am I free Thursday?',
      offset: 0.0, gap: 2.4, hold: 4.2, outcome: 'done',
      phases: [
        { key: 'reading',  dur: 2.4, acts: ['opening primary inbox', 'indexing 142 threads', 'ranking by sender'] },
        { key: 'thinking', dur: 3.2, acts: ['two asks · split them', 'signal vs noise', 'plan delegation'] },
        { key: 'tools',    dur: 4.0, acts: ['gmail.search from:daniela', 'calendar.read thu 12–18', 'merging results'] },
        { key: 'writing',  dur: 3.0, acts: ['3 of 12 worth your time', 'drafting summary', 'proposing 4–5pm hold'] },
      ],
      subs: [
        { name: 'gmail.search',  spawn: 5.6, dur: 3.0, outcome: 'done',  result: '12 found · 3 flagged' },
        { name: 'calendar.read', spawn: 6.3, dur: 2.8, outcome: 'done',  result: '1 event · 3pm' },
      ],
    },
    {
      id: 'pr', key: 'amber', tint: TINTS.amber,
      title: 'PR #284 review',
      prompt: 'Review PR #284 and tell me why CI is red.',
      offset: 5.5, gap: 2.0, hold: 4.6, outcome: 'done',
      phases: [
        { key: 'reading',  dur: 2.6, acts: ['cloning pr #284', 'reading 6 changed files', '+142 −38'] },
        { key: 'thinking', dur: 3.6, acts: ['flaky or real?', 'trace the failing job', 'isolate the assertion'] },
        { key: 'tools',    dur: 4.4, acts: ['repo.read --files', 'ci.run job:test', 'parsing failure log'] },
        { key: 'writing',  dur: 3.0, acts: ['one blocker found', 'auth.spec.ts:42', 'TTL 3600 vs 300'] },
      ],
      subs: [
        { name: 'repo.read',   spawn: 5.4, dur: 3.0, outcome: 'done',  result: '6 files · config.ts' },
        { name: 'test.runner', spawn: 6.2, dur: 3.4, outcome: 'error', result: 'exit 1 · auth.spec:42' },
      ],
    },
    {
      id: 'trip', key: 'sage', tint: TINTS.sage,
      title: 'Lisbon offsite',
      prompt: 'Plan the Lisbon offsite — flights and a hotel near the venue.',
      offset: 2.8, gap: 2.8, hold: 4.0, outcome: 'done',
      phases: [
        { key: 'reading',  dur: 2.2, acts: ['venue · LX Factory', 'team of 9', 'budget €18k'] },
        { key: 'thinking', dur: 3.0, acts: ['cluster arrivals', 'walkable to venue', 'optimise cost'] },
        { key: 'tools',    dur: 4.6, acts: ['flights.search MAD→LIS', 'hotels.near venue 800m', 'maps.route check'] },
        { key: 'writing',  dur: 3.2, acts: ['3 hotels shortlisted', 'flight blocks held', 'itinerary draft'] },
      ],
      subs: [
        { name: 'flights.search', spawn: 5.2, dur: 3.2, outcome: 'done', result: '4 routes · €312 avg' },
        { name: 'hotels.near',    spawn: 5.9, dur: 3.6, outcome: 'done', result: '3 within 800m' },
        { name: 'maps.route',     spawn: 7.4, dur: 2.4, outcome: 'done', result: '9 min walk' },
      ],
    },
    {
      id: 'churn', key: 'mauve', tint: TINTS.mauve,
      title: 'Q2 churn drivers',
      prompt: 'Summarize the Q2 churn drivers from the data room.',
      offset: 8.4, gap: 3.0, hold: 4.4, outcome: 'error',
      phases: [
        { key: 'reading',  dur: 2.6, acts: ['mounting data room', '14 sheets · 2.1M rows', 'schema check'] },
        { key: 'thinking', dur: 3.4, acts: ['cohort the accounts', 'isolate drivers', 'rank by impact'] },
        { key: 'tools',    dur: 4.2, acts: ['sql.query cohorts', 'stats.regress drivers', 'rate limit hit'] },
      ],
      subs: [
        { name: 'sql.query',    spawn: 5.0, dur: 3.0, outcome: 'done',  result: '8 cohorts built' },
        { name: 'stats.regress', spawn: 6.4, dur: 2.6, outcome: 'error', result: '429 · provider cap' },
      ],
    },
  ];

  // precompute timelines
  TASKS.forEach((t) => {
    let acc = t.gap;
    t._spans = t.phases.map((p) => {
      const span = { key: p.key, start: acc, end: acc + p.dur, dur: p.dur, acts: p.acts };
      acc += p.dur;
      return span;
    });
    t._work = acc;                 // queued-gap + all phase durations
    t._period = acc + t.hold;      // + done/error hold, then loop
  });

  // ── math helpers ─────────────────────────────────────────────────────
  const clamp01 = (x) => Math.max(0, Math.min(1, x));
  const lerp = (a, b, t) => a + (b - a) * t;
  const easeInOut = (t) => (t < 0.5 ? 2 * t * t : 1 - Math.pow(-2 * t + 2, 2) / 2);
  const easeOut = (t) => 1 - Math.pow(1 - t, 3);

  // deterministic small hash → [0,1)
  function hash01(n) { const s = Math.sin(n * 127.1) * 43758.5453; return s - Math.floor(s); }

  // ── synced clock — one origin for every concept ──────────────────────
  const T0 = performance.now() / 1000;
  function now() { return performance.now() / 1000 - T0; }

  // ── derive one task's live state at global time gt ────────────────────
  function deriveTask(t, gt) {
    const lt = (gt + t.offset) % t._period;     // local looping time
    let status, phaseKey, phaseLabel, progress, phaseIdx = -1, inPhase = 0, act = '';

    if (lt < t.gap) {
      status = 'queued'; phaseKey = 'queued'; progress = 0;
      phaseLabel = PHASES.queued.label; act = 'in queue';
    } else if (lt >= t._work) {
      // terminal hold
      status = t.outcome; phaseKey = t.outcome; progress = 1;
      phaseLabel = PHASES[t.outcome].label;
      act = t.outcome === 'error' ? 'recovered · surfaced' : 'delivered';
      phaseIdx = t._spans.length - 1; inPhase = 1;
    } else {
      status = 'running';
      // find active span
      const span = t._spans.find((s) => lt < s.end) || t._spans[t._spans.length - 1];
      phaseKey = span.key; phaseLabel = PHASES[span.key].label;
      phaseIdx = t._spans.indexOf(span);
      inPhase = clamp01((lt - span.start) / span.dur);
      progress = clamp01((lt - t.gap) / (t._work - t.gap));
      const ai = Math.min(span.acts.length - 1, Math.floor(inPhase * span.acts.length));
      act = span.acts[ai];
    }

    // sub-agents
    const subs = t.subs.map((s, i) => {
      const e = lt - s.spawn;
      if (e < 0) return { name: s.name, status: 'queued', progress: 0, result: s.result };
      if (e >= s.dur) return { name: s.name, status: s.outcome, progress: 1, result: s.result };
      return { name: s.name, status: 'running', progress: clamp01(e / s.dur), result: s.result };
    });

    return {
      id: t.id, key: t.key, tint: t.tint, title: t.title, prompt: t.prompt,
      status, phaseKey, phaseLabel, phaseIdx, phaseCount: t._spans.length,
      progress, inPhase, act, subs, lt, period: t._period,
      tintPhase: PHASES[phaseKey].tint,
    };
  }

  function snapshot(gt) {
    if (gt == null) gt = now();
    return TASKS.map((t) => deriveTask(t, gt));
  }

  // shared mono activity stream — short tokens for "raw thought" texture
  const STREAM_TOKENS = [
    'scan', 'rank', 'embed', 'recall', 'weigh', 'branch', 'merge', 'verify',
    'sift', 'map', 'cite', 'draft', 'prune', 'fetch', 'parse', 'align',
    '0x9f', '·', '⟶', 'ctx+', 'tok', 'Δ', 'ok', '∴',
  ];

  window.BOB = {
    PALETTE, TINTS, FONTS, PHASES, PHASE_ORDER, TASKS,
    clamp01, lerp, easeInOut, easeOut, hash01,
    now, snapshot, deriveTask, STREAM_TOKENS,
  };
})();
