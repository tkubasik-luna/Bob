// data-layouts.jsx — 4 explorations de disposition pour « Données générées ».
//   A · Orbite gravitaire — les artefacts gravitent, reliés au core (récence = proximité)
//   B · Fil temporel      — colonne vertébrale issue du core, cartes enfilées dans le temps
//   C · Mémoire vive      — plateau net de 5 emplacements, tenus en mémoire active
//   D · Constellation     — cartes-nœuds reliées entre elles + au core (graphe)
// Reuse DATA_POOL / DATA_TYPE_LABEL / useClock from p3d-content.jsx.

const { useRef: useRefDL } = React;

// ── icons (mirrors p3d-panels) ───────────────────────────────────────────
const DL_ICONS = {
  mail:    <svg viewBox="0 0 16 16"><rect x="1.5" y="3.5" width="13" height="9" rx="1.2" /><path d="M2 4l6 4.5L14 4" /></svg>,
  doc:     <svg viewBox="0 0 16 16"><path d="M4 1.5h5l3 3v10h-8z" /><path d="M9 1.5v3h3" /><path d="M5.6 8h5M5.6 10.4h5" /></svg>,
  video:   <svg viewBox="0 0 16 16"><rect x="1.5" y="3.5" width="9" height="9" rx="1.2" /><path d="M10.5 6.5l4-2v7l-4-2z" /></svg>,
  contact: <svg viewBox="0 0 16 16"><circle cx="8" cy="5.5" r="2.6" /><path d="M3 13.5c0-2.8 2.2-4.4 5-4.4s5 1.6 5 4.4" /></svg>,
  action:  <svg viewBox="0 0 16 16"><path d="M8.5 1.5L3 9h4l-.5 5.5L13 7H9z" /></svg>,
};

// ── shared pieces ─────────────────────────────────────────────────────────
function DLCard({ item, compact, style, className }) {
  return (
    <div className={`dl-card ${compact ? 'is-compact' : ''} ${className || ''}`} style={style}>
      <div className="dl-card-glow" />
      <div className="dl-row">
        <span className={`dl-icon t-${item.type}`}>{DL_ICONS[item.type]}</span>
        <span className="dl-text">
          <span className="dl-cardtitle">{item.title}</span>
          <span className="dl-sub">{item.sub}</span>
        </span>
      </div>
      <span className="dl-type">{DATA_TYPE_LABEL[item.type]}</span>
    </div>
  );
}

function DLCore({ x, y, size, cap }) {
  return (
    <div className="dl-core" style={{ left: x, top: y, width: size, height: size, transform: 'translate(-50%,-50%)' }}>
      <div className="dl-core-halo" />
      <div className="dl-core-body"><div className="dl-core-sheen" /><div className="dl-core-light" /></div>
      {cap && <div className="dl-core-cap" style={{ top: 'calc(100% + 9px)' }}>{cap}</div>}
    </div>
  );
}

function DLHead({ metaphor }) {
  return (
    <div className="dl-head">
      <span className="dl-dot" />
      <span className="dl-title">DONNÉES GÉNÉRÉES</span>
      <span className="dl-metaphor">· {metaphor}</span>
      <span className="dl-count">05 / 05 max</span>
    </div>
  );
}

// SVG link layer — draws faint connectors. links: [{x1,y1,x2,y2,op,w,dot}]
function DLLinks({ links }) {
  return (
    <svg className="dl-links">
      {links.map((l, i) => (
        <React.Fragment key={i}>
          <line className="dl-link" x1={l.x1} y1={l.y1} x2={l.x2} y2={l.y2}
            strokeWidth={l.w || 1} strokeOpacity={l.op} strokeDasharray={l.dash || 'none'} />
          {l.dot && <circle className="dl-link-dot" cx={l.x2} cy={l.y2} r="2.4" fillOpacity={Math.min(1, l.op + 0.2)} />}
        </React.Fragment>
      ))}
    </svg>
  );
}

// rolling working-memory feed: n slots, newest-first, staggered births + recycle
function useFeed(t, n, life, step) {
  const ref = useRefDL(null);
  if (!ref.current) {
    const slots = Array.from({ length: n }, (_, i) => ({
      poolIdx: i % DATA_POOL.length, born: t - (n - 1 - i) * step, seq: i,
    }));
    ref.current = { slots, nextPool: n % DATA_POOL.length, nextSeq: n };
  }
  const st = ref.current;
  st.slots.forEach((s) => {
    if (t - s.born > life) {
      s.poolIdx = st.nextPool; st.nextPool = (st.nextPool + 1) % DATA_POOL.length;
      s.born = t; s.seq = st.nextSeq++;
    }
  });
  return st.slots
    .map((s) => ({ s, item: DATA_POOL[s.poolIdx], age: t - s.born }))
    .sort((a, b) => b.s.born - a.s.born)          // newest first
    .map((row, rank) => ({ ...row, rank }));
}

const W = 680, H = 560;   // artboard content box

// ════════════════════════════════════════════════════════════════════════
// A · ORBITE GRAVITAIRE
// ════════════════════════════════════════════════════════════════════════
function LayoutOrbit() {
  const t = useClock(16);
  const items = DATA_POOL.slice(0, 5);
  const cx = W / 2, cy = H * 0.55, rx = 236, ry = 118;

  const placed = items.map((item, i) => {
    const ang = (i / items.length) * Math.PI * 2 + t * 0.16;
    const d = (Math.sin(ang) + 1) / 2;               // 0 back · 1 front
    const x = cx + Math.cos(ang) * rx;
    const y = cy + Math.sin(ang) * ry;
    return { item, x, y, d, scale: 0.76 + d * 0.28, op: 0.5 + d * 0.5, z: 10 + Math.round(d * 24) };
  });

  const links = placed.map((p) => ({
    x1: cx, y1: cy, x2: p.x, y2: p.y, op: 0.1 + p.d * 0.22, w: 0.9 + p.d * 0.6, dot: true,
  }));

  return (
    <div className="dl-scene dl-orbit">
      <DLHead metaphor="orbite gravitaire" />
      <div className="dl-ellipse" style={{ left: cx, top: cy, width: rx * 2, height: ry * 2, transform: 'translate(-50%,-50%)' }} />
      <DLLinks links={links} />
      <DLCore x={cx} y={cy} size={132} cap="core" />
      {placed.map((p, i) => (
        <DLCard key={i} item={p.item}
          style={{ left: p.x, top: p.y, transform: `translate(-50%,-50%) scale(${p.scale})`, opacity: p.op, zIndex: p.z }} />
      ))}
    </div>
  );
}

// ════════════════════════════════════════════════════════════════════════
// B · FIL TEMPOREL
// ════════════════════════════════════════════════════════════════════════
function LayoutTimeline() {
  const t = useClock(16);
  const feed = useFeed(t, 5, 9.5, 1.9);
  const spineX = W / 2, top = 132, step = 80, life = 9.5;

  const links = feed.map((r) => {
    const y = top + r.rank * step;
    const side = r.rank % 2 === 0 ? -1 : 1;
    const cardX = spineX + side * 116;
    return { x1: spineX, y1: y, x2: cardX, y2: y, op: 0.16 + (1 - r.rank / 5) * 0.2, w: 1, dot: true };
  });

  return (
    <div className="dl-scene dl-timeline">
      <DLHead metaphor="fil temporel" />
      <div className="dl-spine" style={{ left: spineX - 1, top: 118, height: top + 4.2 * step - 100 }} />
      <div className="dl-tl-axis" style={{ left: spineX + 14, top: 110 }}>à l'instant</div>
      <div className="dl-tl-axis" style={{ left: spineX + 14, top: top + 4 * step + 14 }}>plus ancien</div>
      <DLLinks links={links} />
      <DLCore x={spineX} y={74} size={92} cap="core" />
      {feed.map((r) => {
        const y = top + r.rank * step;
        const side = r.rank % 2 === 0 ? -1 : 1;
        const cardX = spineX + side * 116;
        const dying = r.age > life - 1.4;
        const fresh = r.age < 0.6;
        const op = (dying ? Math.max(0.18, (life - r.age) / 1.4) : 1) * (0.7 + (1 - r.rank / 6) * 0.3);
        return (
          <DLCard key={r.s.seq} item={r.item} className={fresh ? 'is-fresh' : ''}
            style={{
              left: cardX, top: y,
              transform: `translate(${side < 0 ? '-100%' : '0'}, -50%) scale(${dying ? 0.94 : 1})`,
              opacity: op, zIndex: 10 + (5 - r.rank),
            }} />
        );
      })}
    </div>
  );
}

// ════════════════════════════════════════════════════════════════════════
// C · MÉMOIRE VIVE (dock)
// ════════════════════════════════════════════════════════════════════════
function LayoutMemory() {
  const t = useClock(16);
  const feed = useFeed(t, 5, 10.5, 2.1);
  const coreX = 96, coreY = H * 0.5;
  const stackX = 232, top = 104, step = 78, cardW = 330, life = 10.5;

  const links = feed.map((r) => {
    const y = top + r.rank * step + 31;
    return { x1: coreX + 44, y1: coreY, x2: stackX, y2: y, op: 0.12 + (1 - r.rank / 6) * 0.16, w: 1 };
  });

  return (
    <div className="dl-scene dl-memory">
      <DLHead metaphor="mémoire vive" />
      <DLLinks links={links} />
      <DLCore x={coreX} y={coreY} size={104} cap="core" />
      <div style={{ position: 'absolute', left: stackX - 16, top: top - 4, bottom: 44, width: 2, background: 'linear-gradient(180deg, color-mix(in oklab, var(--accent) 45%, transparent), color-mix(in oklab, var(--accent) 6%, transparent))', zIndex: 3 }} />
      {feed.map((r) => {
        const y = top + r.rank * step;
        const dying = r.age > life - 1.4;
        const fresh = r.age < 0.6;
        const op = dying ? Math.max(0.2, (life - r.age) / 1.4) : 1;
        return (
          <div key={r.s.seq} style={{ position: 'absolute', left: stackX, top: y, opacity: op, zIndex: 10 + (5 - r.rank) }}>
            <DLCard item={r.item} className={fresh ? 'is-fresh' : ''}
              style={{ position: 'relative', left: 0, top: 0, width: cardW, transform: `translateX(${dying ? 10 : 0}px)` }} />
            <span className="dl-mem-tick" style={{ left: -16 }} />
            <span className="dl-slot-rank" style={{ position: 'absolute', right: 12, top: 12 }}>{String(r.rank + 1).padStart(2, '0')}</span>
          </div>
        );
      })}
    </div>
  );
}

// ════════════════════════════════════════════════════════════════════════
// D · CONSTELLATION RELIÉE
// ════════════════════════════════════════════════════════════════════════
const CONST_BASE = [
  { a: -0.55, r: 188 }, { a: 0.5, r: 210 }, { a: 1.5, r: 176 },
  { a: 2.55, r: 206 }, { a: -1.7, r: 192 },
];
const CONST_EDGES = [[0, 1], [1, 2], [2, 3], [3, 4], [4, 0], [0, 2]];

function LayoutConstellation() {
  const t = useClock(16);
  const items = DATA_POOL.slice(5, 10);
  const cx = W / 2, cy = H * 0.53;

  const nodes = items.map((item, i) => {
    const b = CONST_BASE[i];
    const ang = b.a + Math.sin(t * 0.2 + i) * 0.05;
    const r = b.r + Math.sin(t * 0.32 + i * 1.3) * 11;
    return { item, x: cx + Math.cos(ang) * r * 1.22, y: cy + Math.sin(ang) * r };
  });

  const pulse = (k) => 0.14 + (Math.sin(t * 0.9 + k) + 1) / 2 * 0.16;
  const links = [
    ...nodes.map((n, i) => ({ x1: cx, y1: cy, x2: n.x, y2: n.y, op: 0.1 + pulse(i) * 0.5, w: 1, dot: true })),
    ...CONST_EDGES.map(([a, b], i) => ({
      x1: nodes[a].x, y1: nodes[a].y, x2: nodes[b].x, y2: nodes[b].y,
      op: pulse(i + 7) * 0.7, w: 0.8, dash: '2 5',
    })),
  ];

  return (
    <div className="dl-scene dl-constellation">
      <DLHead metaphor="constellation reliée" />
      <DLLinks links={links} />
      <DLCore x={cx} y={cy} size={100} cap="core" />
      {nodes.map((n, i) => (
        <DLCard key={i} item={n.item} compact
          style={{ left: n.x, top: n.y, transform: 'translate(-50%,-50%)', zIndex: 12 }} />
      ))}
    </div>
  );
}

Object.assign(window, {
  DLCard, DLCore, DLHead, DLLinks, useFeed,
  LayoutOrbit, LayoutTimeline, LayoutMemory, LayoutConstellation,
});
