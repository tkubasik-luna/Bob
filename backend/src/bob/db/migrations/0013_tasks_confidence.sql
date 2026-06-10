-- 0013 — tasks.confidence (fact-scope fast answer).
--
-- The sub-agent's terminal ``done`` action now carries a ``confidence``
-- field: 'confirmed' (the answer is cross-checked / directly stated by a
-- reliable source) or 'probable' (best available answer, not verified —
-- e.g. a fact-scope run that answered from its first search, or a run
-- terminated at the iteration cap). The orchestrator's done-synthesis
-- reads it to make Jarvis voice the uncertainty and offer a deeper
-- follow-up run instead of announcing every result as certain.
--
-- NULL means "legacy row / model did not set it" and is treated as
-- 'confirmed' (the pre-0013 behaviour: everything announced plainly).
--
-- Rollback: like 0012_tasks_scope.sql, leaving the column in place is
-- free; a true rollback would DELETE the bookkeeping row and rebuild
-- ``tasks`` via the standard sqlite CREATE/INSERT/DROP/RENAME dance.

ALTER TABLE tasks ADD COLUMN confidence TEXT;
