"""SQLite persistence layer for Bob (Jarvis thread + future task tables).

Migrations live under :mod:`bob.db.migrations` as ``*.sql`` files applied at
boot, ordered by filename. The runner records applied filenames in a
``_migrations`` bookkeeping table so re-running is idempotent.
"""
