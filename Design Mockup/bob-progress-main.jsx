// bob-progress-main.jsx — composes the four explorations onto the design canvas,
// led by a framing card (assumptions + how to read).

const { useRef: useRefM } = React;

function IntroCard() {
  const P = BOB.PALETTE, F = BOB.FONTS;
  const pistes = [
    ['ORBIT', BOB.PALETTE.accent, 'Les tâches gravitent ; la traîne = le progrès.'],
    ['STREAM', BOB.TINTS.teal, 'Un courant de pensée ; le front lumineux = le progrès.'],
    ['BLOOM', BOB.TINTS.sage, 'Chaque tâche pousse ; la hauteur = le progrès.'],
    ['SYNAPSE', BOB.TINTS.mauve, 'Le plan en réseau ; la fraction allumée = le progrès.'],
  ];
  const Row = ({ children }) => (
    <div style={{ display: 'flex', gap: 10, alignItems: 'flex-start', marginBottom: 9 }}>
      <span style={{ color: P.accent, fontFamily: F.mono, fontSize: 11, lineHeight: '1.5', flex: '0 0 auto' }}>›</span>
      <span style={{ color: P.inkDim, fontSize: 13, lineHeight: 1.55 }}>{children}</span>
    </div>
  );
  return (
    <div style={{
      position: 'absolute', inset: 0, overflow: 'hidden', padding: '38px 36px',
      background: `radial-gradient(120% 80% at 0% 0%, ${P.bg3} 0%, ${P.bg2} 45%, ${P.bg} 85%)`,
      color: P.ink, fontFamily: F.sans, display: 'flex', flexDirection: 'column',
    }}>
      <div style={{ fontFamily: F.mono, fontSize: 10, letterSpacing: '0.34em', color: P.inkFaint, marginBottom: 14 }}>BOB · R&D AFFICHAGE DU PROGRÈS</div>
      <h1 style={{ fontFamily: F.serif, fontWeight: 400, fontSize: 33, lineHeight: 1.12, margin: '0 0 8px', letterSpacing: '-0.01em' }}>
        Faire <em style={{ color: P.accent, fontStyle: 'italic' }}>sentir</em> que l'intelligence travaille
      </h1>
      <p style={{ color: P.inkDim, fontSize: 13.5, lineHeight: 1.6, margin: '0 0 22px', maxWidth: 440 }}>
        Le texte + barre de progression dit <em>« ça charge »</em>. On veut une présence vivante qui montre <em>réellement</em> Bob en train d'agir — sur plusieurs tâches à la fois.
      </p>

      <div style={{ fontFamily: F.mono, fontSize: 9.5, letterSpacing: '0.24em', color: P.inkFaint, margin: '0 0 12px', paddingBottom: 8, borderBottom: `1px solid ${P.inkGhost}` }}>PARTI PRIS</div>
      <Row>Organisme vivant, pas tableau de bord — ça respire, ça pulse, ça pousse.</Row>
      <Row>La progression est <strong style={{ color: P.ink, fontWeight: 600 }}>portée par la forme elle-même</strong>, pas par une barre à part.</Row>
      <Row>Les 4 pistes partagent <strong style={{ color: P.ink, fontWeight: 600 }}>la même horloge</strong> : même instant, mêmes 4 tâches, 4 langages.</Row>

      <div style={{ fontFamily: F.mono, fontSize: 9.5, letterSpacing: '0.24em', color: P.inkFaint, margin: '22px 0 14px', paddingBottom: 8, borderBottom: `1px solid ${P.inkGhost}` }}>LES 4 PISTES</div>
      <div style={{ display: 'flex', flexDirection: 'column', gap: 11 }}>
        {pistes.map(([n, c, d]) => (
          <div key={n} style={{ display: 'flex', gap: 11, alignItems: 'baseline' }}>
            <span style={{ width: 8, height: 8, borderRadius: 2, background: c, transform: 'rotate(45deg)', flex: '0 0 auto', position: 'relative', top: 1 }} />
            <span style={{ fontFamily: F.mono, fontSize: 11.5, letterSpacing: '0.14em', color: P.ink, minWidth: 78, flex: '0 0 auto' }}>{n}</span>
            <span style={{ fontSize: 12.5, color: P.inkDim, lineHeight: 1.45 }}>{d}</span>
          </div>
        ))}
      </div>

      <div style={{ marginTop: 'auto', paddingTop: 22, fontFamily: F.mono, fontSize: 10, color: P.inkFaint, lineHeight: 1.7, letterSpacing: '0.04em' }}>
        4 tâches en boucle · queued → reading → thinking → tools → writing → done/failed<br />
        ◆ couleur = phase · petits nœuds = sous-agents · l'une échoue exprès (Q2 churn)<br />
        <span style={{ color: P.inkDim }}>→ Ouvre une piste en plein écran (⤢) pour la ressentir. Dis-moi celle(s) à pousser.</span>
      </div>
    </div>
  );
}

const AB_W = 1060, AB_H = 680;

function ProgressCanvas() {
  return (
    <DesignCanvas>
      <DCSection id="brief" title="Brief & système" subtitle="Le constat, le parti pris, comment lire les pistes">
        <DCArtboard id="intro" label="Lecture" width={560} height={AB_H}><IntroCard /></DCArtboard>
      </DCSection>
      <DCSection id="pistes" title="Les 4 pistes" subtitle="Mêmes 4 tâches, même instant — 4 façons de montrer le travail. Ouvre-en une en plein écran.">
        <DCArtboard id="orbit" label="1 · ORBIT — gravitation" width={AB_W} height={AB_H}><OrbitConcept /></DCArtboard>
        <DCArtboard id="stream" label="2 · STREAM — courant de pensée" width={AB_W} height={AB_H}><StreamConcept /></DCArtboard>
        <DCArtboard id="bloom" label="3 · BLOOM — croissance organique" width={AB_W} height={AB_H}><BloomConcept /></DCArtboard>
        <DCArtboard id="synapse" label="4 · SYNAPSE — réseau neuronal" width={AB_W} height={AB_H}><SynapseConcept /></DCArtboard>
      </DCSection>
    </DesignCanvas>
  );
}

ReactDOM.createRoot(document.getElementById('root')).render(<ProgressCanvas />);
