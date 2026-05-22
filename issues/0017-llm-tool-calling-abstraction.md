## Parent

prd/0003-jarvis-orchestrator.md

## What to build

Abstraction tool-calling unifiée pour les deux backends LLM existants. L'orchestrateur Jarvis (slice #4) doit pouvoir lui demander : "voici les messages + voici les tools dispos, renvoie soit un ou plusieurs tool calls, soit un texte direct". Sans connaître si c'est Claude CLI ou LM Studio en dessous.

Le slice livre :

- Interface commune `LLMClient.complete(messages, tools=None) -> Either[list[ToolCall], str]`.
- Implémentation Claude CLI : utilise tool calling natif Claude (format Anthropic).
- Implémentation LM Studio : utilise function-calling OpenAI-compatible (champ `tools` + `tool_choice="auto"` dans le payload chat completions). Si le modèle retourne du texte au lieu d'un tool call malgré tools fournis, on accepte et on renvoie comme texte (pas de fallback JSON-parsing — on assume modèle compatible).
- Types Python partagés : `ToolDefinition`, `ToolCall(name, arguments)`, `LLMResponse`.

Aucun usage en prod encore : ce slice expose juste l'API + ses tests unitaires avec mocks backend.

## Acceptance criteria

- [ ] Interface `LLMClient.complete(messages, tools=None)` ajoutée (sans casser les usages existants single-turn `complete_text`).
- [ ] `ClaudeCliClient.complete` supporte tools via format natif Anthropic.
- [ ] `LMStudioClient.complete` supporte tools via function-calling OpenAI-compatible (`tools` + `tool_choice="auto"`).
- [ ] Si LM Studio retourne du texte au lieu d'un tool call : accepté et renvoyé tel quel (pas de fallback parsing).
- [ ] Types `ToolDefinition`, `ToolCall`, `LLMResponse` exposés depuis `bob.llm.types`.
- [ ] Tests : LLM mock retourne tool call → parsing OK ; mock retourne texte → returned as text ; mock retourne malformed → exception explicite.
- [ ] Smoke test manuel avec un modèle LM Studio function-calling-capable + Claude CLI documenté dans le PR.
- [ ] README backend précise : "LM Studio model doit supporter function calling (ex: Qwen2.5, Llama 3.1 Instruct)".

## Blocked by

None - can start immediately.
