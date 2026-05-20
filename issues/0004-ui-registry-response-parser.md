## Parent

`prd/0001-bob-mvp-foundation.md`

## What to build

Construire le cœur du contrat LLM → UI : `ui_registry` (source de vérité du catalogue de composants côté backend) et `response_parser` (validation + retry + fallback). Testés en isolation avec `LLMClient` mocké, pas encore branchés sur le WS.

`ui_registry` : structure de données qui définit chaque composant disponible avec son nom et le JSON Schema de ses props. V0 inclut `ChatMessage` (props `{role: "assistant" | "user", content: string}`) et `Markdown` (props `{content: string}`). Le module expose :
- `get_response_schema() -> dict` : construit le JSON Schema complet de la réponse LLM attendue, soit `{speech: str, ui: [oneOf<components>]}` où `oneOf` énumère tous les composants connus. Format conforme à ce que LM Studio attend dans `response_format=json_schema`.
- `get_components_description_for_prompt() -> str` : produit une description Markdown lisible des composants disponibles, à injecter dans le system prompt pour que le LLM sache ce qu'il peut générer.
- `validate_response(payload: dict) -> ParsedResponse` : valide un payload contre le schéma via `pydantic` ou `jsonschema`, lève une exception typée en cas d'échec.

`response_parser` : reçoit la string brute renvoyée par le LLM. Tente `json.loads` + `ui_registry.validate_response`. Si succès, retourne `ParsedResponse`. Si échec, appelle `LLMClient.chat(...)` une seconde fois avec le message correctif ajouté (`"Ton dernier message était invalide : <erreur>. Réessaye en respectant strictement le schéma."`), retente parse+validate. Si second échec, retourne `ParsedResponse(speech=<string brute du premier essai>, ui=[])`. Toutes les erreurs intermédiaires loguées en WARN.

Le `response_parser` consomme `LLMClient` injecté par DI — pas d'instanciation interne — pour permettre le mock en test.

## Acceptance criteria

- [ ] Module `bob.ui_registry` définit `ChatMessage` et `Markdown` avec leurs schémas de props
- [ ] `get_response_schema()` produit un JSON Schema valide (validable par `jsonschema.Draft202012Validator.check_schema`)
- [ ] `get_response_schema()` impose `{speech: str, ui: array<oneOf<components>>}`
- [ ] `get_components_description_for_prompt()` retourne un Markdown listant les composants V0 avec leurs props
- [ ] `validate_response` accepte un payload conforme, rejette un payload non conforme avec exception typée
- [ ] Module `bob.response_parser` expose `async parse(raw_llm_output, llm_client, messages_so_far) -> ParsedResponse`
- [ ] JSON valide + schéma OK → retourne `ParsedResponse` directement, pas de retry
- [ ] JSON invalide → retry 1x avec message correctif → si OK retourne `ParsedResponse`
- [ ] Schema violation → même comportement de retry que JSON invalide
- [ ] Échec retry → retourne `ParsedResponse(speech=raw_first_attempt, ui=[])`
- [ ] Tests pytest couvrent : valid happy path, JSON syntax error → retry succeeds, schema violation → retry succeeds, both attempts fail → fallback
- [ ] Tests utilisent un fake `LLMClient` retournant des strings contrôlées
- [ ] `ruff`, `mypy strict`, `pytest` passent

## Blocked by

- `issues/0003-llm-client-config-prompts.md`
