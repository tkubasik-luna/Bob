# Investigation — Assistant IA type Jarvis

Source: https://claude.ai/share/74b25259-947e-4d6d-86e0-890559436f47

---

## Vous avez dit

Je veux faire un assistant IA avec une interface UI + TTS. Avec un llm sur lm studio. Suivant ce que je lui dit, il m'affiche des interface, mes tâches en cours etc… tu me conseille quoi comme stack technique

## Claude a répondu : Bonne idée de projet.

Bonne idée de projet. Avant de te donner une stack, quelques questions pour calibrer :

En attendant tes réponses, voici déjà la direction que je recommanderais par défaut, sachant que tu es à l'aise en KMP/Compose :

**Frontend** : Compose Multiplatform (desktop + mobile depuis un seul codebase Kotlin, tu réutilises ton expertise). Sinon Tauri + React si tu veux du web-tech avec un bundle léger.

**LLM** : LM Studio expose une API OpenAI-compatible sur `localhost:1234/v1`. Tu tapes dedans avec un client HTTP standard (Ktor côté Kotlin). Active le function calling ou demande au modèle de répondre en JSON structuré — c'est ce qui te permet de mapper "montre-moi mes tâches" → rendu d'un composant UI dédié plutôt qu'un blob de texte.

**TTS** : Piper (local, rapide, qualité étonnante en français, ~100ms de latence) si tu veux rester offline. ElevenLabs si tu acceptes le cloud pour une voix vraiment premium. Kokoro est une alternative locale récente qui monte en qualité.

**STT** (si tu veux parler à l'assistant) : Whisper.cpp en local, ou faster-whisper.

Le pattern clé pour le rendu dynamique d'UI : le LLM ne génère pas de l'UI directement, il renvoie un JSON typé du genre `{"action": "show_tasks", "filter": "today"}` et ton app a des composants Compose pré-construits qu'elle assemble selon la réponse. Tu gardes le contrôle du design et la latence reste basse.

Dis-moi tes réponses et je précise la stack avec des libs concrètes.

---

## Vous avez dit

- Q : L'app tourne où ? R : Multi-plateforme
- Q : Qui l'utilise ? R : Juste moi (perso)
- Q : TTS : qualité vs latence ? R : Local, rapide, voix correcte

## Claude a répondu : Parfait, ça clarifie.

Parfait, ça clarifie. Voici la stack que je te recommande :

**Frontend — Compose Multiplatform**
Tu connais déjà KMP/Compose, donc c'est le choix évident : un seul codebase pour desktop (Mac/Windows/Linux via JVM) et mobile (Android natif, iOS via le compilateur Kotlin/Native). Tu réutilises tes patterns Keylo.

Pour le "rendu d'UI dynamique selon ce que dit l'utilisateur", tu construis une bibliothèque de composants Compose typés (TaskList, Calendar, Note, Chart…) et un dispatcher qui les assemble selon la réponse du LLM.

**LLM via LM Studio**
LM Studio expose une API OpenAI-compatible sur `http://localhost:1234/v1/chat/completions`. Tu tapes dedans avec Ktor Client (déjà familier).

Le pattern clé : structured outputs. Tu forces le modèle à répondre en JSON avec un schéma précis. LM Studio supporte le mode `json_schema` depuis quelques versions. Exemple de réponse :

```json
{
  "speech": "Voici tes 3 tâches prioritaires aujourd'hui",
  "ui": { "component": "TaskList", "props": { "filter": "today", "limit": 3 } }
}
```

Le champ `speech` part au TTS, le champ `ui` est dispatché vers ton composant Compose. Propre et déterministe.

Modèles à essayer sur LM Studio : Qwen 2.5 7B ou Llama 3.1 8B pour un bon équilibre vitesse/qualité en function calling. Si ta machine encaisse, Qwen 2.5 14B est nettement meilleur sur le suivi de schéma.

**TTS local — Piper**
Piper coche toutes tes cases : local, rapide (~100-200ms de latence sur CPU), voix françaises correctes (`fr_FR-siwis-medium` ou `fr_FR-upmc-medium`), binaire C++ standalone que tu appelles en sous-processus depuis Kotlin. Tu lui envoies du texte sur stdin, il sort du WAV sur stdout. Setup en 10 minutes.

Alternative si tu veux un cran au-dessus en qualité : Kokoro (très récent, qualité quasi-cloud, un peu plus lourd).

**STT optionnel — whisper.cpp**
Si tu veux parler à l'assistant et pas juste taper : `whisper.cpp` avec le modèle `small` ou `medium` en français. Pareil, binaire standalone appelé depuis Kotlin.

**Persistance locale — SQLDelight**
Pour tes tâches, notes, historique de conversation : SQLDelight (KMP-native, requêtes typées). Évite Room qui est Android-only.

**Architecture en gros**

```
[Mic] → whisper.cpp → texte
                        ↓
[Texte] → Ktor → LM Studio (JSON schema) → { speech, ui }
                                              ↓         ↓
                                           Piper    Compose dispatcher
                                              ↓         ↓
                                          [Audio]   [UI rendue]
```

**Quelques pièges à anticiper**
Le streaming des tokens du LLM avec structured output : tu dois attendre le JSON complet avant de parser, donc tu perds le streaming mot-à-mot. Workaround : demande au modèle de mettre le `speech` en premier dans le JSON et stream le TTS en parsant progressivement, ou fais deux appels (un pour la parole, un pour l'UI). La première option est plus élégante mais demande un parseur JSON tolérant.

Sur iOS, lancer des sous-processus (Piper, whisper) est interdit hors sandbox — tu devras passer par leurs versions compilées en lib statique liée à ton binaire. C'est faisable mais ajoute du travail. Si iOS n'est pas prioritaire pour ta v1, commence desktop seulement.

Tu veux que je te détaille une partie en particulier (le schéma JSON LLM → UI, le setup Piper, l'intégration Ktor avec LM Studio) ?

---

## Vous avez dit

Je veux une partie serveur sur mon Mac, et une app cliente. Je veux une interface type Jarvis dans iron man donc pas sûr pour le Kotlin

## Claude a répondu : Ah ok, ça change complètement la donne.

Ah ok, ça change complètement la donne. Pour une UI type Jarvis (hologrammes animés, ondes audio réactives, particules, glassmorphism, transitions fluides), tu veux du web tech — c'est là que le tooling créatif est le plus mature.

**Architecture client/serveur**

```
┌─────────────── Mac (serveur) ──────────────┐
│  LM Studio (port 1234)                     │
│  Backend Python ou Node (port 8000)        │
│   ├─ Orchestration LLM                     │
│   ├─ Piper TTS                             │
│   ├─ Whisper STT                           │
│   └─ WebSocket pour streaming              │
└────────────────────────────────────────────┘
                    ↕ WebSocket
┌─────────────── Client (multi) ─────────────┐
│  Tauri + React + Three.js / WebGL          │
│   ├─ UI Jarvis (canvas, shaders)           │
│   ├─ Audio playback                        │
│   └─ Mic capture                           │
└────────────────────────────────────────────┘
```

**Backend serveur (sur ton Mac)**
Python + FastAPI est le choix pragmatique : écosystème IA imbattable, bindings natifs pour Piper (`piper-tts` en pip) et Whisper (`faster-whisper`), WebSocket natif via FastAPI. Tu écris ~200 lignes et c'est en l'air.

Alternative si tu préfères TypeScript partout : Node + Fastify, mais tu devras appeler Piper/Whisper en sous-processus plutôt qu'en lib native.

Le serveur expose grosso modo :
- `WS /chat` — flux bidirectionnel : tu pushes audio ou texte, il streame back `{type: "speech_chunk", audio: ...}`, `{type: "ui", component: ...}`, `{type: "transcript", text: ...}`
- Il parle à LM Studio via son endpoint OpenAI-compatible
- Il génère le TTS à la volée et streame les chunks audio
- Il garde l'état conversation, l'historique, tes tâches (SQLite)

**Client — Tauri + React + Three.js**
Tauri plutôt qu'Electron : bundle ~10 Mo au lieu de 150, perf natives, et ça compile pour macOS / Windows / Linux / iOS / Android depuis le même codebase. Le frontend dedans c'est du web standard donc tu profites de tout l'écosystème.

Pour le look Jarvis, la stack qui marche :
- React pour la structure (familier, large écosystème)
- Three.js ou React Three Fiber pour les éléments 3D (sphère centrale, hologrammes, anneaux rotatifs)
- Shaders GLSL pour les effets vraiment "Jarvis" (distorsions, glow, scan lines) — c'est là que ça devient beau, pas juste joli
- Framer Motion pour les transitions 2D fluides entre les états
- Web Audio API pour visualiser les ondes audio en temps réel pendant que ça parle (le truc qui pulse au rythme de la voix, signature de Jarvis)
- Tailwind pour le layout rapide

Pour t'inspirer du visuel, cherche "audio reactive shader", "Three.js hologram", "siri orb webgl" — beaucoup d'exemples open-source.

**LLM avec structured output**
Pareil que ce que je t'ai décrit avant : tu forces LM Studio à répondre en JSON typé. Pour Jarvis ça donnerait quelque chose comme :

```json
{
  "speech": "Voici ton planning de la journée, Tom",
  "ui_actions": [
    { "type": "show_panel", "panel": "calendar", "data": {...} },
    { "type": "highlight", "target": "next_meeting" }
  ],
  "ambient": "thinking_done"
}
```

Le champ `ambient` peut piloter l'état visuel de la sphère centrale (idle, thinking, speaking, alert).

**Modèle LLM**
Pour du function calling/JSON propre en local : Qwen 2.5 14B Instruct si ton Mac le supporte (M1 Pro 16Go ou plus), sinon Qwen 2.5 7B. Llama 3.1 8B marche aussi mais Qwen est meilleur sur le respect du schéma JSON en français.

**TTS — Piper côté serveur**
Le serveur Python charge Piper en lib native, génère les chunks WAV à la volée et les streame via WebSocket. Le client les joue avec Web Audio API et alimente en même temps le visualiseur de fréquences. Latence totale (LLM → audio dans tes oreilles) : 500ms-1s selon le modèle.

**STT — Whisper côté serveur**
Le client capture le micro, streame les chunks PCM via WebSocket, le serveur fait tourner `faster-whisper` (modèle `small` ou `medium`) et renvoie la transcription. Tu peux aussi faire du STT côté client avec Whisper Web (WebAssembly) si tu veux décharger le serveur, mais c'est plus lent.

**Ordre de construction recommandé**
D'abord le backend nu : FastAPI + WebSocket + appel LM Studio + JSON structuré, testé avec un client en ligne de commande. Ensuite Piper en sortie. Ensuite un client React/Tauri minimaliste qui affiche le texte et joue l'audio. Ensuite seulement tu attaques le Jarvis visuel — sinon tu vas passer trois semaines sur des shaders avant d'avoir un truc qui marche.

Tu veux que je détaille le backend FastAPI (squelette de code avec WebSocket + LM Studio + Piper) ou la partie visuelle Three.js en premier ?

---

## Vous avez dit

Je veux mettre en place un RAG via un knowledge graph

## Claude a répondu : Bon choix pour un assistant perso

Bon choix pour un assistant perso — un KG donne du raisonnement structuré là où un RAG vectoriel classique se contente de retrouver des bouts de texte. Pour un Jarvis qui doit savoir "quelles tâches sont liées à Keylo cette semaine", c'est nettement plus pertinent.

**Architecture RAG via knowledge graph**

```
Ingestion : doc/note/tâche/event
   ↓
LLM extrait entités + relations (triplets)
   ↓
Stockage : graphe (Neo4j) + embeddings (Qdrant)
   ↓
Query : LLM transforme la question en requête graphe + recherche sémantique
   ↓
Contexte hybride → LM Studio → réponse
```

**Le pattern hybride (GraphRAG)**
Tu mélanges deux choses qui se complètent :
- Le graphe capture les relations explicites : "Tâche X concerne projet Keylo", "Event Y avec personne Z", "Note A référence note B"
- Les embeddings capturent la similarité sémantique floue : "trouve-moi des trucs liés à 'compliance'"

Au moment de la requête, tu fais les deux en parallèle puis tu fusionnes. C'est l'approche GraphRAG popularisée par Microsoft Research fin 2024, mais en version perso/léger.

**Stack recommandée**

**Base graphe — Neo4j Community Edition**
Gratuit, mature, tourne en Docker sur ton Mac en une commande. Le langage Cypher est lisible et le LLM le génère bien. Driver Python officiel.

Alternative plus légère : Kùzu (embedded, comme SQLite mais pour graphes, pas de serveur séparé). Plus jeune mais sympa pour du perso. Si tu veux du minimaliste, c'est ça.

**Embeddings — Qdrant ou pgvector**
Qdrant en Docker à côté de Neo4j, ou pgvector si tu utilises déjà PostgreSQL (tu l'as dans ta stack Keylo, ça peut être le même Postgres avec un schéma dédié). Pour un usage perso, pgvector suffit largement et ça simplifie l'ops.

Pour générer les embeddings localement : sentence-transformers côté Python avec un modèle multilingue comme `paraphrase-multilingual-mpnet-base-v2` ou plus récent `BAAI/bge-m3` (excellent en français).

**Extraction d'entités/relations**
C'est là que ton LLM local bosse. Tu lui donnes un schéma de triplets attendus et tu lui demandes d'extraire en JSON :

```json
{
  "entities": [
    {"id": "keylo", "type": "Project", "name": "Keylo"},
    {"id": "task_123", "type": "Task", "name": "Setup Spring Boot"}
  ],
  "relations": [
    {"from": "task_123", "to": "keylo", "type": "BELONGS_TO"}
  ]
}
```

Pour cette étape, Qwen 2.5 14B est nettement meilleur que 7B. Tu peux aussi utiliser Claude via l'API si tu acceptes du cloud pour l'ingestion (les réponses temps réel restent locales).

**Framework — LlamaIndex ou direct**
LlamaIndex a un module `PropertyGraphIndex` qui fait tout ça out-of-the-box : extraction, stockage Neo4j, retrieval hybride. Ça t'épargne 80% du code de plomberie. C'est la voie rapide.

Si tu veux comprendre/contrôler chaque étape (recommandé vu que tu construis perso et que tu veux apprendre), code-le toi-même en ~500 lignes Python. LangGraph peut aussi orchestrer ça si tu veux une approche par agents, mais pour un RAG simple c'est overkill.

**Schéma de graphe suggéré pour un assistant perso**
Définis ton ontologie en avance, sinon le LLM va créer 50 types d'entités incohérents :

```
Entities : Person, Project, Task, Note, Event, Document, Topic, Tool
Relations : BELONGS_TO, MENTIONS, ASSIGNED_TO, DEPENDS_ON,
            HAPPENED_AT, REFERENCES, ABOUT_TOPIC
```

Tu donnes ce schéma au LLM dans le prompt d'extraction et tu le forces à n'utiliser que ces types. Sinon le graphe devient illisible en deux semaines.

**Pipeline de requête**
Quand tu poses une question à Jarvis :
1. Le LLM analyse l'intent et génère soit une requête Cypher, soit une query sémantique, soit les deux
2. Cypher → Neo4j → liste de nœuds/relations pertinents
3. Query sémantique → Qdrant/pgvector → chunks de texte
4. Tu fusionnes (les nœuds graphe servent à étendre le contexte : pour chaque nœud trouvé, tu vas chercher ses voisins à 1-2 hops)
5. Contexte assemblé → injecté dans le prompt final → LM Studio génère la réponse

L'astuce qui change tout : utiliser le graphe pour expansion de contexte, pas juste pour le lookup. Tu trouves "Keylo" → tu remontes automatiquement les 10 tâches liées, les notes mentionnant Keylo, les events récents tagués Keylo, et tu donnes ce paquet au LLM.

**Ce qui s'intègre dans ton backend FastAPI**
Tu ajoutes deux services à côté du serveur que je t'ai décrit avant :
- `ingestion_service` : appelé quand tu crées une note, finis une tâche, etc. → extrait triplets → met à jour graphe + embeddings
- `retrieval_service` : appelé avant chaque tour de conversation → assemble le contexte → l'injecte dans le prompt LM Studio

**Ordre de construction**
Commence vraiment simple : juste pgvector ou Qdrant avec embeddings, sans graphe. Tu valides que le RAG vectoriel fait déjà le job pour 80% des requêtes. Puis tu ajoutes Neo4j et l'extraction de triplets pour les 20% qui demandent du raisonnement relationnel. Sinon tu vas passer un mois à debugger l'extraction d'entités avant d'avoir un truc utilisable.

Un piège classique : ne pas ré-extraire tout à chaque modif. Garde un hash du contenu source et ne ré-ingère que les diffs.

Tu veux que je détaille l'ontologie pour un assistant perso (quels nœuds/relations pour tâches, notes, calendrier, projets), le code d'extraction de triplets avec LM Studio, ou le pipeline de requête hybride ?

---

## Vous avez dit

Comment on fait pour que le llm puisse faire des queries sur le neo4j ou autre ?

## Claude a répondu : Excellente question, c'est le cœur du système.

Excellente question, c'est le cœur du système. Il y a trois approches, par ordre de robustesse :

**Approche 1 — Text-to-Cypher (naïve)**
Tu demandes au LLM de générer directement du Cypher depuis la question en langage naturel.

```python
prompt = f"""Schéma du graphe :
(:Project)-[:HAS_TASK]->(:Task)
(:Task)-[:ASSIGNED_TO]->(:Person)
(:Task {{status: 'todo'|'doing'|'done'}})

Question : {user_question}
Génère UNIQUEMENT la requête Cypher, sans explication."""

cypher = llm.complete(prompt)
results = neo4j.run(cypher)
```

Ça marche pour des questions simples mais ça casse vite : le LLM invente des propriétés, oublie des `WHERE`, génère du Cypher invalide. Sur Qwen 7B local, attends-toi à ~60% de réussite. Sur 14B, ~80%. Pas suffisant pour de la prod, même perso.

**Approche 2 — Function calling / Tool use (recommandée)**
Tu n'exposes pas Cypher au LLM. Tu exposes des fonctions typées qu'il peut appeler, et toi tu traduis ces appels en requêtes Cypher sûres derrière. C'est l'approche pro.

```python
tools = [
    {
        "name": "find_tasks",
        "description": "Trouve des tâches selon des critères",
        "parameters": {
            "project": {"type": "string", "optional": True},
            "status": {"enum": ["todo", "doing", "done"], "optional": True},
            "assignee": {"type": "string", "optional": True},
            "due_before": {"type": "date", "optional": True}
        }
    },
    {
        "name": "find_related",
        "description": "Trouve les entités liées à une entité donnée",
        "parameters": {
            "entity_id": {"type": "string"},
            "max_hops": {"type": "integer", "default": 2}
        }
    },
    {
        "name": "search_notes",
        "description": "Recherche sémantique dans les notes",
        "parameters": {"query": {"type": "string"}}
    }
]
```

LM Studio supporte le function calling au format OpenAI depuis quelques versions. Le LLM renvoie :

```json
{
  "tool_calls": [
    {"name": "find_tasks", "args": {"project": "Keylo", "status": "todo"}}
  ]
}
```

Toi tu mappes ça vers du Cypher paramétré sûr :

```python
def find_tasks(project=None, status=None, assignee=None, due_before=None):
    query = "MATCH (t:Task)"
    conditions, params = [], {}
    if project:
        query += "-[:BELONGS_TO]->(p:Project {name: $project})"
        params["project"] = project
    if status:
        conditions.append("t.status = $status")
        params["status"] = status
    # ...
    if conditions:
        query += " WHERE " + " AND ".join(conditions)
    query += " RETURN t LIMIT 20"
    return neo4j.run(query, params)
```

Avantages : pas d'injection Cypher possible, schéma stable, erreurs prévisibles, fonctionne avec des LLM plus petits (Qwen 7B suffit largement). C'est ce que tu veux pour ton Jarvis.

**Approche 3 — Agent ReAct (plus puissant, plus lent)**
Le LLM tourne en boucle : il appelle un outil, voit le résultat, décide d'appeler un autre, jusqu'à avoir assez de contexte pour répondre.

```
User: "Quelles tâches Keylo sont bloquées par d'autres tâches ?"

LLM: appel find_tasks(project="Keylo")
     → reçoit liste de 12 tâches
LLM: appel find_related(entity_id="task_5", max_hops=1, relation="DEPENDS_ON")
     → voit que task_5 dépend de task_2 (status: todo)
LLM: même chose pour task_6, task_7...
LLM: synthétise la réponse finale
```

Frameworks qui font ça pour toi : LangGraph (le plus contrôlable), LlamaIndex agents, ou tu codes la boucle ReAct toi-même en 80 lignes (recommandé pour comprendre). Coût : 3-8 appels LLM au lieu d'un, donc plusieurs secondes de latence en local.

**Approche 4 — MCP (Model Context Protocol)**
Plus récent : tu exposes Neo4j comme un serveur MCP, et n'importe quel client compatible (Claude Desktop, ton client custom) peut s'y connecter. Il existe un `mcp-neo4j` officiel. Intéressant si tu veux découpler tes outils du backend, mais pour un setup mono-utilisateur sur ton Mac, c'est un peu over-engineered. À garder en tête pour plus tard.

**Ma reco concrète pour ton Jarvis**
Commence par l'approche 2 (function calling avec ~5-8 outils bien définis). C'est le meilleur ratio simplicité/robustesse. Tu codes les outils comme des fonctions Python qui font du Cypher paramétré derrière.

Ensuite, si tu vois que certaines questions nécessitent plusieurs étapes ("trouve les tâches X puis pour chacune cherche Y"), tu enveloppes ça dans une boucle ReAct minimaliste — l'approche 3 — sans framework, juste un `while` qui rappelle le LLM tant qu'il génère des `tool_calls`.

**Le piège qui tue tous ces projets**
Les LLM locaux 7B-14B font du function calling correct mais pas parfait. Tu dois :
- Valider chaque tool_call contre un schéma JSON (avec Pydantic) avant exécution
- Avoir un fallback : si le LLM rate 3 fois, tu réponds "je n'ai pas compris, reformule"
- Logger toutes les requêtes Cypher pour debug
- Mettre un timeout sur Neo4j (`CALL apoc.cypher.runTimeboxed`) au cas où le LLM génère une requête qui scanne tout le graphe

**Côté boîte à outils typique pour un assistant perso**

```
search_notes(query, top_k)         → vectoriel
find_tasks(filters...)              → graphe
find_events(date_range, ...)        → graphe
find_related(entity, hops, rel)     → graphe (1-2 hops)
get_entity_details(entity_id)       → graphe (nœud + voisins immédiats)
create_task(...) / update_task(...) → mutation
add_note(content, tags)             → mutation + ingestion
```

7-8 outils bien pensés couvrent 95% des cas d'usage et restent compréhensibles par un LLM 7B.

Tu veux que je te montre un squelette concret en Python — le client LM Studio + définition d'outils + boucle de dispatch — pour que tu aies un point de départ exécutable ?

---

## Vous avez dit

Je veux que l'agent mette à jour le RAG tout seul en fonction de ce que je lui dit

## Claude a répondu : Excellente direction

Excellente direction — c'est ce qui transforme l'assistant en vraie mémoire vivante. Le pattern s'appelle parfois "self-updating memory" ou "agentic memory". Voici comment je le construirais.

**Le principe**
À chaque tour de conversation, en plus de répondre, l'agent décide en parallèle si quelque chose mérite d'être mémorisé, et quoi exactement. Il a des outils de mutation du graphe au même titre que des outils de lecture.

```
User: "J'ai eu un call avec Marc aujourd'hui sur la partie OCR de Keylo,
       il pense qu'on devrait utiliser Mistral OCR plutôt que Tesseract"

Agent (en arrière-plan) :
  → extract_memory(text)
    → entities: [Person:Marc, Project:Keylo, Topic:OCR, Tool:Mistral OCR, Tool:Tesseract]
    → relations: [Marc DISCUSSED_WITH user ABOUT OCR, Marc RECOMMENDS Mistral OCR]
    → facts: ["Marc préfère Mistral OCR à Tesseract pour Keylo"]
  → upsert_entities(...)
  → upsert_relations(...)
  → add_note(content, links_to=[Marc, Keylo, OCR])

Agent (réponse vocale) :
  "Noté. Je retiens que Marc te recommande Mistral OCR pour l'OCR Keylo.
   Tu veux que je crée une tâche pour évaluer Mistral OCR ?"
```

**Architecture : deux boucles parallèles**
Le truc qui marche vraiment bien : séparer la réponse temps réel de l'écriture mémoire, qui peut prendre plus de temps.

```
                   User input
                       ↓
              ┌────────┴────────┐
              ↓                 ↓
        [Boucle rapide]    [Boucle mémoire]
        Récupère contexte  Extrait entités/relations
        Répond + TTS       Décide quoi écrire
        (< 2s)             Update graphe + embeddings
                           (peut prendre 5-10s)
```

L'utilisateur a sa réponse vocale immédiate. L'écriture mémoire tourne en async (asyncio task ou queue Redis/RQ). Si l'écriture rate, tu loggues et tu retry — pas grave si Jarvis met 30s à enregistrer un fait, l'important c'est qu'il réponde vite.

**Les outils de mutation à exposer**
En plus des outils de lecture (find_tasks, search_notes…), tu ajoutes :

```python
upsert_entity(type, name, properties, aliases=[])
  # Crée ou met à jour. Gère le déduplication par name + aliases.

upsert_relation(from_id, to_id, type, properties={})
  # Crée ou met à jour une relation.

add_note(content, mentions=[entity_ids], tags=[])
  # Ajoute une note libre + l'indexe dans embeddings + crée des MENTIONS.

create_task(title, project_id, due_date=None, ...)
update_task(task_id, changes={...})
mark_done(task_id)

record_fact(subject_id, predicate, object, source="conversation",
            confidence=0.9, expires_at=None)
  # Pour les "préférences" : "Marc préfère X", "Tom code en Kotlin"

forget(entity_id | fact_id, reason="user_request")
  # Soft delete avec audit log. Jamais de hard delete.
```

**Le prompt système qui pilote la mémorisation**
C'est là que ça se joue. Tu donnes à l'agent une politique de mémorisation claire dans le system prompt :

```
À chaque message de l'utilisateur, après avoir répondu, tu évalues :

MÉMORISE si le message contient :
- Un fait nouveau et durable (préférence, décision, info sur une personne/projet)
- Une tâche, un événement, une échéance
- Une référence explicite ("rappelle-toi que...", "note que...")
- Un changement d'état (tâche terminée, projet abandonné)

NE MÉMORISE PAS :
- Les questions sans nouvelle info
- Les small talk
- Les commandes ponctuelles ("éteins la lumière")
- Les répétitions d'infos déjà en base

Pour chaque élément à mémoriser, choisis l'outil approprié et appelle-le.
Plusieurs outils peuvent être appelés en séquence.
```

**Le piège central : la déduplication d'entités**
Le LLM va extraire "Marc", puis demain "Marc Dupont", puis "marc". Sans déduplication, ton graphe explose en doublons inutiles en deux semaines.

Stratégie en couches :
1. **Normalisation** : lowercase, strip, accents enlevés pour la clé de lookup
2. **Aliases stockés** : chaque entité a un `aliases: [string]`. Recherche exact match d'abord.
3. **Similarité d'embedding** : si pas de match exact, tu compares l'embedding du nom contre les entités du même type. Si > 0.85, c'est probablement la même.
4. **Validation LLM** : en cas d'ambiguïté (0.70-0.85), tu demandes à un LLM "Marc et Marc Dupont sont-ils la même personne dans ce contexte ?" — c'est cher mais rare.
5. **Confirmation utilisateur** pour les cas vraiment ambigus : "Tu parles de Marc Dupont (ton collègue Keylo) ou Marc Lefèvre (que tu as rencontré chez OVH) ?"

C'est de loin la partie la plus pénible à bien faire, et celle qui détermine si ton KG reste utilisable à 6 mois.

**Extraction structurée — le prompt clé**

```python
EXTRACTION_PROMPT = """Tu es un extracteur de mémoire pour un assistant personnel.

Contexte conversation :
{recent_history}

Nouveau message utilisateur :
{user_message}

Entités déjà connues qui peuvent être liées :
{candidate_entities}  # top-K par recherche vectorielle sur le message

Ontologie autorisée :
- Types d'entités : Person, Project, Task, Note, Event, Topic, Tool, Place, Org
- Types de relations : BELONGS_TO, MENTIONS, ASSIGNED_TO, DEPENDS_ON,
                       HAPPENED_AT, WORKS_ON, RECOMMENDS, PREFERS, DECIDED,
                       LOCATED_AT, MEMBER_OF

Extrais en JSON :
{
  "should_memorize": bool,
  "reason_if_not": str | null,
  "entities": [
    {"existing_id": str | null, "type": str, "name": str,
     "aliases": [str], "properties": {}}
  ],
  "relations": [
    {"from": str, "to": str, "type": str, "properties": {},
     "confidence": float}
  ],
  "facts": [
    {"subject": str, "predicate": str, "object": str, "confidence": float}
  ]
}
"""
```

Tu passes `existing_id` quand l'entité est déjà dans `candidate_entities`. Sinon `null` et tu lui assigneras un ID après dedup.

**Gestion du temps et de l'obsolescence**
Une mémoire qui n'oublie jamais finit par être bruyante. Quelques tactiques :
- **Timestamp partout** : chaque nœud, chaque relation a `created_at`, `updated_at`, `last_referenced_at`
- **Confidence decay** : les faits perdent en confiance avec le temps si non confirmés
- **Contradiction detection** : avant d'écrire un fait, tu cherches s'il en existe un contradictoire. Si oui, tu déprécies l'ancien (`superseded_by`) plutôt que de l'écraser. Garde l'historique.
- **Retrieval pondère par fraîcheur** : un fait de la semaine pèse plus qu'un fait d'il y a un an, sauf s'il est marqué `permanent: true`

Exemple : "Je travaille chez X" il y a 6 mois, puis "J'ai changé pour Y" aujourd'hui → l'ancien fait est marqué `superseded_at=now, superseded_by=fact_456`, pas effacé. Si plus tard tu dis "rappelle-moi quand j'ai changé de boîte", l'historique est là.

**Confirmation interactive (le côté Jarvis)**
Pour les écritures importantes ou ambiguës, l'agent te demande confirmation avant de persister. Tu décides du seuil :

```
"Je note que tu as décidé d'utiliser Mistral OCR pour Keylo, c'est bien ça ?"
[oui/non/modifier]
```

Pour les petites infos triviales, il écrit silencieusement. Tu peux mettre un indicateur visuel dans ton UI Jarvis (genre une petite pulse sur la sphère + un toast "mémoire mise à jour : 2 entités, 1 relation") pour que ce ne soit pas un boîte noire.

**Mécanisme de correction**
Capital, parce que l'agent va se tromper. Tu dois pouvoir lui dire :

```
"Non, Marc travaille pas chez OVH, c'est chez Scaleway"
→ Agent : find Marc → find relation Marc WORKS_AT OVH → supersede → create new
```

Outil :

```python
correct_memory(description_of_correction)
# Le LLM doit identifier ce qui est faux et le corriger via les outils ci-dessus.
```

Ça nécessite un agent ReAct (boucle multi-tours) parce qu'il faut chercher l'erreur avant de la corriger.

**Architecture finale du backend**

```python
# Pseudo-code FastAPI
@app.websocket("/chat")
async def chat(ws):
    async for user_msg in ws:
        # 1. Récupération contexte (parallèle)
        context = await retrieve_context(user_msg)

        # 2. Génération réponse (streaming TTS)
        async for chunk in generate_response(user_msg, context):
            await ws.send_json(chunk)  # speech + ui chunks

        # 3. Mise à jour mémoire en background
        asyncio.create_task(
            update_memory(user_msg, assistant_response, context)
        )

async def update_memory(user_msg, asst_resp, context):
    extraction = await extract_with_llm(user_msg, asst_resp, context)
    if not extraction["should_memorize"]:
        return
    for entity in extraction["entities"]:
        await upsert_entity_dedup(entity)
    for relation in extraction["relations"]:
        await upsert_relation(relation)
    for fact in extraction["facts"]:
        await record_fact_with_contradiction_check(fact)
```

**Ordre de construction**
Vu la complexité, attaque dans cet ordre, sinon tu vas t'embourber :
1. RAG vectoriel pur d'abord (juste embeddings sur notes/messages) — ça doit déjà marcher
2. Outils de lecture sur Neo4j (function calling)
3. Outils d'écriture avec confirmation utilisateur systématique au début
4. Extraction structurée avec ontologie figée
5. Déduplication d'entités (commence simple : exact match sur nom normalisé)
6. Passage en écriture autonome avec UI de notification
7. Gestion des contradictions et obsolescence
8. Correction interactive

Chaque étape doit marcher solidement avant la suivante. Sinon tu vas avoir un graphe pourri et tu ne sauras pas si c'est l'extraction, la dedup, ou la query qui foire.

**Un détail crucial : versioning et rollback**
Garde un event log de toutes les mutations (style event-sourcing léger). Une simple table SQLite :

```
(timestamp, source_message_id, tool_name, args, result, can_undo)
```

Comme ça quand l'agent fait n'importe quoi, tu peux dire "annule les 5 dernières mémorisations" et tu rejoues l'inverse. Ça te sauvera la vie pendant le dev.

Tu veux qu'on creuse l'ontologie précise adaptée à ton usage (vu que tu fais du dev sur Keylo, gères des projets, etc.), le code de déduplication d'entités, ou la boucle ReAct pour les corrections multi-étapes ?
