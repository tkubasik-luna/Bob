// agents.jsx — Live agent console. Jarvis (orchestrator, persistent lane) delegates
// to sub-agents spawned per task. Each lane streams through phases:
//   loading → reading context → thinking → using tools → writing answer → done|error
// Content shapes per the brief: phase+progress, thinking monologue (collapsible),
// narrated-step fallback, tool chips (start→args→result), streamed answer markdown,
// perf footer, inline error. Aesthetic matches the warm/calm HUD vocabulary.

const { useState: useStateAg, useEffect: useEffectAg, useRef: useRefAg } = React;

// ── phase → display + sphere-state mapping ──────────────────────────────
const PHASE_META = {
  loading: { label: 'Loading model',   tint: 'var(--accent)',  sphere: 'idle'   },
  reading: { label: 'Reading context', tint: '#8FB3C6',        sphere: 'listen' },
  thinking:{ label: 'Thinking',        tint: '#A89BC0',        sphere: 'think'  },
  tool:    { label: 'Using tools',     tint: '#C2A06A',        sphere: 'think'  },
  delegate:{ label: 'Delegating',      tint: '#C2A06A',        sphere: 'think'  },
  writing: { label: 'Writing answer',  tint: '#8FBA9F',        sphere: 'speak'  },
  done:    { label: 'Done',            tint: '#8FBA9F',        sphere: 'idle'   },
  error:   { label: 'Failed',          tint: '#BE8B8B',        sphere: 'error'  },
};

const SUB_TINTS = ['#82A4AE', '#8FA585', '#C2A06A', '#A88BA2', '#8294B0'];

// ── scenario library — two cycle, A succeeds, B contains a failure ──────
function scenarioInbox() {
  return {
    prompt: '“Catch me up on what Daniela sent this week — and am I free Thursday afternoon?”',
    agents: [
      {
        id: 'jarvis', kind: 'jarvis', name: 'JARVIS', role: 'orchestrator', offset: 0,
        phases: [
          { key: 'loading', dur: 1.3, prog: true },
          { key: 'reading', dur: 1.9, prog: true, meta: '8 sources · 9.4k tok' },
          { key: 'thinking', dur: 4.6, think:
            "Two asks here. First, surface what Daniela sent in the last seven days and separate the signal from the noise. Second, check whether Thursday afternoon is actually open. I shouldn't do either from memory — I'll delegate: one agent on Gmail with a tight sender+date filter, another reading the calendar. While they run I'll hold the thread and decide the framing — lead with anything time-sensitive, then answer the meeting question plainly." },
          { key: 'delegate', dur: 6.4, label: 'Delegating · 2 agents', tools: [
            { name: 'spawn', args: 'agent · gmail.search', result: 'dispatched', ok: true },
            { name: 'spawn', args: 'agent · calendar.read', result: 'dispatched', ok: true },
          ] },
          { key: 'writing', dur: 5.4, answer:
            "**Daniela — 3 of 12 worth your time:**\n- Budget v4 needs your sign-off by **Fri**\n- Moved the design review to **Thu 3pm**\n- FYI only: vendor contract renewed\n\nThursday afternoon is **mostly open** — just the 3pm review she moved. Want me to hold 4–5pm for focus?" },
          { key: 'done', dur: Infinity, perf: { tokS: 47, ttft: 0.7, think: 312, ctx: '9.4k' } },
        ],
      },
      {
        id: 'gmail', kind: 'sub', name: 'GMAIL SEARCH', role: 'sub-agent', offset: 8.4,
        phases: [
          { key: 'loading', dur: 0.7, prog: true },
          { key: 'reading', dur: 1.0, prog: true, meta: 'mailbox · primary' },
          { key: 'tool', dur: 3.0, label: 'Searching', tools: [
            { name: 'gmail.search', args: 'from:daniela after:2026-05-25', result: '12 messages · 3 flagged', ok: true },
          ] },
          { key: 'writing', dur: 1.6, answer: "12 messages. 3 are time-sensitive: budget sign-off, review moved to Thu 3pm, contract renewal." },
          { key: 'done', dur: Infinity, perf: { tokS: 61, ttft: 0.4, think: 0, ctx: '2.1k' } },
        ],
      },
      {
        id: 'cal', kind: 'sub', name: 'CALENDAR', role: 'sub-agent', offset: 9.1,
        phases: [
          { key: 'loading', dur: 0.6, prog: true },
          { key: 'tool', dur: 2.9, label: 'Reading calendar', steps: [
            'Opening calendar', 'Range · Thu 12:00–18:00', 'Filtering busy blocks', '1 event found',
          ], tools: [
            { name: 'calendar.list', args: 'date:2026-06-04 12:00-18:00', result: '1 event · 15:00 design review', ok: true },
          ] },
          { key: 'writing', dur: 1.3, answer: "Thursday PM: one event at 3pm. 12–3 and 4–6 are free." },
          { key: 'done', dur: Infinity, perf: { tokS: 58, ttft: 0.5, think: 0, ctx: '1.4k' } },
        ],
      },
    ],
  };
}

function scenarioReview() {
  return {
    prompt: '“Review PR #284 and tell me why CI is red.”',
    agents: [
      {
        id: 'jarvis', kind: 'jarvis', name: 'JARVIS', role: 'orchestrator', offset: 0,
        phases: [
          { key: 'loading', dur: 1.3, prog: true },
          { key: 'reading', dur: 2.1, prog: true, meta: '24 files · 18k tok' },
          { key: 'thinking', dur: 4.8, think:
            "CI is failing on #284, so I need the actual diff and the real job log — not a guess. One agent reads the changed files, another pulls the failing test output. If it's a flaky timeout I'll say so; if it's a genuine assertion I'll point at the line. The user wants the cause, not a lecture — keep the answer tight." },
          { key: 'delegate', dur: 6.8, label: 'Delegating · 2 agents', tools: [
            { name: 'spawn', args: 'agent · repo.read', result: 'dispatched', ok: true },
            { name: 'spawn', args: 'agent · ci.run', result: 'dispatched', ok: true },
          ] },
          { key: 'writing', dur: 5.2, answer:
            "**PR #284 — one blocker.**\n- CI is red on `auth.spec.ts:42`. Not flaky — a real assertion: TTL expected **3600**, got **300**.\n- The change in `config.ts` overrode the default.\n\nEverything else passes. Fix the TTL and it's good to merge." },
          { key: 'done', dur: Infinity, perf: { tokS: 44, ttft: 0.8, think: 356, ctx: '18k' } },
        ],
      },
      {
        id: 'repo', kind: 'sub', name: 'REPO READ', role: 'sub-agent', offset: 8.6,
        phases: [
          { key: 'loading', dur: 0.7, prog: true },
          { key: 'reading', dur: 1.1, prog: true, meta: 'tree · 24 files' },
          { key: 'tool', dur: 3.0, label: 'Reading diff', tools: [
            { name: 'repo.read', args: 'pr:284 --files', result: '6 files · +142 −38', ok: true },
          ] },
          { key: 'writing', dur: 1.5, answer: "Key change: config.ts sets tokenTTL=300. Rest is tests and types." },
          { key: 'done', dur: Infinity, perf: { tokS: 60, ttft: 0.4, think: 0, ctx: '4.2k' } },
        ],
      },
      {
        id: 'ci', kind: 'sub', name: 'TEST RUNNER', role: 'sub-agent', offset: 9.3,
        phases: [
          { key: 'loading', dur: 0.7, prog: true },
          { key: 'tool', dur: 2.7, label: 'Running tests', steps: [
            'Spawning runner', 'npm test', 'Collecting output',
          ], tools: [
            { name: 'ci.run', args: 'job:test --pr 284', result: '1 failed · auth.spec.ts:42', ok: false },
          ] },
          { key: 'error', dur: Infinity, error: 'Suite exited 1 — assertion failed (token TTL). Surfaced to Jarvis.', perf: { tokS: 39, ttft: 0.6, think: 0, ctx: '1.1k' } },
        ],
      },
    ],
  };
}

const SCENARIOS = [scenarioInbox, scenarioReview];

// ── precompute content-phase indices once per agent ────────────────────
function indexAgent(a) {
  a._think = a.phases.findIndex(p => p.think);
  a._tools = a.phases.findIndex(p => p.tools || p.steps);
  a._answer = a.phases.findIndex(p => p.answer);
  a._total = a.phases.reduce((s, p) => s + (p.dur === Infinity ? 0 : p.dur), 0);
  return a;
}
function makeSession(factory) {
  const s = factory();
  s.agents.forEach(indexAgent);
  s.start = performance.now() / 1000;
  s.maxEnd = Math.max(...s.agents.map(a => a.offset + a._total));
  return s;
}

const clamp01 = (x) => Math.max(0, Math.min(1, x));

// derive current phase index + elapsed-in-phase for an agent
function derive(a, sessionT) {
  const localT = sessionT - a.offset;
  if (localT < 0) return { spawned: false };
  let acc = 0, idx = a.phases.length - 1, elapsed = 0;
  for (let i = 0; i < a.phases.length; i++) {
    const dur = a.phases[i].dur;
    if (dur === Infinity || localT < acc + dur) { idx = i; elapsed = localT - acc; break; }
    acc += dur;
  }
  const phase = a.phases[idx];
  const status = phase.key === 'done' ? 'done' : phase.key === 'error' ? 'error' : 'running';
  return { spawned: true, idx, elapsed, phase, status };
}
// reveal fraction for content attached to phase i, given current idx/elapsed
function revealAt(i, idx, elapsed, phases) {
  if (i < 0) return 0;
  if (idx > i) return 1;
  if (idx < i) return 0;
  const d = phases[i].dur;
  return d && d !== Infinity ? clamp01(elapsed / d) : 1;
}
function takeChars(str, frac) {
  if (frac >= 1) return str;
  return str.slice(0, Math.floor(str.length * frac));
}
function wordCount(s) { return s.trim().split(/\s+/).length; }

// ── tiny markdown (bold + bullet list + paragraphs) ─────────────────────
function inlineMd(text, keyp) {
  const parts = text.split(/(\*\*[^*]+\*\*|`[^`]+`)/g).filter(Boolean);
  return parts.map((p, i) => {
    if (p.startsWith('**')) return <strong key={keyp + 'b' + i}>{p.slice(2, -2)}</strong>;
    if (p.startsWith('`')) return <code key={keyp + 'c' + i}>{p.slice(1, -1)}</code>;
    return <span key={keyp + 't' + i}>{p}</span>;
  });
}
function AnswerMd({ text }) {
  const lines = text.split('\n');
  const blocks = [];
  let list = null;
  lines.forEach((ln, i) => {
    if (ln.trim().startsWith('- ')) {
      if (!list) { list = []; blocks.push({ t: 'ul', items: list }); }
      list.push(ln.trim().slice(2));
    } else {
      list = null;
      if (ln.trim()) blocks.push({ t: 'p', text: ln });
    }
  });
  return (
    <div className="al-md">
      {blocks.map((b, i) =>
        b.t === 'ul'
          ? <ul className="al-md-ul" key={'u' + i}>{b.items.map((it, j) => <li key={j}>{inlineMd(it, 'u' + i + j)}</li>)}</ul>
          : <p className="al-md-p" key={'p' + i}>{inlineMd(b.text, 'p' + i)}</p>
      )}
    </div>
  );
}

// ── phase row: pip + label + right meta + progress/spinner ──────────────
function PhaseRow({ d, phase }) {
  const meta = PHASE_META[phase.key] || PHASE_META.loading;
  const label = phase.label || meta.label;
  const indeterminate = !phase.prog && phase.key !== 'done' && phase.key !== 'error';
  let right = '';
  if (phase.prog) right = `${Math.round(clamp01(d.elapsed / phase.dur) * 100)}%`;
  else if (phase.key === 'done') right = 'complete';
  else if (phase.key === 'error') right = 'fault';
  return (
    <div className="al-phase" style={{ '--phase-tint': meta.tint }}>
      <span className={`al-pip is-${phase.key}`} />
      <span className="al-phase-label">{label}</span>
      {phase.meta && d.status === 'running' && <span className="al-phase-meta">{phase.meta}</span>}
      <span className="al-phase-right">{right}</span>
      {phase.prog
        ? <span className="al-prog"><span className="al-prog-fill" style={{ width: `${clamp01(d.elapsed / phase.dur) * 100}%` }} /></span>
        : indeterminate
          ? <span className="al-prog al-prog-indef"><span className="al-prog-sweep" /></span>
          : <span className="al-prog al-prog-done" />}
    </div>
  );
}

// ── tool chip with start → args → result reveal ─────────────────────────
function ToolChip({ tool, frac, slot, slots }) {
  const seg = 1 / slots;
  const start = slot * seg;
  const argsAt = start + seg * 0.28;
  const resAt = start + seg * 0.78;
  if (frac < start) return null;
  const showArgs = frac >= argsAt;
  const showRes = frac >= resAt;
  return (
    <div className={`al-chip ${showRes ? (tool.ok ? 'is-ok' : 'is-fail') : 'is-run'}`}>
      <div className="al-chip-head">
        <span className="al-chip-mark" />
        <span className="al-chip-name">{tool.name}</span>
        {!showRes && <span className="al-chip-run">running</span>}
      </div>
      {showArgs && <div className="al-chip-args">{tool.args}</div>}
      {showRes && (
        <div className="al-chip-result">
          <span className="al-chip-glyph">{tool.ok ? '✓' : '✗'}</span>
          <span>{tool.result}</span>
        </div>
      )}
    </div>
  );
}

// ── one agent lane ──────────────────────────────────────────────────────
function AgentLane({ agent, d, sessionT, tint, openMap, setOpen }) {
  const { idx, elapsed, phase, status } = d;
  const phases = agent.phases;

  // thinking
  const thinkFrac = revealAt(agent._think, idx, elapsed, phases);
  const thinkPhase = agent._think >= 0 ? phases[agent._think] : null;
  const thinkActive = idx === agent._think;
  const key = agent.id;
  const userOpen = openMap[key];
  const thinkOpen = userOpen === undefined ? thinkActive : userOpen;

  // tools / steps
  const toolFrac = revealAt(agent._tools, idx, elapsed, phases);
  const toolPhase = agent._tools >= 0 ? phases[agent._tools] : null;

  // answer
  const ansFrac = revealAt(agent._answer, idx, elapsed, phases);
  const ansPhase = agent._answer >= 0 ? phases[agent._answer] : null;
  const ansText = ansPhase ? takeChars(ansPhase.answer, ansFrac) : '';

  // sub-agents auto-collapse once finished (brief: "appear, work, finish, can be collapsed").
  // user toggle always wins.
  const laneKey = 'lane:' + key;
  const autoFold = agent.kind === 'sub' && (status === 'done' || status === 'error');
  const collapsed = openMap[laneKey] === undefined ? autoFold : openMap[laneKey] === true;
  const meta = PHASE_META[phase.key] || PHASE_META.loading;

  return (
    <div className={`agent-lane is-${agent.kind} status-${status} ${collapsed ? 'is-folded' : ''}`}
         data-aid={agent.id}
         style={{ '--lane-tint': tint }}>
      <button className="al-head" onClick={() => setOpen('lane:' + key, !collapsed)}>
        <span className="al-glyph">{agent.kind === 'jarvis' ? '⌬' : '◇'}</span>
        <span className="al-name">{agent.name}</span>
        <span className="al-role">{agent.role}</span>
        <span className="al-chev" style={{ color: meta.tint }}>{collapsed ? '▸' : '▾'}</span>
      </button>

      <PhaseRow d={d} phase={phase} />

      {collapsed && (status === 'done' || status === 'error') && (() => {
        let txt = phase.error || '';
        if (!txt && toolPhase && toolPhase.tools) txt = toolPhase.tools[toolPhase.tools.length - 1].result;
        if (!txt && ansPhase) txt = ansPhase.answer.replace(/\*\*|`/g, '').split('\n')[0];
        return txt ? <div className={`al-fold ${status === 'error' ? 'is-err' : ''}`}>{txt}</div> : null;
      })()}

      {!collapsed && (
        <div className="al-body">
          {/* THINKING — collapsible monologue */}
          {thinkPhase && thinkFrac > 0 && (
            <div className="al-think">
              <button className="al-think-head" onClick={() => setOpen(key, !thinkOpen)}>
                <span className="al-think-label">Thinking</span>
                <span className="al-think-count">
                  {thinkActive && thinkFrac < 1 ? 'streaming…' : `${wordCount(thinkPhase.think)} words`}
                </span>
                <span className="al-think-toggle">{thinkOpen ? 'hide' : 'show'}</span>
              </button>
              {thinkOpen && (
                <div className="al-think-body">
                  {takeChars(thinkPhase.think, thinkFrac)}
                  {thinkActive && thinkFrac < 1 && <span className="al-caret" />}
                </div>
              )}
            </div>
          )}

          {/* NARRATED STEPS — fallback when no thinking */}
          {toolPhase && toolPhase.steps && toolFrac > 0 && (
            <ul className="al-steps">
              {toolPhase.steps.slice(0, Math.ceil(toolFrac * toolPhase.steps.length)).map((s, i) => (
                <li key={i}>{s}</li>
              ))}
            </ul>
          )}

          {/* TOOL CHIPS */}
          {toolPhase && toolPhase.tools && toolFrac > 0 && (
            <div className="al-tools">
              {toolPhase.tools.map((t, i) => (
                <ToolChip key={i} tool={t} frac={toolFrac} slot={i} slots={toolPhase.tools.length} />
              ))}
            </div>
          )}

          {/* ANSWER — streamed markdown */}
          {ansPhase && ansFrac > 0 && (
            <div className="al-answer">
              <div className="al-answer-cap">Answer</div>
              <AnswerMd text={ansText} />
              {idx === agent._answer && ansFrac < 1 && <span className="al-caret al-caret-ink" />}
            </div>
          )}

          {/* ERROR — inline red state */}
          {status === 'error' && phase.error && (
            <div className="al-error">
              <span className="al-error-glyph">✗</span>
              <span>{phase.error}</span>
            </div>
          )}

          {/* PERF FOOTER — on terminal */}
          {(status === 'done' || status === 'error') && phase.perf && (
            <div className="al-perf">
              <span><b>{phase.perf.tokS}</b> tok/s</span>
              <span><b>{phase.perf.ttft}s</b> ttft</span>
              {phase.perf.think > 0 && <span><b>{phase.perf.think}</b> think</span>}
              <span><b>{phase.perf.ctx}</b> ctx</span>
            </div>
          )}
        </div>
      )}
    </div>
  );
}

// ── system telemetry strip (reused HUD component) + meta footer ─────────
function ConsoleFoot({ sphereState, modelLabel }) {
  const { HUDTelemetry } = window.HUD;
  return (
    <div className="ac-foot">
      <div className="ac-foot-cap">SYSTEM LOAD<span className="ac-foot-models">{modelLabel || 'haiku-4.5 · o4-mini'}</span></div>
      <HUDTelemetry state={sphereState} />
    </div>
  );
}

// ── compact status row (shown when the rail is collapsed) ───────────────
function CompactRow({ a, d, tint, onClick }) {
  const { phase, status, elapsed } = d;
  const meta = PHASE_META[phase.key] || PHASE_META.loading;
  const pct = phase.prog ? Math.round(clamp01(elapsed / phase.dur) * 100) : null;
  const label = status === 'done' ? 'done' : status === 'error' ? 'failed' : (phase.label || meta.label);
  const indef = status === 'running' && pct === null;
  return (
    <button className={`ac-crow is-${a.kind} status-${status}`} style={{ '--lane-tint': tint, '--phase-tint': meta.tint }} onClick={onClick}>
      <span className="ac-crow-glyph">{a.kind === 'jarvis' ? '⌬' : '◇'}</span>
      <span className="ac-crow-name">{a.name}</span>
      <span className={`al-pip is-${phase.key}`} />
      <span className="ac-crow-phase">{label}</span>
      <span className="ac-crow-right">
        {pct !== null ? pct + '%' : indef
          ? <span className="ac-crow-dots"><i /><i /><i /></span>
          : status === 'done' ? '✓' : status === 'error' ? '✗' : ''}
      </span>
      <span className="ac-crow-track">
        <span className="ac-crow-fill" style={{ width: (status === 'done' || status === 'error' ? 100 : pct === null ? 100 : pct) + '%', opacity: indef ? 0.32 : 1 }} />
      </span>
    </button>
  );
}


function AgentConsole({ onSphereState, modelLabel }) {
  const [scenIdx, setScenIdx] = useStateAg(0);
  const sessionRef = useRefAg(null);
  if (!sessionRef.current) sessionRef.current = makeSession(SCENARIOS[0]);
  const [, force] = useStateAg(0);
  const [openMap, setOpenMap] = useStateAg({});
  const [railOpen, setRailOpen] = useStateAg(true);
  const lastSphere = useRefAg('idle');
  const scrollRef = useRefAg(null);
  const stickRef = useRefAg(true);
  const pauseTo = useRefAg(0);
  const focusRef = useRefAg(null);

  const setOpen = (k, v) => setOpenMap(m => ({ ...m, [k]: v }));

  // Follow the active agent: keep the lane that's currently streaming in view
  // (Jarvis while it writes the answer; the latest running sub-agent otherwise).
  // A mouse-wheel gesture pauses the follow for a few seconds so the log is readable.
  useEffectAg(() => {
    const sc = scrollRef.current;
    if (!sc) return;
    if (performance.now() < pauseTo.current) return;
    const id = focusRef.current;
    const lane = id ? sc.querySelector('[data-aid="' + id + '"]') : null;
    if (lane) {
      const target = lane.offsetTop + lane.offsetHeight - sc.clientHeight + 10;
      sc.scrollTop = Math.max(0, target);
    } else {
      sc.scrollTop = sc.scrollHeight;
    }
  });
  const onWheel = () => { pauseTo.current = performance.now() + 4000; };

  useEffectAg(() => {
    let raf;
    const tick = () => {
      const s = sessionRef.current;
      const sessionT = performance.now() / 1000 - s.start;

      // drive sphere from Jarvis's mapped phase
      const jar = s.agents[0];
      const dj = derive(jar, sessionT);
      if (dj.spawned) {
        const sp = (PHASE_META[dj.phase.key] || {}).sphere || 'idle';
        if (sp !== lastSphere.current) { lastSphere.current = sp; onSphereState && onSphereState(sp); }
      }

      // reset to next scenario after the whole session settles
      if (sessionT > s.maxEnd + 4.8) {
        const next = (scenIdx + 1) % SCENARIOS.length;
        sessionRef.current = makeSession(SCENARIOS[next]);
        setScenIdx(next);
        setOpenMap({});
        pauseTo.current = 0;
      }
      force(x => (x + 1) & 0xffff);
      raf = setTimeout(tick, 80);
    };
    tick();
    return () => clearTimeout(raf);
  }, [scenIdx]);

  const s = sessionRef.current;
  const sessionT = performance.now() / 1000 - s.start;
  const derived = s.agents.map(a => ({ a, d: derive(a, sessionT) })).filter(x => x.d.spawned);
  const working = derived.filter(x => x.d.status === 'running').length;
  const total = derived.length;
  // focus = last running agent in render order; if none running, the orchestrator (its answer is the payload)
  const running = derived.filter(x => x.d.status === 'running');
  focusRef.current = running.length ? running[running.length - 1].a.id : (derived[0] && derived[0].a.id);

  // stable per-agent tint, shared by expanded lanes and compact rows
  const tints = {}; let si = 0;
  derived.forEach(({ a }) => { tints[a.id] = a.kind === 'jarvis' ? 'var(--accent-2)' : SUB_TINTS[(si++) % SUB_TINTS.length]; });

  return (
    <div className={`agent-console ${railOpen ? '' : 'is-collapsed'}`}>
      <div className="ac-head">
        <div className="ac-head-l">
          <span className="ac-live-dot" />
          <span className="ac-title">AGENTS · LIVE</span>
        </div>
        <div className="ac-head-r">
          <span className="ac-count">
            <span className={working ? 'is-working' : ''}>{String(working).padStart(2, '0')}</span>
            <span className="ac-count-lbl">working</span>
            <span className="ac-count-sep">/</span>
            <span>{String(total).padStart(2, '0')}</span>
          </span>
          <button className="ac-fold" onClick={() => setRailOpen(o => !o)} title={railOpen ? 'Collapse' : 'Expand'}>{railOpen ? '⌃' : '⌄'}</button>
        </div>
      </div>

      {railOpen ? (
        <>
          <div className="ac-prompt">{s.prompt}</div>
          <div className="ac-scroll" ref={scrollRef} onWheel={onWheel}>
            {derived.map(({ a, d }) => (
              <AgentLane key={a.id} agent={a} d={d} sessionT={sessionT}
                         tint={tints[a.id]} openMap={openMap} setOpen={setOpen} />
            ))}
          </div>
          <ConsoleFoot sphereState={lastSphere.current} modelLabel={modelLabel} />
        </>
      ) : (
        <div className="ac-compact">
          <div className="ac-compact-prompt">{s.prompt}</div>
          <div className="ac-compact-list">
            {derived.map(({ a, d }) => (
              <CompactRow key={a.id} a={a} d={d} tint={tints[a.id]} onClick={() => setRailOpen(true)} />
            ))}
          </div>
          <div className="ac-compact-hint">tap a row to expand</div>
        </div>
      )}
    </div>
  );
}

window.AgentConsole = AgentConsole;
