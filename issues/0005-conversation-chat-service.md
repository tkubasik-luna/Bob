## Parent

`prd/0001-bob-mvp-foundation.md`

## What to build

Brancher tous les modules backend ensemble en un orchestrateur unique `chat_service`, avec la gestion d'historique conversation. Encore pas d'intégration WS — validation via script CLI smoke et tests unitaires. À la fin de la slice, on doit pouvoir faire un échange end-to-end avec LM Studio en ligne de commande, avec historique multi-tour.

`conversation` : module qui maintient `dict[session_id, list[Message]]` in-memory. API :
- `append(session_id, role, content)` ajoute un message
- `get_history(session_id) -> list[Message]` retourne l'historique ordonné
- `clear(session_id)` purge la session
- Opérations sur session inconnue sont no-op (pas d'exception)

`chat_service` : orchestrateur principal. API : `async handle_user_message(session_id, user_content) -> ParsedResponse`. Logique :
1. Append le message user à `conversation`
2. Construit la liste `messages` à envoyer au LLM : system prompt (chargé via `prompts.render("system_chat", components_description=ui_registry.get_components_description_for_prompt())`) + historique
3. Appelle `llm_client.chat(messages, schema=ui_registry.get_response_schema())`
4. Passe la sortie à `response_parser.parse(...)` (qui peut retry une fois, en construisant ses propres messages éphémères sans toucher à `conversation`)
5. Append le `speech` final comme message assistant dans `conversation`
6. Retourne le `ParsedResponse`

Le prompt `system_chat.md` est complété : décrit le rôle d'assistant personnel, impose le format JSON, contient un placeholder `{components_description}` injecté au runtime.

Smoke CLI étendu : `python -m bob.smoke` lance une REPL où chaque ligne tapée est passée à `chat_service.handle_user_message(session_id, line)`, la `ParsedResponse` est pretty-printed. Permet de valider l'historique multi-tour ("souviens-toi de mon prénom" → "comment je m'appelle ?").

## Acceptance criteria

- [ ] Module `bob.conversation` expose `append`, `get_history`, `clear`
- [ ] Sessions isolées : deux `session_id` différents ne se voient pas mutuellement
- [ ] `clear` sur session inconnue ne crashe pas
- [ ] Module `bob.chat_service` expose `async handle_user_message(session_id, user_content) -> ParsedResponse`
- [ ] System prompt injecté en première position de `messages`
- [ ] `components_description` rendu dans le system prompt à chaque appel
- [ ] User content append dans conversation AVANT appel LLM
- [ ] Assistant `speech` append APRÈS validation de la réponse
- [ ] Le retry interne de `response_parser` n'écrit pas le message intermédiaire dans `conversation`
- [ ] Fallback texte produit bien un append assistant avec `speech` brut et `ui: []`
- [ ] Fichier `backend/prompts/system_chat.md` complet avec placeholder `{components_description}`
- [ ] Smoke CLI REPL : tape "mon prénom est Tom" puis "comment je m'appelle ?" → la réponse mentionne "Tom" (validation manuelle avec LM Studio réel + Qwen 7B)
- [ ] Tests pytest avec `LLMClient` mocké :
  - [ ] échange simple ajoute exactement 2 messages (user + assistant) à `conversation`
  - [ ] system prompt présent en première position
  - [ ] retry parser ne pollue pas `conversation`
  - [ ] fallback texte produit `ui: []` dans la conversation
- [ ] `ruff`, `mypy strict`, `pytest` passent

## Blocked by

- `issues/0003-llm-client-config-prompts.md`
- `issues/0004-ui-registry-response-parser.md`
