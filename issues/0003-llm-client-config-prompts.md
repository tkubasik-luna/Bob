## Parent

`prd/0001-bob-mvp-foundation.md`

## What to build

Construire les modules backend qui isolent le LLM : `config`, `llm_client` (interface abstraite + impl `LMStudioClient`), `prompts` (loader fichiers `.md`). À ce stade, aucune intégration avec le WS ou la conversation — c'est de la plomberie qui sera consommée par les slices suivantes. Validation via tests unitaires mockés et un script smoke CLI.

`config` : module qui charge `.env` via `pydantic-settings`, expose un objet `Settings` immuable avec `LLM_BASE_URL`, `LLM_MODEL`, `LLM_API_KEY`, `BACKEND_HOST`, `BACKEND_PORT`, `LOG_LEVEL`, `LLM_TIMEOUT_SECONDS` (défaut 60). Crash early si vars manquantes.

`llm_client` : classe abstraite `LLMClient` avec méthode `async chat(messages: list[dict], schema: dict | None = None) -> str` (retourne la string brute renvoyée par le LLM). Implémentation `LMStudioClient` qui wrappe `openai.AsyncOpenAI(base_url=..., api_key=...)`. Quand `schema` est fourni, passe `response_format={"type": "json_schema", "json_schema": schema}` à LM Studio. Pas de retry interne (géré par `response_parser` en aval). Timeout configurable.

`prompts` : loader qui lit tous les fichiers `.md` dans `backend/prompts/` au démarrage et expose `render(name: str, **kwargs) -> str`. Templating via `str.format` ou jinja2 (au choix de l'impl). Créer un premier prompt `system_chat.md` placeholder (sera complété en slice 5).

Smoke CLI : un script `python -m bob.smoke "<prompt>"` (ou équivalent) qui charge config, instancie `LMStudioClient`, envoie un message au LLM, print la réponse brute. Permet de valider la connexion à LM Studio en local sans WS.

## Acceptance criteria

- [ ] Module `bob.config` charge `.env` via `pydantic-settings`, expose `Settings`
- [ ] Boot crash explicite si une var requise est manquante
- [ ] Interface abstraite `LLMClient` définit `async chat(messages, schema=None) -> str`
- [ ] Implémentation `LMStudioClient` utilise `openai.AsyncOpenAI` avec `base_url` paramétré
- [ ] Quand `schema` fourni, `LMStudioClient` envoie `response_format=json_schema` à LM Studio
- [ ] Timeout LLM configurable et appliqué (default 60s)
- [ ] Module `bob.prompts` charge `backend/prompts/*.md` au démarrage
- [ ] `prompts.render("system_chat", var="x")` retourne le contenu templaté
- [ ] Fichier `backend/prompts/system_chat.md` placeholder créé
- [ ] Script smoke `python -m bob.smoke "hello"` envoie un message à LM Studio et print la réponse
- [ ] Tests pytest avec `LLMClient` mocké (subclasse fake) validant que `LMStudioClient` formate correctement les appels (params openai SDK attendus)
- [ ] `ruff`, `mypy strict`, `pytest` passent

## Blocked by

- `issues/0001-scaffold-monorepo-tooling.md`
