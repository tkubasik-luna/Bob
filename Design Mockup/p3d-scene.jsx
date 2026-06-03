// p3d-scene.jsx — composes one piste: stage + core + task + data + engine.
// Layout & camera are driven by the per-piste config (CSS classes do the
// spatial work; this just places the slots and feeds the core its energy).

function Scene() {
  const cfg = PISTE;
  const t = useClock(14);
  const thread = useThread(t);
  const front = thread.front;
  const energy = thread.energy;
  // the orb breathes to whichever card is live; map both kinds to a core mood
  const phaseKey = front.kind === 'bob'
    ? ({ think: 'think', summon: 'delegate', wait: 'delegate', answer: 'answer', done: 'done' }[front.phase] || 'think')
    : ({ spawn: 'delegate', think: 'think', tool: 'delegate', result: 'answer', done: 'done' }[front.phase] || 'think');

  const stateWord = front.kind === 'bob'
    ? (BOB_STAT[front.phase] || 'réfléchit')
    : 'délègue';

  const [settingsOpen, setSettingsOpen] = React.useState(false);

  // donnée générée ouverte en plein écran (clic sur une carte)
  const [surface, setSurface] = React.useState(null);
  // onClose stable → DataOverlay (memo) ne se re-rend pas à chaque tick d'horloge
  const closeSurface = React.useCallback(() => setSurface(null), []);

  // LLM engine choice — persists across reloads
  const [provider, setProvider] = React.useState(() => {
    try { return localStorage.getItem('bob.provider') || 'claude'; } catch (e) { return 'claude'; }
  });
  const [lmUrl, setLmUrl] = React.useState(() => {
    try { return localStorage.getItem('bob.lmUrl') || 'http://localhost:1234'; } catch (e) { return 'http://localhost:1234'; }
  });
  const [lmModel, setLmModel] = React.useState(() => {
    try { return localStorage.getItem('bob.lmModel') || 'qwen2.5-coder-32b'; } catch (e) { return 'qwen2.5-coder-32b'; }
  });
  React.useEffect(() => { try { localStorage.setItem('bob.provider', provider); } catch (e) {} }, [provider]);
  React.useEffect(() => { try { localStorage.setItem('bob.lmUrl', lmUrl); } catch (e) {} }, [lmUrl]);
  React.useEffect(() => { try { localStorage.setItem('bob.lmModel', lmModel); } catch (e) {} }, [lmModel]);

  return (
    <div
      className={`piste layout-${cfg.layout} cam-${cfg.camera} panel-${cfg.panel} ${surface ? 'has-surface' : ''}`}
      style={cfg.vars}
    >
      <div className="piste-bg" />
      <div className="piste-grain" />

      {/* identity — top-left, soft */}
      <div className="piste-id">
        <div className="id-mark">
          <span className="id-glyph" />
          <span className="id-name">BOB</span>
          <span className="id-state">· {stateWord}</span>
        </div>
        <div className="id-tagline">{cfg.name} — {cfg.tagline}</div>
      </div>

      {/* settings — top-right */}
      <SettingsControl
        open={settingsOpen} setOpen={setSettingsOpen}
        provider={provider} setProvider={setProvider}
        lmUrl={lmUrl} setLmUrl={setLmUrl}
        lmModel={lmModel} setLmModel={setLmModel}
      />

      {/* the 3D stage */}
      <div className="stage-3d">
        <div className="stage-cam">
          <div className="slot-core">
            <Core variant={cfg.core} energy={energy} phaseKey={phaseKey}
              accent={cfg.vars['--accent']} accent2={cfg.vars['--accent2']} />
            <div className="core-label">CORE · conscience</div>
          </div>

          <div className="slot-task">
            <ThreadStack thread={thread} />
          </div>

          <div className="slot-data">
            <DataField t={t} layout={cfg.layout} onOpen={setSurface} />
          </div>
        </div>
      </div>

      {surface && <DataOverlay item={surface} onClose={closeSurface} />}
    </div>
  );
}

window.Scene = Scene;
