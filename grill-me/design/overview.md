# Mail Overlay — Design Recap

Source : `Design Mockup/overlay.jsx` (`EmailBody`, lignes 96-135) + `Design Mockup/screenshots/01-v4.png` (Mail surface active, blurred) + `01-mood.png` (Mail surface visible sur sphere).

## Identité visuelle

- Surface name : `email` (key=`7`, label=`MAIL`, chip=`INBOX`)
- Coexiste avec : image / video / map / doc / contact / notes (markdown)
- Shell commun : `OverlayCard` avec corner brackets, header (`BOB · SURFACING / INBOX` + `REF · MAI-XXXX` + close `✕`), body slot, footer actions (`READ ALOUD ↵`, `OPEN ↗`, `DISMISS ESC`)
- Beam projeté depuis sphère vers card (`.overlay-beam`)

## Structure body EmailBody

```
ov-email
├── ov-email-meta          (avatar + meta texte + flags)
│   ├── ov-avatar          (gradient initiales "ML")
│   ├── ov-email-meta-text
│   │   ├── ov-email-from  (name + role)
│   │   └── ov-email-addr  (addr + timestamp)
│   └── ov-email-flags     (PRIORITY pill jaune)
├── ov-email-subject       (h2)
├── ov-email-body          (p, body preview)
└── ov-email-attachments   (chips ▤ name size)
```

## Données affichées (mockup)

- from : "Marie Lefèvre" + "CFO · Lunabee"
- addr : `marie.lefevre@lunabee.com  ·  14:22 today`
- subject : "Q3 forecast — final review before Thursday"
- body : preview ~3 lignes
- attachments : `Q3-forecast-v4.pdf` 2.4 MB + `Asia-deck-notes.md` 18 KB
- flag : PRIORITY (jaune)

## Surfaces déjà câblées dans frontend réel

D'après mapping `Explore` : seul `Markdown` est implémenté côté `frontend/src/components/sphere/MarkdownOverlay.tsx`. `MailOverlay` à créer, pattern identique.

## Points d'intégration

- Composant frontend : `frontend/src/components/sphere/MailOverlay.tsx` (nouveau, miroir `MarkdownOverlay`)
- Component registry backend : ajouter `MAIL` dans `backend/src/bob/ui_registry.py` (build_registry)
- WS frame : `ui_payload` existant suffit, payload `{component: "Mail", props: {...}}`
- Déclenchement : tool/sub-agent émet `say.ui = {component: "Mail", props: ...}` à la fin de la background task
