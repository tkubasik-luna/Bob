// p3d-overlay.jsx — plein écran "donnée générée".
// Clic sur une carte DONNÉES GÉNÉRÉES → surface projetée par Bob, dans le même
// vocabulaire HUD que l'écran Sphere Lab (cadre d'angle, faisceau, en-tête mono,
// pied d'actions), mais teinté nacre.

const { useState: useStateOv, useEffect: useEffectOv } = React;

const OV_TYPE_LABEL = {
  mail: 'COURRIEL', doc: 'DOCUMENT', video: 'VIDÉO', contact: 'CONTACT', action: 'ACTION',
};
// chip + amorce de transcription par type
const OV_CHIP = { mail: 'BOÎTE', doc: 'FICHIER', video: 'FLUX', contact: 'PERSONNE', action: 'COMMANDE' };

function refId(item) {
  let h = 0; const s = (item.title || '') + item.type;
  for (let i = 0; i < s.length; i++) h = (h * 31 + s.charCodeAt(i)) & 0xffff;
  return (item.type.slice(0, 3).toUpperCase()) + '-' + String(1000 + (h % 9000));
}

// ─────────────────────────────────────────────────────────────────────────
// SHELL
// ─────────────────────────────────────────────────────────────────────────
function DataOverlay({ item, onClose }) {
  useEffectOv(() => {
    const onKey = (e) => { if (e.key === 'Escape') onClose(); };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, [onClose]);

  if (!item) return null;
  const Body = OV_BODIES[item.type] || DocSurface;
  const isAction = item.type === 'action';

  return (
    <div className="ov-stage" onClick={onClose}>
      <div className="ov-scrim" />
      <div className="ov-beam" />
      <div
        className={`ov-card ov-kind-${item.type}`}
        role="dialog" aria-label={item.title}
        onClick={(e) => e.stopPropagation()}
      >
        <span className="ov-corner tl" />
        <span className="ov-corner tr" />
        <span className="ov-corner bl" />
        <span className="ov-corner br" />

        <header className="ov-header">
          <div className="ov-header-left">
            <span className="ov-source-tag">BOB · GÉNÉRÉ</span>
            <span className="ov-divider">/</span>
            <span className="ov-type-chip">{OV_CHIP[item.type]}</span>
          </div>
          <div className="ov-header-right">
            <span className="ov-id-tag">RÉF · {refId(item)}</span>
            <button className="ov-close" onClick={onClose} aria-label="fermer">
              <span className="ov-close-glyph">✕</span>
            </button>
          </div>
        </header>

        <div className="ov-body">
          <Body item={item} detail={item.detail || {}} />
        </div>

        <footer className="ov-footer">
          {isAction ? (
            <>
              <button className="ov-action ov-action-primary" onClick={onClose}>
                <span className="ov-action-key">↵</span>
                <span>{(item.detail && item.detail.confirm) || 'Valider'}</span>
              </button>
              <button className="ov-action" onClick={onClose}>
                <span className="ov-action-key">ÉCHAP</span>
                <span>ANNULER</span>
              </button>
            </>
          ) : (
            <>
              <button className="ov-action ov-action-primary">
                <span className="ov-action-key">↵</span>
                <span>LIRE À VOIX HAUTE</span>
              </button>
              <button className="ov-action">
                <span className="ov-action-key">↗</span>
                <span>OUVRIR</span>
              </button>
              <button className="ov-action" onClick={onClose}>
                <span className="ov-action-key">ÉCHAP</span>
                <span>FERMER</span>
              </button>
            </>
          )}
        </footer>
      </div>
    </div>
  );
}

// ─────────────────────────────────────────────────────────────────────────
// MAIL
// ─────────────────────────────────────────────────────────────────────────
function MailSurface({ detail }) {
  const d = detail;
  return (
    <div className="ov-email">
      <div className="ov-email-meta">
        <div className={`ov-avatar ov-avatar-grad-${d.grad || 1}`}>{d.avatar || '··'}</div>
        <div className="ov-email-meta-text">
          <div className="ov-email-from">
            <span className="ov-email-name">{d.from}</span>
            <span className="ov-email-role">{d.role}</span>
          </div>
          <div className="ov-email-addr">{d.addr}  ·  {d.time}</div>
        </div>
        {d.flag && <div className="ov-email-flags"><span className="ov-flag">{d.flag}</span></div>}
      </div>

      <h2 className="ov-email-subject">{d.subject}</h2>
      <p className="ov-email-body">{d.body}</p>

      {d.attachments && d.attachments.length > 0 && (
        <div className="ov-email-attachments">
          {d.attachments.map((a, i) => (
            <span className="ov-attach" key={i}>
              <span className="ov-attach-icon">▤</span>
              <span className="ov-attach-name">{a.name}</span>
              <span className="ov-attach-size">{a.size}</span>
            </span>
          ))}
        </div>
      )}
    </div>
  );
}

// ─────────────────────────────────────────────────────────────────────────
// VIDÉO
// ─────────────────────────────────────────────────────────────────────────
function fmtT(s) {
  const m = Math.floor(s / 60), sec = Math.floor(s % 60);
  return `${String(m).padStart(2, '0')}:${String(sec).padStart(2, '0')}`;
}
function VideoSurface({ detail }) {
  const d = detail;
  const [t, setT] = useStateOv(0.34);
  const [playing, setPlaying] = useStateOv(true);
  useEffectOv(() => {
    if (!playing) return;
    const id = setInterval(() => setT((x) => (x + 0.005) % 1), 60);
    return () => clearInterval(id);
  }, [playing]);
  const durSecs = (() => {
    const [m, s] = (d.dur || '03:04').split(':').map(Number);
    return (m * 60 + s) || 184;
  })();

  return (
    <div className="ov-video">
      <div className="ov-video-frame">
        <div className="ov-video-canvas">
          <div className="ov-video-bg" />
          {d.figure && <div className="ov-video-figure" />}
          <div className="ov-video-door" />
          <div className="ov-video-scanline" />
          <div className="ov-image-grain" />
        </div>
        <div className="ov-video-recdot"><span className="ov-rec-pulse" /> REC · {d.recAt}</div>
        <div className="ov-video-cam">{d.cam}</div>
        <button className="ov-video-play" onClick={() => setPlaying(!playing)} aria-label="lecture">
          {playing
            ? <svg width="22" height="22" viewBox="0 0 22 22"><rect x="6" y="5" width="3" height="12" fill="currentColor" /><rect x="13" y="5" width="3" height="12" fill="currentColor" /></svg>
            : <svg width="22" height="22" viewBox="0 0 22 22"><path d="M7 5 L17 11 L7 17 Z" fill="currentColor" /></svg>}
        </button>
      </div>
      <div className="ov-video-controls">
        <span className="ov-video-time">{fmtT(t * durSecs)}</span>
        <div className="ov-video-scrub">
          <div className="ov-video-scrub-track">
            <div className="ov-video-scrub-buffer" />
            <div className="ov-video-scrub-fill" style={{ width: `${t * 100}%` }} />
            <div className="ov-video-scrub-head" style={{ left: `${t * 100}%` }} />
            {[0.12, 0.28, 0.41, 0.55, 0.71, 0.86].map((m, i) =>
              <div key={i} className="ov-video-marker" style={{ left: `${m * 100}%` }} />)}
          </div>
        </div>
        <span className="ov-video-time ov-video-time-end">{d.dur}</span>
      </div>
    </div>
  );
}

// ─────────────────────────────────────────────────────────────────────────
// CONTACT
// ─────────────────────────────────────────────────────────────────────────
function ContactSurface({ item, detail }) {
  const d = detail;
  return (
    <div className="ov-contact">
      <div className="ov-contact-card">
        <div className={`ov-avatar ov-avatar-lg ov-avatar-grad-${d.grad || 1}`}>{d.avatar}</div>
        <div className="ov-contact-text">
          <div className="ov-contact-name">{item.title}</div>
          <div className="ov-contact-role">{d.role}</div>
          <div className="ov-contact-line"><span className="ov-contact-dot" />{d.status}</div>
        </div>
        <div className="ov-contact-actions">
          <button className="ov-circle-btn" title="appeler">
            <svg width="14" height="14" viewBox="0 0 14 14"><path d="M3 2.5 L5 2 L6 4.5 L4.7 5.5 Q6 8 8.5 9.3 L9.5 8 L12 9 L11.5 11 Q8 12 5 9 T3 2.5 Z" fill="none" stroke="currentColor" strokeWidth="1.2" /></svg>
          </button>
          <button className="ov-circle-btn" title="message">
            <svg width="14" height="14" viewBox="0 0 14 14"><path d="M2 3 L12 3 L12 9 L7 9 L5 11 L5 9 L2 9 Z" fill="none" stroke="currentColor" strokeWidth="1.2" /></svg>
          </button>
        </div>
      </div>

      <div className="ov-contact-grid">
        <div className="ov-contact-field"><div className="ov-contact-field-label">COURRIEL</div><div className="ov-contact-field-val">{d.email}</div></div>
        <div className="ov-contact-field"><div className="ov-contact-field-label">TÉLÉPHONE</div><div className="ov-contact-field-val">{d.phone}</div></div>
        <div className="ov-contact-field"><div className="ov-contact-field-label">LIEU</div><div className="ov-contact-field-val">{d.place}</div></div>
        <div className="ov-contact-field"><div className="ov-contact-field-label">HEURE</div><div className="ov-contact-field-val">{d.local}</div></div>
      </div>

      {d.recent && (
        <div className="ov-contact-recent">
          <div className="ov-contact-recent-head">ÉCHANGES RÉCENTS</div>
          {d.recent.map((r, i) => (
            <div className="ov-contact-recent-row" key={i}>
              <span className="ov-tag-tiny">{r.tag}</span>
              <span className="ov-contact-recent-text">{r.text}</span>
              <span className="ov-contact-recent-time">{r.when}</span>
            </div>
          ))}
        </div>
      )}
    </div>
  );
}

// ─────────────────────────────────────────────────────────────────────────
// ACTION — carte de commande à valider
// ─────────────────────────────────────────────────────────────────────────
function ActionSurface({ item, detail }) {
  const d = detail;
  return (
    <div className="ov-act">
      <div className="ov-act-head">
        <span className="ov-act-glyph">⚡</span>
        <div className="ov-act-head-text">
          <div className="ov-act-title">{item.title}</div>
          <div className="ov-act-verb">{d.verb} → {d.target}</div>
        </div>
      </div>
      <p className="ov-act-summary">{d.summary}</p>
      <div className="ov-act-fields">
        {(d.fields || []).map((f, i) => (
          <div className="ov-act-field" key={i}>
            <span className="ov-act-field-k">{f.k}</span>
            <span className="ov-act-field-v">{f.v}</span>
          </div>
        ))}
      </div>
      <div className="ov-act-note">
        <span className="ov-act-note-dot" />
        En attente de validation — Bob n'exécute rien sans ton accord.
      </div>
    </div>
  );
}

// ─────────────────────────────────────────────────────────────────────────
// DOCUMENT — aperçu de pages, ou markdown si detail.markdown
// ─────────────────────────────────────────────────────────────────────────
const NOTES_SAMPLE = `# Récit Q3 — notes

*Brouillon · dernière édition 17:42 hier par Marie*

Bob, voici les points à tisser dans le deck avant jeudi.

## Ce qui a changé depuis la v3

- **Revenu Asie** en baisse de 8,2 % — plus mou que le −5 % prévu
- EMEA tient le plan ; Amérique du Nord *légèrement* en avance
- La trésorerie couvre désormais jusqu'au **T2 2027**

## Questions ouvertes

1. Ouvrir sur l'Asie, ou sur la trésorerie ?
2. Antoine veut un seul graphe qui raconte tout
3. Nomme-t-on les trois comptes perdus ce trimestre ?

> « Les investisseurs n'ont pas besoin d'optimisme — ils veulent voir qu'on sait exactement ce qui se passe. »

## Prochaines actions

- Figer le graphe titre mercredi matin
- Envoyer le brouillon à Antoine avant jeudi midi
`;

function mdInlineP(text) {
  const out = [];
  const re = /(\*\*([^*]+)\*\*|\*([^*\n]+)\*|`([^`]+)`)/g;
  let last = 0, m, k = 0;
  while ((m = re.exec(text)) !== null) {
    if (m.index > last) out.push(text.slice(last, m.index));
    if (m[2] !== undefined) out.push(<strong key={k++}>{m[2]}</strong>);
    else if (m[3] !== undefined) out.push(<em key={k++}>{m[3]}</em>);
    else if (m[4] !== undefined) out.push(<code key={k++}>{m[4]}</code>);
    last = re.lastIndex;
  }
  if (last < text.length) out.push(text.slice(last));
  return out;
}
function renderMd(src) {
  const lines = src.split('\n');
  const blocks = []; let i = 0;
  while (i < lines.length) {
    const ln = lines[i];
    if (/^#{1,3}\s/.test(ln)) { const mm = ln.match(/^(#{1,3})\s+(.*)/); blocks.push({ t: 'h', lvl: mm[1].length, c: mm[2] }); i++; }
    else if (/^>\s?/.test(ln)) { const q = []; while (i < lines.length && /^>\s?/.test(lines[i])) { q.push(lines[i].replace(/^>\s?/, '')); i++; } blocks.push({ t: 'quote', c: q.join(' ') }); }
    else if (/^[-*]\s/.test(ln)) { const items = []; while (i < lines.length && /^[-*]\s/.test(lines[i])) { items.push(lines[i].replace(/^[-*]\s/, '')); i++; } blocks.push({ t: 'ul', items }); }
    else if (/^\d+\.\s/.test(ln)) { const items = []; while (i < lines.length && /^\d+\.\s/.test(lines[i])) { items.push(lines[i].replace(/^\d+\.\s+/, '')); i++; } blocks.push({ t: 'ol', items }); }
    else if (/^-{3,}\s*$/.test(ln)) { blocks.push({ t: 'hr' }); i++; }
    else if (ln.trim() === '') { i++; }
    else { const para = []; while (i < lines.length && lines[i].trim() !== '' && !/^(#{1,3}\s|>|[-*]\s|\d+\.\s|-{3,}\s*$)/.test(lines[i])) { para.push(lines[i]); i++; } blocks.push({ t: 'p', c: para.join(' ') }); }
  }
  return blocks.map((b, idx) => {
    if (b.t === 'h') { const Tag = `h${b.lvl}`; return <Tag key={idx} className={`md-h md-h${b.lvl}`}>{mdInlineP(b.c)}</Tag>; }
    if (b.t === 'p') return <p key={idx} className="md-p">{mdInlineP(b.c)}</p>;
    if (b.t === 'quote') return <blockquote key={idx} className="md-quote">{mdInlineP(b.c)}</blockquote>;
    if (b.t === 'ul') return <ul key={idx} className="md-ul">{b.items.map((x, j) => <li key={j}>{mdInlineP(x)}</li>)}</ul>;
    if (b.t === 'ol') return <ol key={idx} className="md-ol">{b.items.map((x, j) => <li key={j}>{mdInlineP(x)}</li>)}</ol>;
    if (b.t === 'hr') return <hr key={idx} className="md-hr" />;
    return null;
  });
}

function DocSurface({ item, detail }) {
  const d = detail;
  if (d.markdown) {
    return (
      <article className="ov-md">
        <div className="ov-md-meta">
          <span className="ov-md-filename">{d.file}</span>
          <span className="ov-md-divider">·</span>
          <span className="ov-md-stat">{d.lines} lignes</span>
          <span className="ov-md-divider">·</span>
          <span className="ov-md-stat">lecture ≈ 1 min</span>
        </div>
        {renderMd(NOTES_SAMPLE)}
      </article>
    );
  }
  return (
    <div className="ov-doc">
      <div className="ov-doc-pages">
        <div className="ov-doc-page">
          <div className="ov-doc-page-num">01</div>
          <div className="ov-doc-line ov-doc-line-h" />
          <div className="ov-doc-line" style={{ width: '92%' }} />
          <div className="ov-doc-line" style={{ width: '88%' }} />
          <div className="ov-doc-line" style={{ width: '95%' }} />
          <div className="ov-doc-line" style={{ width: '72%' }} />
          <div className="ov-doc-chart" />
        </div>
        <div className="ov-doc-page ov-doc-page-active">
          <div className="ov-doc-page-num">{String(d.showing || 2).padStart(2, '0')}</div>
          <div className="ov-doc-line ov-doc-line-h" />
          <div className="ov-doc-line" style={{ width: '88%' }} />
          <div className="ov-doc-line" style={{ width: '94%' }} />
          <div className="ov-doc-highlight">
            <div className="ov-doc-line" style={{ width: '70%', background: 'var(--accent)' }} />
            <div className="ov-doc-line" style={{ width: '82%', background: 'var(--accent)' }} />
          </div>
          <div className="ov-doc-line" style={{ width: '90%' }} />
          <div className="ov-doc-line" style={{ width: '66%' }} />
        </div>
        <div className="ov-doc-page">
          <div className="ov-doc-page-num">03</div>
          <div className="ov-doc-line ov-doc-line-h" />
          <div className="ov-doc-line" style={{ width: '80%' }} />
          <div className="ov-doc-grid"><div /><div /><div /><div /><div /><div /></div>
          <div className="ov-doc-line" style={{ width: '90%' }} />
        </div>
      </div>

      <div className="ov-doc-meta">
        <div className="ov-doc-meta-row"><span className="ov-doc-meta-label">TITRE</span><span className="ov-doc-meta-val">{d.file}</span></div>
        <div className="ov-doc-meta-row"><span className="ov-doc-meta-label">PAGES</span><span className="ov-doc-meta-val">{d.pages}  ·  <em>page {d.showing} affichée</em></span></div>
        <div className="ov-doc-meta-row"><span className="ov-doc-meta-label">ÉDITÉ</span><span className="ov-doc-meta-val">{d.edited}</span></div>
        <div className="ov-doc-meta-row"><span className="ov-doc-meta-label">TROUVÉ</span><span className="ov-doc-meta-val"><em>{d.found}</em></span></div>
      </div>
    </div>
  );
}

const OV_BODIES = {
  mail: MailSurface,
  doc: DocSurface,
  video: VideoSurface,
  contact: ContactSurface,
  action: ActionSurface,
};

// Mémoïsé : la scène se re-rend ~14×/s (useClock). Sans memo, l'animation
// d'entrée de la carte redémarre à chaque frame et reste bloquée à opacity:0.
window.DataOverlay = React.memo(DataOverlay);
