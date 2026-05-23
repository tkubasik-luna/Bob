## Parent

`prd/0004-sphere-hud-ui.md`

## What to build

Bootstrap du test runner frontend + premier deep module testable de bout en bout.

Installer Vitest + `@testing-library/react` + `@testing-library/jest-dom` + `jsdom`. Ajouter `frontend/vitest.config.ts` (env `jsdom`, hérite de `vite.config.ts`). Ajouter scripts `pnpm test` (run once) et `pnpm test:watch` dans `frontend/package.json`. Ajouter un fichier setup global (`frontend/src/test/setup.ts`) qui importe `@testing-library/jest-dom`.

Implémenter `shouldOverlayResponse(content: string): boolean` dans `frontend/src/lib/overlayHeuristic.ts`. Fonction pure. Retourne `true` si le contenu matche au moins un pattern markdown structurel (heading `#{1,6}\s`, liste `[-*]\s` ou `\d+\.\s`, code fence ` ``` `, blockquote `>\s`, table `\|.*\|`, lien `[..](..)`, hr `---`) **OU** si `content.split('\n').length > 3`. Sinon `false`.

Tests dans `frontend/src/lib/overlayHeuristic.test.ts` couvrant tous les cas (table-driven via `test.each`) : plain 1 ligne, plain 3 lignes, plain 4 lignes (déclenche par lignes), heading `#`/`##`/`###`, liste tiret, liste numérotée, code fence, blockquote, table GFM, lien inline, hr, vide, whitespace only, mix.

## Acceptance criteria

- [ ] `pnpm install` ajoute Vitest + RTL + jest-dom + jsdom comme devDependencies
- [ ] `pnpm test` exécute la suite et passe (zero test failures)
- [ ] `pnpm test:watch` lance Vitest en watch mode
- [ ] `vitest.config.ts` configure env `jsdom` et le setup file
- [ ] `shouldOverlayResponse` exporté depuis `frontend/src/lib/overlayHeuristic.ts`
- [ ] Tests couvrent au minimum 12 cas distincts dont 3 négatifs (return `false`)
- [ ] `pnpm check` (biome) passe sur les nouveaux fichiers
- [ ] `pnpm typecheck` passe

## Blocked by

None - can start immediately
