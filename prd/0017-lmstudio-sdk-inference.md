# PRD 0017 — Inférence LM Studio via le SDK `lmstudio` (au lieu de l'API OpenAI)

**Statut :** rédigé 2026-06-08.
**Investigation :** `docs/investigations/2026-06-08-lmstudio-sdk-inference.md` (table de décisions du grill).
**Déclencheur utilisateur :** « actuellement on utilise l'API OpenAI pour LM Studio, je voudrais qu'on utilise le SDK LMStudio directement ».

---

## Problem Statement

Aujourd'hui, toute l'inférence LM Studio de Bob passe par le client `openai`
(`AsyncOpenAI`) contre l'endpoint OpenAI-compatible `…/v1`. Le SDK officiel
`lmstudio` est déjà une dépendance et déjà utilisé — mais **uniquement** pour le
management des modèles (lister / charger / décharger / probe). L'inférence et le
management vivent donc sur deux transports différents pour le même serveur.

Du point de vue de l'utilisateur (développeur de Bob), cela pose :

- **Une dépendance à une couche de compatibilité** plutôt qu'au protocole natif de
  LM Studio. Les fonctionnalités spécifiques LM Studio (reasoning, stats de
  prédiction, speculative decoding, structured output natif) sont accédées par des
  champs « hors-OpenAI » bricolés (`extra_body`, `reasoning_content`,
  `stream_options.include_usage`) au lieu de l'API conçue pour.
- **Deux façons de parler au même serveur**, source de divergence et de confusion
  (deux configs, deux modèles mentaux).
- Le souhait explicite d'utiliser **le SDK directement** comme transport d'inférence.

## Solution

Introduire un nouveau client d'inférence `LMStudioSDKClient` qui implémente la même
interface `LLMClient` que l'actuel `LMStudioClient`, mais parle à LM Studio via le
SDK `lmstudio` (`AsyncClient` → `model.respond()` / `respond_stream()` / endpoint
low-level pour les outils). Il est sélectionné par un **flag** de transport, de sorte
que la bascule soit progressive et réversible :

- `LLM_LMSTUDIO_TRANSPORT=openai` (défaut au départ) → comportement actuel inchangé.
- `LLM_LMSTUDIO_TRANSPORT=sdk` → inférence via le SDK.

Une fois le transport SDK validé bout-en-bout sur un LM Studio réel (POC `chat()`
puis le reste), le transport OpenAI et la dépendance `openai` sont **supprimés**
(état final « SDK partout »). Le management reste tel quel (il était déjà SDK).

Le comportement observable de Bob doit rester **identique** : mêmes réponses, même
streaming token-par-token, même démarrage TTS précoce du `say` tool (le streaming
incrémental des arguments de tool-call est préservé), même feed reasoning, mêmes
stats perf, mêmes erreurs.

## User Stories

1. En tant que développeur de Bob, je veux que l'inférence LM Studio passe par le SDK
   `lmstudio`, afin d'utiliser le protocole natif plutôt qu'une couche de compat OpenAI.
2. En tant que développeur, je veux un flag `LLM_LMSTUDIO_TRANSPORT=sdk|openai`, afin
   de basculer entre les deux transports sans changer de code.
3. En tant que développeur, je veux que le flag soit à `openai` par défaut au début,
   afin que rien ne change tant que le SDK n'est pas validé.
4. En tant que développeur, je veux pouvoir revenir instantanément au transport OpenAI
   (rollback par flag), afin de ne prendre aucun risque sur la voix temps-réel shippée.
5. En tant qu'utilisateur de Bob, je veux que les réponses de chat soient identiques
   quel que soit le transport, afin de ne percevoir aucune régression.
6. En tant qu'utilisateur en mode vocal, je veux que le TTS démarre dès les premiers
   mots (comme aujourd'hui), afin que la latence de parole reste basse — donc le
   streaming incrémental des arguments du `say` tool doit être préservé.
7. En tant qu'utilisateur, je veux voir le feed de raisonnement en direct, afin de
   suivre la pensée de l'agent — le SDK doit surfacer les fragments `reasoning`.
8. En tant que développeur, je veux que le guided-JSON (`chat(schema=…)`) reste
   réellement contraint par le serveur, afin que le parse d'action du sous-agent reste
   valide par construction.
9. En tant qu'orchestrateur (acteur logiciel), je veux récupérer les tool-calls du
   modèle **sans qu'ils soient exécutés** par le SDK, afin de continuer à dispatcher
   chaque outil en sous-tâche comme aujourd'hui.
10. En tant qu'orchestrateur, je veux le streaming des tool-calls (start → args deltas
    → end), afin d'alimenter le `PartialJsonParser` du `say` tool tick par tick.
11. En tant que développeur, je veux que le niveau `reasoning` per-rôle (off/low/
    medium/high/on) continue de fonctionner, afin de ne pas perdre le contrôle shippé
    en 0108.
12. En tant que développeur, je veux que le host SDK soit dérivé de `LLM_BASE_URL`
    (via `host_from_base_url`), afin d'avoir une source de configuration unique.
13. En tant que développeur, je veux un `AsyncClient` long-vécu par rôle, afin
    d'amortir le handshake websocket et garder un TTFT bas.
14. En tant que développeur, je veux que le client soit reconstruit au swap de modèle/
    host (par le `RoleLLMSwitcher`), afin que le picker per-rôle continue de marcher à
    chaud.
15. En tant que développeur, je veux un reconnect automatique (retry une fois) si le
    websocket tombe, afin que Bob survive à une coupure transitoire du serveur.
16. En tant que développeur, je veux que les tool-calls soient advertis via le format
    natif du SDK (`ToolFunctionDef`/`raw_tools`), afin de ne plus dépendre du codec
    OpenAI pour LM Studio.
17. En tant que développeur, je veux que `LLM_TOOL_MODE` reste signifiant pour le
    transport OpenAI (pendant le side-by-side) et pour Claude CLI (hermes), afin de ne
    rien casser ailleurs ; `guided`/`hermes` sont rejetés proprement pour LM Studio SDK.
18. En tant que développeur, je veux que l'historique multi-tours (messages système,
    user, assistant-avec-tool_calls, résultats d'outils) soit correctement converti en
    `Chat` SDK, afin que les tours d'outils round-trippent fidèlement.
19. En tant que développeur, je veux que le fold du rôle `system_validator` (issue
    0048) soit appliqué avant conversion, afin de ne pas casser le contrat validateur.
20. En tant que développeur, je veux que les vrais comptes de tokens viennent des stats
    de prédiction du SDK, afin de garder l'observabilité (tokens in/out/reasoning).
21. En tant que développeur, je veux que les stats perf (TTFT, tok/s) viennent du SDK,
    afin que le footer du feed d'activité reste alimenté.
22. En tant que développeur, je veux que les erreurs SDK (`LMStudioError*`) soient
    mappées en `LLMClientError`, afin que les chemins de retry/dégradé existants
    fonctionnent sans changement.
23. En tant que développeur, je veux que la garde « contenu vide / pas de résultat »
    soit conservée, afin de surfacer un échec clair (modèle pas chargé, overflow).
24. En tant que développeur, je veux un **test de garde contractuel** sur l'override de
    l'API privée du SDK, afin d'être alerté bruyamment si un upgrade `lmstudio` change
    la forme des events wire.
25. En tant que développeur, je veux que la version `lmstudio` soit pinnée, afin de ne
    pas subir une rupture silencieuse de l'API privée à un upgrade non maîtrisé.
26. En tant que développeur, je veux que toute la suite de tests reste offline et
    déterministe (SDK faké à la frontière), afin de ne pas dépendre d'un serveur LM
    Studio en CI.
27. En tant que développeur, je veux pouvoir lancer un POC `chat()` sur un LM Studio
    réel derrière le flag, afin de valider la parité (notamment le mapping `reasoning`)
    avant de formaliser le reste.
28. En tant que développeur, une fois le SDK validé, je veux supprimer le transport
    OpenAI et la dépendance `openai`, afin d'atteindre l'état final « SDK partout » sans
    dette de double chemin.
29. En tant que développeur, je veux que le picker LLM (HUD, REST `/api/llm/*`)
    continue de fonctionner à l'identique, afin que la sélection de modèle/provider/
    reasoning reste pilotable.
30. En tant que développeur, je veux que les rôles Claude CLI restent inchangés, afin
    que seul le backend LM Studio bascule de transport.

## Implementation Decisions

**Stratégie de bascule (side-by-side + flag, puis purge).** Nouvelle classe
`LMStudioSDKClient` implémentant l'interface `LLMClient` (mêmes méthodes :
`chat`, `complete`, `stream_chat`, `stream_complete`, `supports_guided_json`). Le
choix entre `LMStudioClient` (OpenAI) et `LMStudioSDKClient` se fait dans la factory
de construction des clients, piloté par un nouveau réglage `LLM_LMSTUDIO_TRANSPORT`
(`sdk` | `openai`, défaut `openai` au démarrage du chantier). À la fin, le transport
OpenAI et la dépendance `openai` sont retirés (M8).

**Transport & config.** Le host `host:port` du SDK est dérivé de `LLM_BASE_URL` via
la fonction existante `host_from_base_url` (déjà utilisée par le management) — source
de configuration unique, override per-rôle préservé. Aucun réglage host dédié.

**Lifecycle des clients.** Un `AsyncClient` SDK long-vécu **par rôle** (lazy-connect,
websocket persistant amorti), fermé/recréé par le coordinateur de swap
(`RoleLLMSwitcher`) au changement de modèle/host. En cas de chute du websocket en
cours de session (`LMStudioWebsocketError`), reconnexion + retry une fois avant de
remonter une `LLMClientError`. Le management garde son client éphémère par appel.

**Mapping des méthodes.**
- `chat(schema)` → conversion des messages en `Chat` SDK puis `model.respond(...)`
  avec `response_format` quand `schema` est fourni ; lecture du contenu + stats.
- `stream_chat(schema)` → `model.respond_stream(...)` ; les fragments marqués
  reasoning deviennent des chunks `reasoning`, les fragments de contenu des chunks
  `text` ; les stats finales produisent le chunk `perf`. La concaténation des chunks
  `text` reconstruit exactement la string de `chat` (parse d'action sous-agent
  inchangé).
- `complete(tools)` → construction d'un endpoint chat low-level avec les outils
  convertis, déroulé des events de prédiction, **capture** des `ToolCallRequest`
  **sans exécution**, mappés en `ToolCall` Bob. Pas de tool-call → texte.
- `stream_complete(tools)` → même endpoint low-level ; fragments texte/reasoning →
  chunks ; tool-calls → cycle `tool_call_start` → `tool_call_args_delta`
  (incrémental) → `tool_call_end`.

**Outils (SDK-natifs, plus de codec OpenAI pour LM Studio).** Les `ToolDefinition`
Bob sont convertis en définitions d'outils natives du SDK (paramètres = JSON Schema).
L'ordre déterministe des specs est conservé en amont. Le codec natif OpenAI n'est plus
la frontière LM Studio ; le codec Hermes reste pour Claude CLI. `LLM_TOOL_MODE` reste
honoré pour le transport OpenAI (tant qu'il existe) et pour Claude CLI ; pour LM Studio
SDK, `native`/`auto` = outils SDK, `guided`/`hermes` = rejet clair. `supports_guided_json`
reste `True` (le `chat(schema)` est réellement gaté par le structured output natif).

**Préservation du streaming des arguments de tool-call.** Le SDK 1.5.0 jette les events
wire de fragments d'arguments incrémentaux. Décision : sous-classer l'endpoint de
réponse chat du SDK et overrider sa méthode d'itération des events de message pour
ressurfacer ces fragments sous forme d'un event interne, mappé en `tool_call_args_delta`.
C'est la seule dépendance à de l'API privée du SDK ; elle est isolée dans un module
unique et couverte par un **test de garde contractuel** + un pin de version `lmstudio`.

**Reasoning per-rôle.** Le niveau (`off`/`low`/`medium`/`high`/`on`) est transmis via le
passthrough de config brute du SDK (`config.raw`, clé `reasoning`), équivalent SDK de
l'actuel `extra_body.reasoning`. La parité réelle est validée au POC `chat()` ; si le
champ n'a aucun effet, fallback = omission (le modèle choisit), sans bloquer la
migration.

**Conversion de l'historique.** Un converter dédié transforme la liste de messages Bob
(dicts) en `Chat` SDK : système, user, assistant, et — pour les tours d'outils
antérieurs — assistant-avec-tool_calls + résultats d'outils. Le fold du rôle
`system_validator` (issue 0048) est appliqué avant conversion ; l'assertion des rôles
standard est conservée.

**Observabilité & erreurs.** Les vrais comptes de tokens et les stats perf (TTFT,
tok/s) proviennent des stats de prédiction du SDK. Les events de debug
(`llm_call_start`/`end`), `log_llm_call`, et la garde « contenu vide / pas de résultat »
sont conservés à l'identique autour des nouveaux appels. Les erreurs `LMStudioError*`
du SDK sont mappées en `LLMClientError`.

**Modules.**
- **M1** — Réglage `LLM_LMSTUDIO_TRANSPORT` + sélection dans la factory de clients.
- **M2** — `LMStudioSDKClient` : façade `LLMClient` sur le SDK (mince ; la logique vit
  dans M3–M5).
- **M3** — Converter historique → `Chat` SDK (deep, pur, testable isolément).
- **M4** — Converter outils : `ToolDefinition` → définition SDK ; `ToolCallRequest` →
  `ToolCall` Bob (deep, pur).
- **M5** — Adapter de streaming : events de prédiction SDK → `StreamChunk` Bob
  (text/reasoning/tool_call_*/perf) (deep ; testé via itérateur d'events scripté).
- **M6** — Sous-classe d'endpoint + override de l'itération d'events (seam API privée)
  pour ressurfacer les fragments d'arguments + test de garde contractuel.
- **M7** — Lifecycle/registry : `AsyncClient` long-vécu par rôle, reconnect+retry-once,
  intégration au coordinateur de swap et au boot.
- **M8** — Purge : retrait du transport OpenAI + dépendance `openai` (phase finale,
  post-validation).

## Testing Decisions

Tests pour **tous les modules** (réponse utilisateur). Principe : tester le
**comportement externe** (entrées → sorties observables, séquences de `StreamChunk`,
erreurs typées), jamais les détails d'implémentation. La suite reste **offline et
déterministe** : le SDK est faké à sa frontière, exactement comme `test_lm_studio_manager`
fake déjà le SDK pour le management (prior art principal). Aucun serveur LM Studio
requis en CI.

- **M3 (converter historique)** — tests purs : divers historiques (système seul,
  multi-tours, assistant-avec-tool_calls + résultats, fold `system_validator`) →
  `Chat` SDK attendu.
- **M4 (converter outils)** — tests purs : `ToolDefinition` → définition SDK (paramètres
  JSON Schema), et `ToolCallRequest` → `ToolCall` (ids, noms, arguments décodés ;
  arguments malformés → `LLMClientError`, parité avec les fixtures golden existantes).
- **M5 (adapter streaming)** — tests pilotés par un **itérateur d'events scripté** :
  séquences text-only, reasoning + text, tool-call (start → args deltas incrémentaux →
  end), chunk de stats final → `perf`. Assertion clé : `tool_call_args_delta` arrive
  **avant** `tool_call_end` (garantie du démarrage TTS précoce du `say`).
- **M6 (override + garde)** — **test de garde contractuel** : alimente la sous-classe
  avec des dicts de messages canal fakés (dont le fragment d'arguments) et asserte que
  l'override émet l'event attendu ; échoue bruyamment si la forme wire / le handler du
  SDK change. Sert de sentinelle anti-régression d'upgrade.
- **M2 (façade) / M7 (lifecycle)** — tests d'intégration légers avec un faux
  `AsyncClient` : sélection par flag (`sdk` vs `openai`), parité de surface de `chat`/
  `complete`, reconnect+retry-once sur websocket simulé tombé, reconstruction au swap.
- **Parité de comportement** — réutiliser, là où c'est pertinent, les attentes des
  tests existants de `LMStudioClient` (mêmes sorties pour les mêmes entrées) afin de
  prouver l'équivalence observable entre transports.

Le mapping `reasoning` via `config.raw` est validé **manuellement** au POC contre un LM
Studio réel (hors CI), puisque son effet dépend du modèle/serveur.

## Out of Scope

- **Budget / fenêtre de contexte** : adoption de `count_tokens` / `get_context_length`
  du SDK pour un garde-fou de contexte — enhancement séparé. On garde l'estimate char/4
  pour le summary de début et les vrais counts via les stats.
- **Management des modèles** : déjà sur le SDK ; non touché par ce PRD (sauf intégration
  lifecycle au swap).
- **Claude CLI** : transport inchangé (codec Hermes), hors périmètre.
- **Embeddings, image input, speculative decoding** : non utilisés par Bob aujourd'hui ;
  hors scope.
- **Patch upstream du SDK** (PR au projet `lmstudio` pour surfacer les fragments
  d'arguments) : on fait un override local ; un éventuel upstream est un suivi séparé.
- **Refonte du picker / REST `/api/llm/*`** : doit continuer à marcher tel quel, pas de
  refonte.

## Further Notes

- **Risque principal = M6 (API privée).** Mitigations : surface d'accès minimale et
  isolée dans un seul module, test de garde contractuel, pin de version `lmstudio`. Si
  l'upstream finit par exposer les fragments d'arguments, M6 pourra être retiré au
  profit de l'API publique.
- **Ordre de livraison conseillé (tracer-bullet).** D'abord M1 + un `chat()` minimal
  (POC validé sur LM Studio réel, dont la parité `reasoning`), puis `stream_chat`, puis
  la capture de tool-calls (`complete`), puis le streaming des tool-calls + override
  (`stream_complete` + M6), puis lifecycle/swap (M7), enfin la purge (M8) une fois tout
  vert et validé en usage réel.
- **Invariant de robustesse :** tant que M8 n'est pas fait, `LLM_LMSTUDIO_TRANSPORT=openai`
  doit reproduire le comportement actuel à l'identique (rollback sûr).
- **Voix temps-réel (PRD 0016) :** la préservation du `tool_call_args_delta` incrémental
  est un critère d'acceptation dur — toute dégradation vers un envoi « whole-call »
  serait une régression de latence vocale.
