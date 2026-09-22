-- 0026_app_meta — a small key/value table for facts about *this ledger*, and its
-- first entry: ``ledger_id``.
--
-- The phone snapshot (ticket 73/74) publishes a derived projection of the
-- inventory under a per-ledger prefix, and a reader has to be able to ask "is
-- this snapshot from my ledger?" before it trusts a single row. That needs an
-- identity, and the identity has to live *in the database* rather than in
-- preferences.json: restore a backup onto a new machine and the ledger should
-- still be the same ledger, while the prefs file may well not have travelled.
--
-- Minted here, in SQL, rather than by the first Python caller that notices it is
-- missing. A migration runs exactly once, inside the runner's transaction, so
-- there is no window in which two callers both mint one and no "is it there yet"
-- branch to carry forever. ``randomblob(16)`` is 128 bits from SQLite's own RNG,
-- rendered as 32 lowercase hex characters — a UUID's worth of entropy without
-- depending on the UUID text format.
--
-- The table is deliberately generic. Anything else that is a property of the
-- ledger rather than of a row belongs here rather than in a new one-column
-- table, and it stays out of importexport._ALL_TABLES for the same reason
-- operation_run does: a full-backup restore should not overwrite the identity of
-- the ledger it is being restored into.

CREATE TABLE app_meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

INSERT INTO app_meta (key, value)
VALUES ('ledger_id', lower(hex(randomblob(16))));
