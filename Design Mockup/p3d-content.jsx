// p3d-content.jsx — shared content + thread model for the 3D exploration.
//
// NEW MODEL — one consciousness, many hands:
//   BOB        — le FIL DE LA CONSCIENCE PRINCIPALE. Il lit la demande,
//                réfléchit, puis INVOQUE des tâches en arrière-plan, tient le
//                fil pendant qu'elles tournent, et synthétise la réponse.
//   SUBTASKS   — les TÂCHES que Bob a créées. Chacune est autonome : elle
//                réfléchit, APPELLE SON PROPRE OUTIL, et rend sa conclusion.
//
// The left stack therefore shows TWO kinds of card with a clear visual split:
// the Bob thread (primary, orchestrator) and the sub-tasks (secondary, tools).
//
// Exports to window: PISTE, BOB, SUBTASKS, DATA_POOL, MAX_DATA, useClock,
// useThread, takeChars.

const { useState: useStateC, useEffect: useEffectC, useRef: useRefC } = React;

// ─────────────────────────────────────────────────────────────────────────
// LE FIL PRINCIPAL — BOB. Une demande, son monologue, sa réponse synthétisée.
// `summons` = les ids des sous-tâches qu'il crée et tient pendant qu'elles
// tournent.
// ─────────────────────────────────────────────────────────────────────────
const BOB = {
  id: 'bob',
  prompt:
    '« Reprends-moi ce que Daniela a envoyé cette semaine, vois si je suis ' +
    'libre jeudi après-midi, et fais-moi un point sur le budget v4. »',
  think:
    "Trois fils à tirer : trier les messages de Daniela, vérifier jeudi après-midi, " +
    "et résumer le budget v4. Je ne garde rien de ça en tête — j'invoque une tâche " +
    "en arrière-plan pour chacun, et je tiens le fil pendant qu'elles travaillent.",
  summons: ['courriel', 'agenda', 'budget'],
  answer:
    "**Daniela — 3 à voir :** budget v4 à valider, revue déplacée à **jeudi 15h**, " +
    "contrat renouvelé (info).\nJeudi après-midi est **libre**, sauf le créneau de 15h.\n" +
    "**Budget v4 :** +8 % vs v3, l'écart vient du poste cloud. Je te garde **16–17h jeudi** " +
    "pour qu'on le passe ensemble ?",
};

// ─────────────────────────────────────────────────────────────────────────
// LES SOUS-TÂCHES — créées par Bob, chacune appelle son propre outil.
// ─────────────────────────────────────────────────────────────────────────
const SUBTASKS = [
  {
    id: 'courriel', name: 'tâche · courriel', spec: 'gmail',
    think: "Filtrer les messages de Daniela sur 7 jours, séparer le signal du bruit.",
    tool: { name: 'gmail.search', args: 'from:daniela · 7 jours', result: '12 messages · 3 importants' },
    answer: "3 à voir : budget v4 à valider, revue déplacée jeudi 15h, contrat renouvelé (info).",
  },
  {
    id: 'agenda', name: 'tâche · agenda', spec: 'calendar',
    think: "Lire jeudi 12h–18h et repérer les créneaux déjà pris.",
    tool: { name: 'calendar.read', args: 'jeudi · 12h–18h', result: '1 créneau occupé · 15h' },
    answer: "Jeudi après-midi libre, sauf le créneau de 15h.",
  },
  {
    id: 'budget', name: 'tâche · budget', spec: 'drive',
    think: "Ouvrir le budget v4, comparer à la v3 et isoler l'écart principal.",
    tool: { name: 'drive.read', args: 'budget · v4 vs v3', result: '14 pages · écart +8 %' },
    answer: "Budget v4 : +8 % vs v3, l'écart vient du poste cloud.",
  },
];

// ─────────────────────────────────────────────────────────────────────────
// TIMELINE — un seul cycle, en boucle.
//   bob.think → bob.summon → (sous-tâches tournent en // ) → bob.answer → repos
// Chaque sous-tâche a son propre micro-cycle, décalé, donc elles se relaient au
// premier plan pendant qu'elles appellent leurs outils.
// ─────────────────────────────────────────────────────────────────────────
const SUB_PHASES = [
  { key: 'spawn',  dur: 0.5 },
  { key: 'think',  dur: 2.2 },
  { key: 'tool',   dur: 1.9 },
  { key: 'result', dur: 0.8 },
];
const SUB_RUN = SUB_PHASES.reduce((s, p) => s + p.dur, 0);   // 5.4
const SUB_TOOL_OFFSET = SUB_PHASES[0].dur + SUB_PHASES[1].dur; // when the tool call begins (2.7)

const SUMMON_START = 4.4;     // bob commence à invoquer
const SUMMON_END = 5.6;       // toutes les sous-tâches sont lancées
const SUB_STAGGER = 1.35;     // décalage de lancement entre sous-tâches

const N_SUB = SUBTASKS.length;
const subStartAt = (i) => SUMMON_END + i * SUB_STAGGER;
const LAST_SUB_DONE = subStartAt(N_SUB - 1) + SUB_RUN;
const GATHER = 0.9;
const ANSWER_AT = LAST_SUB_DONE + GATHER;
const ANSWER_DUR = 6.0;
const REST = 3.6;
const LOOP = ANSWER_AT + ANSWER_DUR + REST;

const SUB_STAT = { dormant: 'en attente', spawn: 'lancement', think: 'réflexion', tool: 'outil', result: 'résultat', done: 'rendu' };
const BOB_STAT = { think: 'réfléchit', summon: 'invoque', wait: 'tient le fil', answer: 'répond', done: 'au repos' };

function deriveSub(lt, i) {
  const start = subStartAt(i);
  const local = lt - start;
  let phase = 'dormant', frac = 0, beat = -Infinity, done = false, visible = false;
  if (local >= 0) {
    visible = true;
    if (local >= SUB_RUN) { phase = 'done'; frac = 1; done = true; beat = start + SUB_RUN; }
    else {
      let acc = 0;
      for (const p of SUB_PHASES) {
        if (local < acc + p.dur) { phase = p.key; frac = (local - acc) / p.dur; beat = start + acc; break; }
        acc += p.dur;
      }
    }
  }
  return { task: SUBTASKS[i], kind: 'sub', start, phase, frac, done, visible, beat };
}

function deriveBob(lt, summoned, returned) {
  let phase, frac, beat;
  if (lt < SUMMON_START) { phase = 'think'; frac = lt / SUMMON_START; beat = 0; }
  else if (lt < SUMMON_END) { phase = 'summon'; frac = (lt - SUMMON_START) / (SUMMON_END - SUMMON_START); beat = SUMMON_START; }
  else if (lt < ANSWER_AT) { phase = 'wait'; frac = (lt - SUMMON_END) / (ANSWER_AT - SUMMON_END); beat = SUMMON_END; }
  else if (lt < ANSWER_AT + ANSWER_DUR) { phase = 'answer'; frac = (lt - ANSWER_AT) / ANSWER_DUR; beat = ANSWER_AT; }
  else { phase = 'done'; frac = 1; beat = ANSWER_AT + ANSWER_DUR; }
  return { task: BOB, kind: 'bob', phase, frac, beat, summoned, returned };
}

// who sits at the front of the deck at time lt
function frontIdAt(lt, subs) {
  if (lt < SUMMON_END) return 'bob';
  if (lt >= ANSWER_AT) return 'bob';
  // during the working window, the sub-task that most recently entered its
  // TOOL phase is foregrounded so you can watch the call happen
  let id = 'bob', best = -Infinity;
  subs.forEach((s, i) => {
    const toolAt = s.start + SUB_TOOL_OFFSET;
    if (lt >= toolAt && !s.done && toolAt > best) { best = toolAt; id = s.task.id; }
  });
  return id;
}

// ENERGY feeding the core, per the front card's live phase
function energyAt(front) {
  if (front.kind === 'bob') return { think: 0.7, summon: 0.85, wait: 0.4, answer: 0.98, done: 0.2 }[front.phase] ?? 0.4;
  return { spawn: 0.5, think: 0.6, tool: 0.8, result: 0.7, done: 0.3 }[front.phase] ?? 0.4;
}

// ─────────────────────────────────────────────────────────────────────────
// THE DATA (artefacts générés). Plateau de MAX_DATA emplacements vivants.
// ─────────────────────────────────────────────────────────────────────────
const MAX_DATA = 5;

const DATA_POOL = [
  {
    type: 'mail', title: 'Daniela — budget v4', sub: 'reçu 14:22',
    detail: {
      from: 'Daniela Marsh', role: 'Finance · Lunabee', addr: 'daniela.marsh@lunabee.com',
      time: '14:22 · aujourd’hui', flag: 'PRIORITÉ', avatar: 'DM', grad: 1,
      subject: 'Budget v4 — à valider avant jeudi',
      body: "Bob, peux-tu faire valider la v4 avant la revue de jeudi ? L'écart principal vient du poste " +
            "cloud (+8 % vs v3). J'ai joint le tableau détaillé et mes notes sur les trois lignes qui bougent.",
      attachments: [
        { name: 'budget-v4.xlsx', size: '1.2 Mo' },
        { name: 'notes-cloud.md', size: '12 Ko' },
      ],
    },
  },
  {
    type: 'doc', title: 'Prévision Q3 · v4', sub: '14 pages',
    detail: {
      file: 'prevision-Q3-v4.pdf', pages: 14, showing: 2,
      edited: 'Hier · 17:42  par Marie Lefèvre',
      found: '3 mentions de « ralentissement Asie »',
    },
  },
  {
    type: 'video', title: "Porte d'entrée · replay", sub: '03:04',
    detail: { cam: 'CAM 02 · PORTE', recAt: '03:14', dur: '03:04', figure: true },
  },
  {
    type: 'contact', title: 'Sarah Chen', sub: 'Design · Paris',
    detail: {
      avatar: 'SC', grad: 2, role: 'Principal Designer · Lunabee',
      status: 'Disponible — focus jusqu’à 16h',
      email: 'sarah.chen@lunabee.com', phone: '+33 6 12 34 56 78',
      place: 'Paris · GMT+1', local: '14:32 locale',
      recent: [
        { tag: 'APPEL', text: 'Appel 12 min — hier 11:14', when: '−1j' },
        { tag: 'FICHIER', text: 'A envoyé sphere-lab-v3.fig', when: '−2j' },
        { tag: 'NOTE', text: '« Préfère l’async avant midi »', when: '−6j' },
      ],
    },
  },
  {
    type: 'action', title: 'Bloquer 16–17h jeudi', sub: 'agenda',
    detail: {
      verb: 'agenda.bloquer', target: 'Agenda — jeudi',
      summary: 'Réserver 16h–17h jeudi pour passer le budget v4 ensemble.',
      fields: [
        { k: 'QUAND', v: 'Jeudi · 16:00 → 17:00' },
        { k: 'AVEC', v: 'Daniela, Marie' },
        { k: 'OBJET', v: 'Revue budget v4' },
      ],
      confirm: 'Bloquer le créneau',
    },
  },
  {
    type: 'doc', title: 'Notes — récit Q3', sub: 'markdown',
    detail: { markdown: true, file: 'recit-q3.md', lines: 42 },
  },
  {
    type: 'mail', title: 'Marie — revue jeudi', sub: 'priorité',
    detail: {
      from: 'Marie Lefèvre', role: 'CFO · Lunabee', addr: 'marie.lefevre@lunabee.com',
      time: '11:04 · aujourd’hui', flag: 'PRIORITÉ', avatar: 'ML', grad: 2,
      subject: 'Revue Q3 — déplacée à jeudi 15h',
      body: "La revue passe à jeudi 15h pour caler avec Antoine avant le board. Garde-nous le créneau — " +
            "on enchaîne sur le budget juste après, prévois de quoi présenter la v4.",
      attachments: [{ name: 'ordre-du-jour.pdf', size: '320 Ko' }],
    },
  },
  {
    type: 'action', title: 'Envoyer deck à Antoine', sub: 'jeudi midi',
    detail: {
      verb: 'gmail.envoyer', target: 'Antoine Roux',
      summary: 'Transmettre le deck Q3 à Antoine avant la revue de jeudi midi.',
      fields: [
        { k: 'À', v: 'antoine.roux@lunabee.com' },
        { k: 'PIÈCE', v: 'Q3-forecast-v4.pdf · 2,4 Mo' },
        { k: 'AVANT', v: 'Jeudi · 12:00' },
      ],
      confirm: 'Préparer l’envoi',
    },
  },
  {
    type: 'contact', title: 'Antoine Roux', sub: 'Board · GMT+1',
    detail: {
      avatar: 'AR', grad: 1, role: 'Board · Lunabee',
      status: 'En réunion — dispo après 15h',
      email: 'antoine.roux@lunabee.com', phone: '+33 6 98 76 54 32',
      place: 'Paris · GMT+1', local: '14:32 locale',
      recent: [
        { tag: 'MAIL', text: 'Demande le récit Asie en 1 slide', when: '−1j' },
        { tag: 'AGENDA', text: 'Board call — jeudi 17h', when: '−3j' },
      ],
    },
  },
  {
    type: 'video', title: 'Salon · 14:08', sub: 'caméra',
    detail: { cam: 'CAM 01 · SALON', recAt: '02:41', dur: '02:08', figure: false },
  },
];

const DATA_TYPE_LABEL = {
  mail: 'COURRIEL', doc: 'DOCUMENT', video: 'VIDÉO', contact: 'CONTACT', action: 'ACTION',
};

const ENGINE = { name: 'claude-sonnet-4.5', spec: 'CLI', state: 'connecté' };

// ─────────────────────────────────────────────────────────────────────────
// THE PISTE — look « Nacre », layout « Céladon » (profondeur 3D).
// ─────────────────────────────────────────────────────────────────────────
const PISTE = {
  name: 'Nacre',
  tagline: 'sphère liquide · sanctuaire en profondeur',
  core: 'nebula',
  layout: 'depth',
  panel: 'frost',
  camera: 'deep',
  vars: {
    '--bg': '#160F18', '--bg2': '#211829',
    '--ink': '#F4E9F1', '--dim': 'rgba(244,233,241,0.62)', '--faint': 'rgba(244,233,241,0.34)',
    '--accent': '#E7B4CB', '--accent2': '#C6A2DB', '--accent3': '#F1E3EC',
    '--line': 'rgba(231,180,203,0.22)', '--fill': 'rgba(231,180,203,0.06)',
    // teinte propre aux sous-tâches — lavande plus froide, pour les distinguer de Bob
    '--sub': '#B79AE0', '--sub-line': 'rgba(183,154,224,0.30)', '--sub-fill': 'rgba(183,154,224,0.06)',
  },
};

// ─────────────────────────────────────────────────────────────────────────
// CLOCK — one rAF-driven seconds value, re-rendered ~every 80ms.
// ─────────────────────────────────────────────────────────────────────────
function useClock(fps = 12) {
  const [, force] = useStateC(0);
  useEffectC(() => {
    let raf, last = 0;
    const loop = (now) => {
      if (now - last > 1000 / fps) { last = now; force((x) => (x + 1) & 0xffff); }
      raf = requestAnimationFrame(loop);
    };
    raf = requestAnimationFrame(loop);
    return () => cancelAnimationFrame(raf);
  }, [fps]);
  return performance.now() / 1000;
}

// ─────────────────────────────────────────────────────────────────────────
// THREAD — derive the whole deck (Bob + summoned sub-tasks) from the clock,
// rank it (front = the live card), and expose a click-to-promote pin.
// ─────────────────────────────────────────────────────────────────────────
const PIN_HOLD = 7.0;

function useThread(t) {
  const [pin, setPin] = useStateC(null);   // { id, at } | null
  const pinActive = pin && (t - pin.at) < PIN_HOLD ? pin.id : null;

  const lt = ((t % LOOP) + LOOP) % LOOP;

  const subs = SUBTASKS.map((_, i) => deriveSub(lt, i));
  const summoned = subs.filter((s) => s.visible).length;
  const returned = subs.filter((s) => s.done).length;
  const bob = deriveBob(lt, summoned, returned);

  const autoFront = frontIdAt(lt, subs);
  const frontId = pinActive || autoFront;

  // visible deck = bob + summoned subs
  const cards = [bob, ...subs.filter((s) => s.visible)];
  cards.sort((a, b) => {
    const aid = a.kind === 'bob' ? 'bob' : a.task.id;
    const bid = b.kind === 'bob' ? 'bob' : b.task.id;
    if (aid === frontId) return -1;
    if (bid === frontId) return 1;
    return b.beat - a.beat;
  });
  const ordered = cards.map((c, i) => ({ ...c, rank: i }));

  const front = ordered[0];
  const promote = (id) => setPin({ id, at: performance.now() / 1000 });

  return { ordered, front, energy: energyAt(front), promote, pinnedId: pinActive, total: ordered.length };
}

function takeChars(str, frac) {
  if (frac >= 1) return str;
  return str.slice(0, Math.floor(str.length * frac));
}

Object.assign(window, {
  PISTE, BOB, SUBTASKS, SUB_STAT, BOB_STAT, DATA_POOL, DATA_TYPE_LABEL, MAX_DATA,
  ENGINE, useClock, useThread, takeChars,
});
