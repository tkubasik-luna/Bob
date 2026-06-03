// p3d-panels.jsx — the left thread deck + right data field + settings.
//
//   BobCard    — LE FIL DE LA CONSCIENCE PRINCIPALE. Prompt → réflexion →
//                tâches invoquées (références vivantes aux sous-tâches) →
//                réponse synthétisée. Primary chrome, round BOB orb glyph.
//   SubCard    — UNE TÂCHE créée par Bob. Réflexion → appel d'OUTIL → rendu.
//                Secondary chrome, ◇ glyph, teinte lavande, marqué « par BOB ».
//   ThreadStack — empile les cartes ; la carte vivante passe devant (glide CSS).
//   DataField / EngineBadge / SettingsControl — inchangés.

const { useRef: useRefP } = React;

// ── tiny markdown: **bold**, lines, "- " bullets ─────────────────────────
function softInline(text, kp) {
  const parts = text.split(/(\*\*[^*]+\*\*)/g).filter(Boolean);
  return parts.map((p, i) =>
    p.startsWith('**')
      ? <strong key={kp + 'b' + i}>{p.slice(2, -2)}</strong>
      : <span key={kp + 't' + i}>{p}</span>);
}
function SoftAnswer({ text }) {
  const blocks = [];
  let list = null;
  text.split('\n').forEach((ln) => {
    const s = ln.trim();
    if (s.startsWith('- ')) { if (!list) { list = []; blocks.push({ t: 'ul', items: list }); } list.push(s.slice(2)); }
    else { list = null; if (s) blocks.push({ t: 'p', text: s }); }
  });
  return (
    <div className="ans-md">
      {blocks.map((b, i) => b.t === 'ul'
        ? <ul key={i}>{b.items.map((it, j) => <li key={j}>{softInline(it, 'l' + i + j)}</li>)}</ul>
        : <p key={i}>{softInline(b.text, 'p' + i)}</p>)}
    </div>
  );
}

// ── BOB — orchestrator body ──────────────────────────────────────────────
// reached() over bob's own phase chain
const BOB_ORDER = ['think', 'summon', 'wait', 'answer', 'done'];

function BobBody({ card, subRefs }) {
  const { task, phase, frac } = card;
  const reached = (key) => BOB_ORDER.indexOf(key) <= BOB_ORDER.indexOf(phase);

  const thinkStreaming = phase === 'think' && frac < 1;
  const thinkText = phase === 'think' ? takeChars(task.think, frac) : task.think;

  const showSummon = reached('summon');
  const allIn = card.returned === subRefs.length && subRefs.length > 0;
  const summonMeta = !showSummon ? ''
    : (allIn ? `${subRefs.length} rendus` : `${card.returned}/${subRefs.length} rendus`);

  const ansStreaming = phase === 'answer' && frac < 1;
  const ansText = phase === 'answer' ? takeChars(task.answer, frac) : (reached('done') ? task.answer : '');

  return (
    <React.Fragment>
      <div className="task-prompt">{task.prompt}</div>

      <div className="task-scroll">
        {/* RÉFLEXION */}
        <div className={`task-step ${phase === 'think' ? 'is-active' : 'is-done'}`}>
          <div className="step-key">
            <span className="step-pip" />
            <span className="step-label">Réflexion</span>
            <span className="step-meta">{thinkStreaming ? 'en cours…' : 'monologue'}</span>
          </div>
          <p className="think-body">{thinkText}{thinkStreaming && <span className="caret" />}</p>
        </div>

        {/* TÂCHES INVOQUÉES — références vivantes aux sous-tâches créées */}
        {showSummon && (
          <div className={`task-step ${phase === 'summon' || phase === 'wait' ? 'is-active' : 'is-done'}`}>
            <div className="step-key">
              <span className="step-pip" />
              <span className="step-label">Tâches en arrière-plan</span>
              <span className="step-meta">{summonMeta}</span>
            </div>
            <div className="invoked">
              {subRefs.map((s) => (
                <div key={s.task.id} className={`invoked-row ${s.done ? 'is-done' : s.visible ? 'is-live' : 'is-pending'}`}>
                  <span className="invoked-glyph">◇</span>
                  <span className="invoked-name">{s.task.name}</span>
                  <span className="invoked-tool">{s.task.tool.name}</span>
                  <span className="invoked-stat">
                    {s.done
                      ? <><span className="bgtask-chk">✓</span>rendu</>
                      : <><span className="invoked-dot" />{SUB_STAT[s.phase] || '…'}</>}
                  </span>
                </div>
              ))}
            </div>
          </div>
        )}

        {/* RÉPONSE — synthèse de Bob */}
        {reached('answer') && ansText && (
          <div className={`task-step ${phase === 'answer' ? 'is-active' : 'is-done'}`}>
            <div className="step-key">
              <span className="step-pip" />
              <span className="step-label">Réponse</span>
            </div>
            <div className="answer-box">
              <SoftAnswer text={ansText} />
              {ansStreaming && <span className="caret caret-ink" />}
            </div>
          </div>
        )}

        {phase === 'done' && (
          <div className="task-perf">
            <span><b>47</b> tok/s</span><span><b>0.7s</b> ttft</span><span><b>9.4k</b> ctx</span>
          </div>
        )}
      </div>
    </React.Fragment>
  );
}

// ── SUB-TASK body — réflexion → outil → rendu ────────────────────────────
const SUB_ORDER = ['spawn', 'think', 'tool', 'result', 'done'];

function SubBody({ card }) {
  const { task, phase, frac, done } = card;
  const reached = (key) => SUB_ORDER.indexOf(key) <= SUB_ORDER.indexOf(phase);

  const thinkStreaming = phase === 'think' && frac < 1;
  const thinkText = phase === 'think' ? takeChars(task.think, frac) : (reached('tool') ? task.think : '');

  const toolShown = reached('tool');
  const showRes = reached('result');
  const showArgs = toolShown && (showRes || (phase === 'tool' && frac > 0.15));

  const renduStreaming = phase === 'result' && frac < 1;
  const renduText = phase === 'result' ? takeChars(task.answer, frac) : (done ? task.answer : '');

  return (
    <div className="sub-body">
      {reached('think') && thinkText && (
        <p className="sub-think">{thinkText}{thinkStreaming && <span className="caret" />}</p>
      )}

      {toolShown && (
        <div className={`sub-tool ${showRes ? 'is-ok' : 'is-run'}`}>
          <div className="sub-tool-line">
            <span className="sub-tool-mark" />
            <span className="sub-tool-name">{task.tool.name}</span>
            {!showRes && <span className="sub-tool-run">appel…</span>}
          </div>
          {showArgs && <div className="sub-tool-args">{task.tool.args}</div>}
          {showRes && <div className="sub-tool-res"><span className="bgtask-chk">✓</span>{task.tool.result}</div>}
        </div>
      )}

      {(showRes || done) && renduText && (
        <div className="sub-ret">
          <span className="sub-ret-arrow">↳</span>
          <span>{renduText}{renduStreaming && <span className="caret" />}</span>
        </div>
      )}
    </div>
  );
}

// ── DECK CARD — places one card in the pile by rank, draws the right chrome ─
function jitterFor(id) {
  let h = 0;
  for (let i = 0; i < id.length; i++) h = (h * 31 + id.charCodeAt(i)) & 0xffff;
  return ((h % 90) / 10) - 4.5;   // -4.5°..+4.5°
}

function DeckCard({ card, subRefs, total, promote, pinned }) {
  const isBob = card.kind === 'bob';
  const id = isBob ? 'bob' : card.task.id;
  const rank = card.rank;
  const front = rank === 0;
  const behind = (total || 1) - 1 - rank;

  const tx = rank * 9;
  const ty = -rank * 50;
  const tz = -rank * 18;
  const sc = 1 - rank * 0.05;
  const rot = front ? 0 : jitterFor(id);

  const style = {
    transform: `translate3d(${tx}px, ${ty}px, ${tz}px) rotateZ(${rot}deg) scale(${sc})`,
    zIndex: 200 - rank,
    opacity: front ? 1 : Math.max(0.42, 1 - rank * 0.15),
  };

  const stat = isBob ? BOB_STAT[card.phase] : SUB_STAT[card.phase];
  const working = isBob ? card.phase !== 'done' : !card.done;

  return (
    <div
      className={`stack-card ${isBob ? 'is-bob' : 'is-sub'} ${front ? 'is-front' : 'is-back'} ${pinned ? 'is-pinned' : ''}`}
      style={style}
      onClick={front ? undefined : () => promote(id)}
      role={front ? undefined : 'button'}
      title={front ? undefined : `Mettre « ${isBob ? 'BOB' : card.task.name} » au premier plan`}
    >
      <div className={`panel ${isBob ? 'bob-panel' : 'sub-panel'}`}>
        <div className="panel-head">
          {isBob
            ? <span className="bob-orb" data-live={working} />
            : <span className="sub-glyph">◇</span>}
          <span className="panel-title">{isBob ? 'BOB' : card.task.name}</span>
          {isBob
            ? <span className="bob-role">fil de conscience</span>
            : <span className="sub-by">par&nbsp;BOB</span>}
          {pinned && <span className="panel-pin">épinglé</span>}
          {isBob && front && behind > 0 && <span className="panel-stackn">+{behind} tâches</span>}
          <span className="panel-phase">{stat}</span>
        </div>

        {isBob
          ? <BobBody card={card} subRefs={subRefs} />
          : (
            <React.Fragment>
              {!front && <div className="sub-spec-line">{card.task.spec} · {card.task.tool.name}</div>}
              <SubBody card={card} />
            </React.Fragment>
          )}
      </div>
    </div>
  );
}

// ── THREAD STACK ─────────────────────────────────────────────────────────
function ThreadStack({ thread }) {
  const { ordered, promote, pinnedId } = thread;
  // live state of each sub-task, in declared order, for Bob's invoked list
  const byId = {};
  ordered.forEach((c) => { if (c.kind === 'sub') byId[c.task.id] = c; });
  const subRefs = SUBTASKS.map((s) => byId[s.id] || { task: s, phase: 'dormant', done: false, visible: false });

  // keep DOM order stable (bob first, then subs in declared order) so cards
  // stay the same elements across reshuffles — only the transform glides.
  const domOrder = [
    ordered.find((c) => c.kind === 'bob'),
    ...SUBTASKS.map((s) => ordered.find((c) => c.kind === 'sub' && c.task.id === s.id)),
  ].filter(Boolean);

  return (
    <div className="task-stack">
      {domOrder.map((card) => {
        const id = card.kind === 'bob' ? 'bob' : card.task.id;
        return (
          <DeckCard
            key={id}
            card={card}
            subRefs={subRefs}
            total={ordered.length}
            promote={promote}
            pinned={pinnedId === id}
          />
        );
      })}
    </div>
  );
}

// ── DATA FIELD ─────────────────────────────────────────────────────────
const ICONS = {
  mail: <svg viewBox="0 0 16 16"><rect x="1.5" y="3.5" width="13" height="9" rx="1.2" /><path d="M2 4l6 4.5L14 4" /></svg>,
  doc: <svg viewBox="0 0 16 16"><path d="M4 1.5h5l3 3v10h-8z" /><path d="M9 1.5v3h3" /><path d="M5.6 8h5M5.6 10.4h5" /></svg>,
  video: <svg viewBox="0 0 16 16"><rect x="1.5" y="3.5" width="9" height="9" rx="1.2" /><path d="M10.5 6.5l4-2v7l-4-2z" /></svg>,
  contact: <svg viewBox="0 0 16 16"><circle cx="8" cy="5.5" r="2.6" /><path d="M3 13.5c0-2.8 2.2-4.4 5-4.4s5 1.6 5 4.4" /></svg>,
  action: <svg viewBox="0 0 16 16"><path d="M8.5 1.5L3 9h4l-.5 5.5L13 7H9z" /></svg>,
};

const N_SLOTS = MAX_DATA;
const DISSOLVE = 1.3;
const MEM_LIFE = 11;            // secondes visibles avant de céder la place
const MEM_TOP = 14, MEM_STEP = 80;

function DataField({ t, layout, onOpen }) {
  const slots = useRefP(null);
  if (!slots.current) {
    slots.current = {
      arr: Array.from({ length: N_SLOTS }, (_, i) => ({
        poolIdx: i % DATA_POOL.length,
        born: t - (N_SLOTS - 1 - i) * (MEM_LIFE / N_SLOTS),
        seq: i,
      })),
      nextPool: N_SLOTS % DATA_POOL.length,
      nextSeq: N_SLOTS,
    };
  }
  const st = slots.current;

  st.arr.forEach((s) => {
    if (t - s.born > MEM_LIFE + DISSOLVE) {
      s.poolIdx = st.nextPool; st.nextPool = (st.nextPool + 1) % DATA_POOL.length;
      s.born = t; s.seq = st.nextSeq++;
    }
  });

  const ordered = st.arr
    .map((s) => ({ s, item: DATA_POOL[s.poolIdx], age: Math.max(0, t - s.born) }))
    .sort((a, b) => b.s.born - a.s.born);

  const liveCount = ordered.filter((r) => r.age <= MEM_LIFE).length;

  return (
    <div className="panel data-panel mem-dock">
      <div className="panel-head">
        <span className="panel-dot" data-live="true" />
        <span className="panel-title">DONNÉES GÉNÉRÉES</span>
        <span className="panel-phase">{String(liveCount).padStart(2, '0')} / {String(MAX_DATA).padStart(2, '0')} max</span>
      </div>
      <div className="data-field mem-field">
        <div className="mem-rail" />
        {ordered.map((row, rank) => {
          const { s, item, age } = row;
          const dissolving = age > MEM_LIFE;
          const dF = dissolving ? Math.min(1, (age - MEM_LIFE) / DISSOLVE) : 0;
          const fresh = age < 0.55;
          const y = MEM_TOP + rank * MEM_STEP;
          return (
            <div
              key={s.seq}
              className={`mem-card a-${item.type} ${fresh ? 'is-fresh' : ''} ${dissolving ? 'is-leaving' : ''}`}
              style={{ top: y, opacity: 1 - dF, zIndex: 100 - rank, transform: `translateX(${dF * 16}px)` }}
              onClick={onOpen ? () => onOpen(item) : undefined}
              role={onOpen ? 'button' : undefined}
              title={onOpen ? `Ouvrir « ${item.title} »` : undefined}
            >
              <span className="mem-tick" />
              <div className="art-glow" />
              <div className="art-row">
                <span className="art-icon">{ICONS[item.type]}</span>
                <span className="art-text">
                  <span className="art-title">{item.title}</span>
                  <span className="art-sub">{item.sub}</span>
                </span>
              </div>
              <span className="art-type">{DATA_TYPE_LABEL[item.type]}</span>
              <span className="mem-rank">{String(rank + 1).padStart(2, '0')}</span>
            </div>
          );
        })}
      </div>
    </div>
  );
}

// ── ENGINE BADGE ─────────────────────────────────────────────────────────
function EngineBadge() {
  return (
    <div className="engine-badge">
      <span className="eng-cap">MOTEUR LLM</span>
      <span className="eng-dot" />
      <span className="eng-name">{ENGINE.name}</span>
      <span className="eng-spec">{ENGINE.spec}</span>
    </div>
  );
}

// ── SETTINGS (top-right) ──────────────────────────────────────────────────
const LM_PRESETS = [
  { label: 'localhost', url: 'http://localhost:1234' },
  { label: 'studio.local', url: 'http://studio.local:1234' },
  { label: '192.168.1.20', url: 'http://192.168.1.20:1234' },
];

const LM_MODELS = [
  { id: 'qwen2.5-coder-32b', name: 'Qwen2.5 Coder',      params: '32B', quant: 'Q4_K_M', ram: '18.5 GB' },
  { id: 'llama-3.3-70b',     name: 'Llama 3.3 Instruct', params: '70B', quant: 'Q3_K_L', ram: '37.1 GB' },
  { id: 'mistral-small-3',   name: 'Mistral Small 3',    params: '24B', quant: 'Q4_K_M', ram: '14.3 GB' },
  { id: 'gemma-2-27b',       name: 'Gemma 2',            params: '27B', quant: 'Q5_K_M', ram: '19.4 GB' },
  { id: 'deepseek-r1-14b',   name: 'DeepSeek R1 Distill',params: '14B', quant: 'Q4_K_M', ram: '8.9 GB'  },
  { id: 'phi-4',             name: 'Phi-4',              params: '14B', quant: 'Q4_K_M', ram: '8.4 GB'  },
];

function lmUrlReachable(raw) {
  const s = (raw || '').trim().replace(/^https?:\/\//i, '');
  return /^[\w.-]+(:\d{2,5})?(\/.*)?$/.test(s) && s.length > 3;
}

const SettingsControl = React.memo(function SettingsControl({ open, setOpen, provider, setProvider, lmUrl, setLmUrl, lmModel, setLmModel }) {
  const isLM = provider === 'lmstudio';
  const reachable = lmUrlReachable(lmUrl);
  const urlBody = (lmUrl || '').replace(/^https?:\/\//i, '');
  const onUrlChange = (v) => setLmUrl('http://' + v.replace(/^https?:\/\//i, ''));

  return (
    <div className="settings-zone">
      <button
        className={`settings-btn ${open ? 'is-open' : ''}`}
        onClick={() => setOpen((v) => !v)}
        aria-label="Réglages"
        aria-expanded={open}
      >
        <svg viewBox="0 0 24 24" className="settings-gear">
          <circle cx="12" cy="12" r="3.2" />
          <path d="M12 2.5v3M12 18.5v3M21.5 12h-3M5.5 12h-3M18.7 5.3l-2.1 2.1M7.4 16.6l-2.1 2.1M18.7 18.7l-2.1-2.1M7.4 7.4L5.3 5.3" />
        </svg>
        <span className="settings-cap">Réglages</span>
      </button>

      {open && (
        <>
          <div className="settings-scrim" onClick={() => setOpen(false)} />
          <div className="settings-panel" role="dialog" aria-label="Réglages">
            <div className="settings-head">
              <span className="settings-title">RÉGLAGES</span>
              <button className="settings-close" onClick={() => setOpen(false)} aria-label="Fermer">✕</button>
            </div>

            <div className="settings-section">
              <div className="settings-label">MOTEUR LLM</div>

              <div className="set-seg" role="radiogroup" aria-label="Moteur LLM">
                <button
                  type="button" role="radio" aria-checked={!isLM}
                  className={`set-seg-btn ${!isLM ? 'on' : ''}`}
                  onClick={() => setProvider('claude')}
                >
                  <span className="set-seg-glyph">&gt;_</span>Claude CLI
                </button>
                <button
                  type="button" role="radio" aria-checked={isLM}
                  className={`set-seg-btn ${isLM ? 'on' : ''}`}
                  onClick={() => setProvider('lmstudio')}
                >
                  <span className="set-seg-glyph">▦</span>LM Studio
                </button>
              </div>

              {!isLM ? (
                <div className="set-detail" key="claude">
                  <div className="set-status is-ok">
                    <span className="set-dot is-ok" />
                    <span className="eng-name">claude-sonnet-4.5</span>
                    <span className="set-status-state">CLI · connecté</span>
                  </div>
                  <div className="set-field-hint"><b>Pont CLI local — modèle fixe, aucune URL requise.</b></div>
                </div>
              ) : (
                <div className="set-detail" key="lm">
                  <div className="set-field">
                    <div className="settings-label">SERVEUR LM STUDIO</div>
                    <div className="set-field-row">
                      <span className="set-field-proto">http://</span>
                      <input
                        className="set-input" type="text" inputMode="url" spellCheck="false"
                        value={urlBody}
                        placeholder="localhost:1234"
                        onChange={(e) => onUrlChange(e.target.value)}
                        aria-label="URL du serveur LM Studio"
                      />
                    </div>
                    <div className="set-presets">
                      {LM_PRESETS.map((p) => (
                        <button
                          key={p.url} type="button"
                          className={`set-preset ${lmUrl === p.url ? 'on' : ''}`}
                          onClick={() => setLmUrl(p.url)}
                        >{p.label}</button>
                      ))}
                    </div>
                  </div>
                  <div className={`set-status ${reachable ? 'is-ok' : 'is-off'}`}>
                    <span className={`set-dot ${reachable ? 'is-ok' : 'is-off'}`} />
                    <span className="eng-name">{reachable ? 'serveur joignable' : 'serveur introuvable'}</span>
                    <span className="set-status-state">{reachable ? 'connecté' : 'hors ligne'}</span>
                  </div>

                  <div className="set-field">
                    <div className="set-models-head">
                      <span className="settings-label">MODÈLE LOCAL</span>
                      <span className="set-models-count">{LM_MODELS.length} chargés</span>
                    </div>
                    <div className="set-models" role="listbox" aria-label="Modèle local">
                      {LM_MODELS.map((mm) => {
                        const on = mm.id === lmModel;
                        return (
                          <button
                            key={mm.id} type="button" role="option" aria-selected={on}
                            className={`set-model ${on ? 'on' : ''}`}
                            onClick={() => setLmModel(mm.id)}
                          >
                            <span className="set-model-mark">{on ? '◆' : '◇'}</span>
                            <span className="set-model-name">{mm.name}</span>
                            <span className="set-model-spec">{mm.params} · {mm.quant}</span>
                            <span className="set-model-ram">{mm.ram}</span>
                          </button>
                        );
                      })}
                    </div>
                  </div>
                </div>
              )}
            </div>
          </div>
        </>
      )}
    </div>
  );
});

Object.assign(window, { BobBody, SubBody, DeckCard, ThreadStack, DataField, EngineBadge, SettingsControl });
