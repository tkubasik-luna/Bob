# Components — Mail Overlay

## Nouveau composant frontend

`MailOverlay` (path : `frontend/src/components/sphere/MailOverlay.tsx`)

Props (à valider pendant grilling) :
```ts
type MailOverlayProps = {
  email: {
    from: { name: string; role?: string; address: string };
    receivedAt: string;          // ISO ou pré-formaté ?
    subject: string;
    bodyPreview: string;         // ou full ?
    flags?: ('priority'|'unread'|'starred')[];
    attachments?: { name: string; sizeBytes: number; mime?: string }[];
    threadId?: string;           // Gmail thread for OPEN action
    messageId?: string;
  } | null;
  onClose: () => void;
};
```

## Composants à réutiliser depuis Markdown overlay path

- Shell `overlay-stage` + corner brackets : factoriser ou dupliquer ?
- Footer actions (READ ALOUD / OPEN / DISMISS) : commun à toutes surfaces — candidate pour extraction

## Component descriptor backend

À ajouter dans `backend/src/bob/ui_registry.py:build_registry()` :

```python
MAIL = UIComponent(
    name="Mail",
    description="Display single email with from/subject/body/attachments",
    props_schema={
        "type": "object",
        "required": ["from", "subject", "bodyPreview", "receivedAt"],
        "properties": {
            "from": {...},
            "subject": {"type": "string"},
            "bodyPreview": {"type": "string"},
            "receivedAt": {"type": "string"},  # ISO 8601
            "flags": {"type": "array", "items": {"enum": ["priority","unread","starred"]}},
            "attachments": {"type": "array", "items": {...}},
            "threadId": {"type": "string"},
            "messageId": {"type": "string"},
        },
    },
)
```

Schéma exact à figer dans grilling (cf. questions Q5-Q7).
