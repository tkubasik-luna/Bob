// overlay.jsx — Data overlay surfaces (email, image, video, map, doc, contact).
// Style intent: same HUD vocabulary as the rest of the chrome.
// One card, type-specific body, AI "surfacing" affordances.

const { useState: useStateO, useEffect: useEffectO, useRef: useRefO } = React;

// ─────────────────────────────────────────────────────────────────────────
// SURFACE META — drives picker labels, transcript line, header chips
// ─────────────────────────────────────────────────────────────────────────
const SURFACE_META = {
  none: { label: 'NONE', key: '0' },
  email: { label: 'MAIL', key: '7', chip: 'INBOX', line: "Here's the email from Marie — Q3 forecast review." },
  image: { label: 'IMAGE', key: '8', chip: 'PHOTO', line: "Living room camera, captured 14:08 today." },
  video: { label: 'VIDEO', key: '9', chip: 'FEED', line: "Front door — replay from three minutes ago." },
  map: { label: 'MAP', key: 'a', chip: 'ROUTE', line: "Fastest route — 18 minutes via Rue Lafayette." },
  doc: { label: 'DOC', key: 's', chip: 'FILE', line: "Q3 forecast draft — 14 pages, last edited yesterday." },
  contact: { label: 'CONTACT', key: 'd', chip: 'PERSON', line: "Sarah Chen — last spoken two days ago." },
  notes: { label: 'NOTES', key: 'f', chip: 'MARKDOWN', line: "Quick notes — Q3 narrative draft, three open questions." }
};

const SURFACE_ORDER = ['email', 'image', 'video', 'map', 'doc', 'contact', 'notes'];

// ─────────────────────────────────────────────────────────────────────────
// OVERLAY CARD — shell with corner brackets, header, body slot, actions
// ─────────────────────────────────────────────────────────────────────────
function OverlayCard({ surface, onClose }) {
  const meta = SURFACE_META[surface];
  if (!meta || surface === 'none') return null;

  // Escape closes
  useEffectO(() => {
    const onKey = (e) => {if (e.key === 'Escape') onClose();};
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, [onClose]);

  const Body = SURFACE_BODIES[surface];

  return (
    <div className="overlay-stage" key={surface}>
      {/* Projection beam — thin line from sphere center to card */}
      <div className="overlay-beam" />

      <div className={`overlay-card surface-${surface}`} role="dialog" aria-label={meta.label}>
        {/* corner brackets — match main HUD frame */}
        <span className="ov-corner tl" />
        <span className="ov-corner tr" />
        <span className="ov-corner bl" />
        <span className="ov-corner br" />

        {/* header strip */}
        <header className="ov-header">
          <div className="ov-header-left">
            <span className="ov-source-tag">BOB · SURFACING</span>
            <span className="ov-divider">/</span>
            <span className="ov-type-chip">{meta.chip}</span>
          </div>
          <div className="ov-header-right">
            <span className="ov-id-tag">REF · {meta.label.slice(0, 3)}-{Math.floor(1000 + Math.random() * 9000)}</span>
            <button className="ov-close" onClick={onClose} aria-label="dismiss">
              <span className="ov-close-glyph">✕</span>
            </button>
          </div>
        </header>

        {/* type-specific body */}
        <div className="ov-body">
          <Body />
        </div>

        {/* footer actions */}
        <footer className="ov-footer">
          <button className="ov-action ov-action-primary">
            <span className="ov-action-key">↵</span>
            <span>READ ALOUD</span>
          </button>
          <button className="ov-action">
            <span className="ov-action-key">↗</span>
            <span>OPEN</span>
          </button>
          <button className="ov-action" onClick={onClose}>
            <span className="ov-action-key">ESC</span>
            <span>DISMISS</span>
          </button>
        </footer>
      </div>
    </div>);

}

// ─────────────────────────────────────────────────────────────────────────
// BODIES — one per surface type
// ─────────────────────────────────────────────────────────────────────────

// ── EMAIL ──
function EmailBody() {
  return (
    <div className="ov-email">
      <div className="ov-email-meta">
        <div className="ov-avatar ov-avatar-grad-1">ML</div>
        <div className="ov-email-meta-text">
          <div className="ov-email-from">
            <span className="ov-email-name">Marie Lefèvre</span>
            <span className="ov-email-role">CFO · Lunabee</span>
          </div>
          <div className="ov-email-addr">marie.lefevre@lunabee.com  ·  14:22 today</div>
        </div>
        <div className="ov-email-flags">
          <span className="ov-flag ov-flag-important">PRIORITY</span>
        </div>
      </div>

      <h2 className="ov-email-subject">Q3 forecast — final review before Thursday</h2>

      <p className="ov-email-body">
        Bob, can you have the deck ready by Thursday afternoon? I want to walk through it
        with Antoine before the board call. The numbers from finance should now be locked,
        but I'd like one more pass on the narrative — particularly the Asia slowdown slide…
      </p>

      <div className="ov-email-attachments">
        <span className="ov-attach">
          <span className="ov-attach-icon">▤</span>
          <span className="ov-attach-name">Q3-forecast-v4.pdf</span>
          <span className="ov-attach-size">2.4 MB</span>
        </span>
        <span className="ov-attach">
          <span className="ov-attach-icon">▤</span>
          <span className="ov-attach-name">Asia-deck-notes.md</span>
          <span className="ov-attach-size">18 KB</span>
        </span>
      </div>
    </div>);

}

// ── IMAGE ──
function ImageBody() {
  return (
    <div className="ov-image">
      <div className="ov-image-frame">
        {/* Cinematic placeholder — gradient + grain + sun glow */}
        <div className="ov-image-canvas">
          <div className="ov-image-sky" />
          <div className="ov-image-sun" />
          <div className="ov-image-hill" />
          <div className="ov-image-hill ov-image-hill-2" />
          <div className="ov-image-foreground" />
          <div className="ov-image-grain" />
        </div>
        {/* HUD overlays on top of image */}
        <div className="ov-image-corner tl">CAM · LIVING</div>
        <div className="ov-image-corner tr">2592 × 1936</div>
        <div className="ov-image-corner bl">f/1.8  1/240s  ISO 100</div>
        <div className="ov-image-corner br">14:08:42</div>
      </div>
      <div className="ov-image-caption">
        <div className="ov-image-title">Living room — afternoon</div>
        <div className="ov-image-sub">Captured by iPhone · 14:08 today · 4.2 MB</div>
      </div>
    </div>);

}

// ── VIDEO ──
function VideoBody() {
  const [t, setT] = useStateO(0.34);
  const [playing, setPlaying] = useStateO(true);

  useEffectO(() => {
    if (!playing) return;
    const id = setInterval(() => setT((x) => (x + 0.005) % 1), 60);
    return () => clearInterval(id);
  }, [playing]);

  return (
    <div className="ov-video">
      <div className="ov-video-frame">
        <div className="ov-video-canvas">
          <div className="ov-video-bg" />
          <div className="ov-video-figure" />
          <div className="ov-video-door" />
          <div className="ov-video-scanline" />
          <div className="ov-image-grain" />
        </div>

        <div className="ov-video-recdot">
          <span className="ov-rec-pulse" /> REC · 03:14
        </div>
        <div className="ov-video-cam">CAM 02 · FRONT DOOR</div>

        <button className="ov-video-play" onClick={() => setPlaying(!playing)} aria-label="play">
          {playing ?
          <svg width="22" height="22" viewBox="0 0 22 22"><rect x="6" y="5" width="3" height="12" fill="currentColor" /><rect x="13" y="5" width="3" height="12" fill="currentColor" /></svg> :

          <svg width="22" height="22" viewBox="0 0 22 22"><path d="M7 5 L17 11 L7 17 Z" fill="currentColor" /></svg>
          }
        </button>
      </div>

      <div className="ov-video-controls">
        <span className="ov-video-time">{fmtTime(t * 184)}</span>
        <div className="ov-video-scrub">
          <div className="ov-video-scrub-track">
            <div className="ov-video-scrub-buffer" />
            <div className="ov-video-scrub-fill" style={{ width: `${t * 100}%` }} />
            <div className="ov-video-scrub-head" style={{ left: `${t * 100}%` }} />
            {[0.12, 0.28, 0.41, 0.55, 0.71, 0.86].map((m, i) =>
            <div key={i} className="ov-video-marker" style={{ left: `${m * 100}%` }} />
            )}
          </div>
        </div>
        <span className="ov-video-time ov-video-time-end">03:04</span>
      </div>
    </div>);

}

function fmtTime(s) {
  const m = Math.floor(s / 60);
  const sec = Math.floor(s % 60);
  return `${String(m).padStart(2, '0')}:${String(sec).padStart(2, '0')}`;
}

// ── MAP ──
function MapBody() {
  return (
    <div className="ov-map">
      <div className="ov-map-frame">
        <svg className="ov-map-svg" viewBox="0 0 600 340" preserveAspectRatio="xMidYMid slice">
          <defs>
            <radialGradient id="ovMapVignette" cx="0.5" cy="0.5" r="0.7">
              <stop offset="60%" stopColor="rgba(0,0,0,0)" />
              <stop offset="100%" stopColor="rgba(0,0,0,0.7)" />
            </radialGradient>
            <linearGradient id="ovWater" x1="0" y1="0" x2="1" y2="1">
              <stop offset="0%" stopColor="var(--accent)" stopOpacity="0.12" />
              <stop offset="100%" stopColor="var(--accent)" stopOpacity="0.04" />
            </linearGradient>
          </defs>

          {/* water blocks */}
          <path d="M -20 240 Q 100 220 180 250 T 380 240 L 380 360 L -20 360 Z" fill="url(#ovWater)" />
          <path d="M 460 -20 L 620 -20 L 620 80 Q 540 90 480 60 Z" fill="url(#ovWater)" />

          {/* secondary streets (dim) */}
          <g stroke="var(--hud-rule-dim)" strokeWidth="0.6" fill="none">
            <path d="M 0 60 L 600 80" />
            <path d="M 0 140 L 600 120" />
            <path d="M 0 200 L 600 220" />
            <path d="M 0 280 L 600 260" />
            <path d="M 80 0 L 90 340" />
            <path d="M 220 0 L 230 340" />
            <path d="M 360 0 L 350 340" />
            <path d="M 480 0 L 500 340" />
          </g>

          {/* blocks (subtle fill) */}
          <g fill="var(--hud-fill)" stroke="var(--hud-rule-dim)" strokeWidth="0.5">
            <rect x="100" y="80" width="115" height="55" />
            <rect x="240" y="60" width="105" height="75" />
            <rect x="370" y="100" width="95" height="40" />
            <rect x="110" y="160" width="100" height="35" />
            <rect x="230" y="150" width="115" height="45" />
          </g>

          {/* main route (animated dash) */}
          <path className="ov-map-route" d="M 60 290 Q 120 240 180 220 T 320 170 Q 380 150 440 110 L 510 70"
          fill="none" stroke="var(--accent)" strokeWidth="2.5"
          strokeLinecap="round" strokeDasharray="4 6" />

          {/* origin */}
          <g transform="translate(60, 290)">
            <circle r="6" fill="var(--bg)" stroke="var(--accent)" strokeWidth="1.5" />
            <circle r="2.5" fill="var(--accent)" />
          </g>

          {/* destination pin */}
          <g transform="translate(510, 70)" className="ov-map-pin">
            <circle r="14" fill="var(--accent)" opacity="0.12" />
            <circle r="8" fill="var(--accent)" opacity="0.25" />
            <circle r="4" fill="var(--accent)" />
            <line x1="0" y1="-22" x2="0" y2="-12" stroke="var(--accent)" strokeWidth="1" />
            <line x1="0" y1="12" x2="0" y2="22" stroke="var(--accent)" strokeWidth="1" />
            <line x1="-22" y1="0" x2="-12" y2="0" stroke="var(--accent)" strokeWidth="1" />
            <line x1="12" y1="0" x2="22" y2="0" stroke="var(--accent)" strokeWidth="1" />
          </g>

          <rect width="600" height="340" fill="url(#ovMapVignette)" pointerEvents="none" />
        </svg>

        <div className="ov-image-corner tl">48.8744° N  ·  2.3526° E</div>
        <div className="ov-image-corner tr">SCALE · 1:8000</div>
      </div>

      <div className="ov-map-stats">
        <div className="ov-map-stat">
          <span className="ov-map-stat-label">DEST</span>
          <span className="ov-map-stat-val">Le Bristol</span>
          <span className="ov-map-stat-sub">112 Rue du Faubourg Saint-Honoré</span>
        </div>
        <div className="ov-map-stat">
          <span className="ov-map-stat-label">ETA</span>
          <span className="ov-map-stat-val">18 <em>min</em></span>
          <span className="ov-map-stat-sub">via Rue Lafayette</span>
        </div>
        <div className="ov-map-stat">
          <span className="ov-map-stat-label">DIST</span>
          <span className="ov-map-stat-val">4.2 <em>km</em></span>
          <span className="ov-map-stat-sub">light traffic</span>
        </div>
      </div>
    </div>);

}

// ── DOC ──
function DocBody() {
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
          <div className="ov-doc-line ov-doc-line-h2" />
          <div className="ov-doc-line" style={{ width: '90%' }} />
          <div className="ov-doc-line" style={{ width: '84%' }} />
          <div className="ov-doc-chart" />
        </div>
        <div className="ov-doc-page ov-doc-page-active">
          <div className="ov-doc-page-num">02</div>
          <div className="ov-doc-line ov-doc-line-h" />
          <div className="ov-doc-line" style={{ width: '88%' }} />
          <div className="ov-doc-line" style={{ width: '94%' }} />
          <div className="ov-doc-line" style={{ width: '78%' }} />
          <div className="ov-doc-highlight">
            <div className="ov-doc-line" style={{ width: '70%', background: 'var(--accent)' }} />
            <div className="ov-doc-line" style={{ width: '82%', background: 'var(--accent)' }} />
          </div>
          <div className="ov-doc-line" style={{ width: '90%' }} />
          <div className="ov-doc-line" style={{ width: '66%' }} />
          <div className="ov-doc-line" style={{ width: '92%' }} />
        </div>
        <div className="ov-doc-page">
          <div className="ov-doc-page-num">03</div>
          <div className="ov-doc-line ov-doc-line-h" />
          <div className="ov-doc-line" style={{ width: '80%' }} />
          <div className="ov-doc-grid">
            <div /><div /><div /><div /><div /><div />
          </div>
          <div className="ov-doc-line" style={{ width: '90%' }} />
          <div className="ov-doc-line" style={{ width: '74%' }} />
        </div>
      </div>

      <div className="ov-doc-meta">
        <div className="ov-doc-meta-row">
          <span className="ov-doc-meta-label">TITLE</span>
          <span className="ov-doc-meta-val">Q3-Forecast-v4.pdf</span>
        </div>
        <div className="ov-doc-meta-row">
          <span className="ov-doc-meta-label">PAGES</span>
          <span className="ov-doc-meta-val">14  ·  <em>showing page 2</em></span>
        </div>
        <div className="ov-doc-meta-row">
          <span className="ov-doc-meta-label">EDITED</span>
          <span className="ov-doc-meta-val">Yesterday · 17:42  by Marie Lefèvre</span>
        </div>
        <div className="ov-doc-meta-row">
          <span className="ov-doc-meta-label">FOUND</span>
          <span className="ov-doc-meta-val">3 mentions of <em>"Asia slowdown"</em></span>
        </div>
      </div>
    </div>);

}

// ── CONTACT ──
function ContactBody() {
  return (
    <div className="ov-contact">
      <div className="ov-contact-card">
        <div className="ov-avatar ov-avatar-lg ov-avatar-grad-2">SC</div>
        <div className="ov-contact-text">
          <div className="ov-contact-name">Sarah Chen</div>
          <div className="ov-contact-role">Principal Designer · Lunabee</div>
          <div className="ov-contact-line">
            <span className="ov-contact-dot" />
            Available — focus mode until 16:00
          </div>
        </div>
        <div className="ov-contact-actions">
          <button className="ov-circle-btn" title="call">
            <svg width="14" height="14" viewBox="0 0 14 14"><path d="M3 2.5 L5 2 L6 4.5 L4.7 5.5 Q6 8 8.5 9.3 L9.5 8 L12 9 L11.5 11 Q8 12 5 9 T3 2.5 Z" fill="none" stroke="currentColor" strokeWidth="1.2" /></svg>
          </button>
          <button className="ov-circle-btn" title="message">
            <svg width="14" height="14" viewBox="0 0 14 14"><path d="M2 3 L12 3 L12 9 L7 9 L5 11 L5 9 L2 9 Z" fill="none" stroke="currentColor" strokeWidth="1.2" /></svg>
          </button>
          <button className="ov-circle-btn" title="more">
            <svg width="14" height="14" viewBox="0 0 14 14"><circle cx="3" cy="7" r="1.2" fill="currentColor" /><circle cx="7" cy="7" r="1.2" fill="currentColor" /><circle cx="11" cy="7" r="1.2" fill="currentColor" /></svg>
          </button>
        </div>
      </div>

      <div className="ov-contact-grid">
        <div className="ov-contact-field">
          <div className="ov-contact-field-label">EMAIL</div>
          <div className="ov-contact-field-val">sarah.chen@lunabee.com</div>
        </div>
        <div className="ov-contact-field">
          <div className="ov-contact-field-label">PHONE</div>
          <div className="ov-contact-field-val">+33 6 12 34 56 78</div>
        </div>
        <div className="ov-contact-field">
          <div className="ov-contact-field-label">LOCATION</div>
          <div className="ov-contact-field-val">Paris · GMT+1</div>
        </div>
        <div className="ov-contact-field">
          <div className="ov-contact-field-label">TIMEZONE</div>
          <div className="ov-contact-field-val">14:32 local</div>
        </div>
      </div>

      <div className="ov-contact-recent">
        <div className="ov-contact-recent-head">RECENT INTERACTIONS</div>
        <div className="ov-contact-recent-row">
          <span className="ov-tag-tiny">CALL</span>
          <span className="ov-contact-recent-text">12 min call — yesterday 11:14</span>
          <span className="ov-contact-recent-time">−1d</span>
        </div>
        <div className="ov-contact-recent-row">
          <span className="ov-tag-tiny">FILE</span>
          <span className="ov-contact-recent-text">Sent <em>Sphere-lab-v3.fig</em></span>
          <span className="ov-contact-recent-time">−2d</span>
        </div>
        <div className="ov-contact-recent-row">
          <span className="ov-tag-tiny">NOTE</span>
          <span className="ov-contact-recent-text">"Prefers async over calls before noon"</span>
          <span className="ov-contact-recent-time">−6d</span>
        </div>
      </div>
    </div>);

}

// ── NOTES (markdown) ──
// Tiny markdown renderer — headings, paragraphs, bold/italic/inline-code,
// links, lists, blockquotes, fenced code, horizontal rule.
function mdInline(text) {
  const out = [];
  const re = /(\*\*([^*]+)\*\*|\*([^*\n]+)\*|_([^_\n]+)_|`([^`]+)`|\[([^\]]+)\]\(([^)]+)\))/g;
  let last = 0;let m;let k = 0;
  while ((m = re.exec(text)) !== null) {
    if (m.index > last) out.push(text.slice(last, m.index));
    if (m[2] !== undefined) out.push(<strong key={k++}>{m[2]}</strong>);else
    if (m[3] !== undefined) out.push(<em key={k++}>{m[3]}</em>);else
    if (m[4] !== undefined) out.push(<em key={k++}>{m[4]}</em>);else
    if (m[5] !== undefined) out.push(<code key={k++}>{m[5]}</code>);else
    if (m[6] !== undefined) out.push(<a key={k++} href={m[7]} onClick={(e) => e.preventDefault()}>{m[6]}</a>);
    last = re.lastIndex;
  }
  if (last < text.length) out.push(text.slice(last));
  return out;
}

function renderMarkdown(src) {
  const lines = src.replace(/\r\n/g, '\n').split('\n');
  const blocks = [];
  let i = 0;
  while (i < lines.length) {
    const ln = lines[i];
    if (/^```/.test(ln)) {
      const lang = ln.slice(3).trim();
      const code = [];
      i++;
      while (i < lines.length && !/^```/.test(lines[i])) {code.push(lines[i]);i++;}
      i++;
      blocks.push({ t: 'code', lang, c: code.join('\n') });
    } else if (/^#{1,3}\s/.test(ln)) {
      const mm = ln.match(/^(#{1,3})\s+(.*)/);
      blocks.push({ t: 'h', lvl: mm[1].length, c: mm[2] });
      i++;
    } else if (/^>\s?/.test(ln)) {
      const q = [];
      while (i < lines.length && /^>\s?/.test(lines[i])) {q.push(lines[i].replace(/^>\s?/, ''));i++;}
      blocks.push({ t: 'quote', c: q.join(' ') });
    } else if (/^[-*]\s/.test(ln)) {
      const items = [];
      while (i < lines.length && /^[-*]\s/.test(lines[i])) {items.push(lines[i].replace(/^[-*]\s/, ''));i++;}
      blocks.push({ t: 'ul', items });
    } else if (/^\d+\.\s/.test(ln)) {
      const items = [];
      while (i < lines.length && /^\d+\.\s/.test(lines[i])) {items.push(lines[i].replace(/^\d+\.\s+/, ''));i++;}
      blocks.push({ t: 'ol', items });
    } else if (/^-{3,}\s*$/.test(ln)) {
      blocks.push({ t: 'hr' });
      i++;
    } else if (ln.trim() === '') {
      i++;
    } else {
      const para = [];
      while (i < lines.length && lines[i].trim() !== '' && !/^(#{1,3}\s|>|`{3}|[-*]\s|\d+\.\s|-{3,}\s*$)/.test(lines[i])) {
        para.push(lines[i]);i++;
      }
      blocks.push({ t: 'p', c: para.join(' ') });
    }
  }
  return blocks.map((b, idx) => {
    if (b.t === 'h') {
      const Tag = `h${b.lvl}`;
      return <Tag key={idx} className={`md-h md-h${b.lvl}`}>{mdInline(b.c)}</Tag>;
    }
    if (b.t === 'p') return <p key={idx} className="md-p">{mdInline(b.c)}</p>;
    if (b.t === 'quote') return <blockquote key={idx} className="md-quote">{mdInline(b.c)}</blockquote>;
    if (b.t === 'ul') return <ul key={idx} className="md-ul">{b.items.map((x, j) => <li key={j}>{mdInline(x)}</li>)}</ul>;
    if (b.t === 'ol') return <ol key={idx} className="md-ol">{b.items.map((x, j) => <li key={j}>{mdInline(x)}</li>)}</ol>;
    if (b.t === 'hr') return <hr key={idx} className="md-hr" />;
    if (b.t === 'code') return <pre key={idx} className="md-pre"><code>{b.c}</code></pre>;
    return null;
  });
}

const NOTES_SAMPLE = `# Q3 forecast — narrative notes

*Draft · last edited 17:42 yesterday by Marie*

Bob, here are the key points to weave into the deck before Thursday.

## What changed since v3

- **Asia revenue** down 8.2% QoQ — softer than the −5% we forecast
- EMEA still tracking to plan; North America *slightly* ahead
- Cash runway now extends to **Q2 2027** after the Series C close

## Open questions

1. Lead with Asia, or with cash position?
2. Antoine wants a single chart that tells the whole story
3. Do we name the three accounts that churned this quarter?

> "Investors don't need optimism — they need to see we know exactly what's happening."

## Next actions

- Lock the headline chart by Wed AM
- Send the draft to Antoine before Thursday noon
- Rehearse the Asia slide with \`slowdown_v3.key\`

---

*Sources: \`Q3-forecast-v4.pdf\` · \`Asia-deck-notes.md\`*
`;

function NotesBody() {
  return (
    <article className="ov-md">
      <div className="ov-md-meta">
        <span className="ov-md-filename">q3-narrative.md</span>
        <span className="ov-md-divider">·</span>
        <span className="ov-md-stat">42 lines</span>
        <span className="ov-md-divider">·</span>
        <span className="ov-md-stat">read ≈ 1 min</span>
      </div>
      {renderMarkdown(NOTES_SAMPLE)}
    </article>);

}

const SURFACE_BODIES = {
  email: EmailBody,
  image: ImageBody,
  video: VideoBody,
  map: MapBody,
  doc: DocBody,
  contact: ContactBody,
  notes: NotesBody
};

// ─────────────────────────────────────────────────────────────────────────
// SURFACE PICKER — small horizontal strip above state-pills
// ─────────────────────────────────────────────────────────────────────────
function SurfacePicker({ surface, onChange }) {
  return (
    <div className="surface-picker" style={{ fontFamily: "Times" }}>
      <span className="vs-label sp-label">SURFACE</span>
      <button
        data-surface="none"
        className={`pill pill-ghost ${surface === 'none' ? 'on-ghost' : ''}`}
        onClick={() => onChange('none')}>
        
        <span className="pill-key">0</span>
        <span>NONE</span>
      </button>
      {SURFACE_ORDER.map((s) =>
      <button
        key={s}
        data-surface={s}
        className={`pill pill-ghost ${surface === s ? 'on' : ''}`}
        onClick={() => onChange(s)}>
        
          <span className="pill-key">{SURFACE_META[s].key}</span>
          <span>{SURFACE_META[s].label}</span>
        </button>
      )}
    </div>);

}

window.Overlay = { OverlayCard, SurfacePicker, SURFACE_META, SURFACE_ORDER };